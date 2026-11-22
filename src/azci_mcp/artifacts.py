"""Artifact storage: download from Azure, save under a per-user tree, sign links, sweep."""

from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .azure import AzureError
from .config import Config, redact


def stable_hash(text: str) -> str:
    """Deterministic, non-reversible directory component for a user/conversation id."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class Artifact:
    id: str
    name: str
    mime_type: str
    size: int
    created_at: float
    user_hash: str
    conversation: str
    run_id: str


_ID_RE = __import__("re").compile(r"^[a-f0-9]{32}$")


def _guess_mime(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


class ArtifactStore:
    def __init__(self, config: Config):
        self.root = Path(config.artifact_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.signing_key = hashlib.sha256(b"azci-signing:" + config.mcp_token.encode()).digest()
        self.retention_s = config.retention_days * 86400
        self.max_file_bytes = config.output_max_file_bytes
        self.max_files = config.output_max_files

    # -- paths -------------------------------------------------------------
    def _run_dir(self, user_hash: str, conversation: str, run_id: str) -> Path:
        return self.root / user_hash / conversation / run_id

    def _data_path(self, artifact: Artifact) -> Path:
        return self._run_dir(artifact.user_hash, artifact.conversation, artifact.run_id) / artifact.id

    def _meta_path(self, artifact: Artifact) -> Path:
        return self._data_path(artifact).with_suffix(".meta.json")

    # -- save --------------------------------------------------------------
    def save(
        self,
        *,
        user_hash: str,
        conversation: str,
        run_id: str,
        name: str,
        data: bytes,
        mime_type: str | None = None,
    ) -> Artifact:
        if len(data) > self.max_file_bytes:
            raise ValueError(f"{name} is {len(data)} bytes, over the {self.max_file_bytes} byte cap")
        artifact = Artifact(
            id=secrets.token_hex(16),
            name=name,
            mime_type=mime_type or _guess_mime(name),
            size=len(data),
            created_at=time.time(),
            user_hash=user_hash,
            conversation=conversation,
            run_id=run_id,
        )
        run_dir = self._run_dir(user_hash, conversation, run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        self._data_path(artifact).write_bytes(data)
        self._meta_path(artifact).write_text(
            json.dumps({"name": name, "mime_type": artifact.mime_type, "created_at": artifact.created_at})
        )
        return artifact

    def count_in_run(self, user_hash: str, conversation: str, run_id: str) -> int:
        run_dir = self._run_dir(user_hash, conversation, run_id)
        if not run_dir.exists():
            return 0
        return sum(1 for p in run_dir.iterdir() if p.is_file() and not p.name.endswith(".meta.json"))

    # -- signed links ------------------------------------------------------
    def token(self, artifact_id: str, ttl_s: int = 86400) -> str:
        expires = int(time.time()) + ttl_s
        payload = f"{artifact_id}.{expires}"
        mac = hmac.new(self.signing_key, payload.encode(), hashlib.sha256).hexdigest()
        return f"{artifact_id}.{expires}.{mac}"

    def verify(self, token: str) -> str | None:
        """Return the artifact id if the token is valid and unexpired, else None."""
        try:
            artifact_id, expires_raw, mac = token.split(".", 2)
            expires = int(expires_raw)
        except (ValueError, AttributeError):
            return None
        payload = f"{artifact_id}.{expires}"
        expected = hmac.new(self.signing_key, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mac, expected):
            return None
        if expires < int(time.time()):
            return None
        return artifact_id

    # -- lookup ------------------------------------------------------------
    def find(self, artifact_id: str) -> Artifact | None:
        """Resolve an artifact id to its record, refusing anything that is not a bare hex id."""
        if not _ID_RE.fullmatch(artifact_id):
            return None
        matches = list(self.root.rglob(artifact_id))
        if not matches:
            return None
        data_path = matches[0]
        meta_path = data_path.with_suffix(".meta.json")
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        rel = data_path.relative_to(self.root).parts
        return Artifact(
            id=artifact_id,
            name=meta.get("name", artifact_id),
            mime_type=meta.get("mime_type", "application/octet-stream"),
            size=data_path.stat().st_size,
            created_at=meta.get("created_at", data_path.stat().st_mtime),
            user_hash=rel[0],
            conversation=rel[1],
            run_id=rel[2],
        )

    # -- retention ---------------------------------------------------------
    def sweep(self, now: float | None = None) -> int:
        now = now or time.time()
        cutoff = now - self.retention_s
        removed = 0
        for path in list(self.root.rglob("*")):
            if not path.is_file():
                continue
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        for run_dir in sorted([p for p in self.root.rglob("*") if p.is_dir()], key=lambda p: len(p.parts), reverse=True):
            if run_dir != self.root and not any(run_dir.iterdir()):
                run_dir.rmdir()
        return removed

    # -- Azure retrieval ---------------------------------------------------
    async def download_container_file(
        self, client: httpx.AsyncClient, config: Config, container_id: str, file_id: str
    ) -> bytes:
        url = f"{config.azure_base_url.rstrip('/')}/containers/{container_id}/files/{file_id}/content"
        headers = {"Authorization": f"Bearer {config.azure_api_key}"}
        response = await client.get(url, headers=headers, timeout=config.default_timeout_s)
        if response.status_code != 200:
            raise AzureError(
                response.status_code,
                redact(f"failed to download {file_id}: HTTP {response.status_code}", config.secrets()),
            )
        return response.content
