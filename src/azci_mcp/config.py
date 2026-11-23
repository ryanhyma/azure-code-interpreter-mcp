"""Configuration loading and redaction for the Azure Code Interpreter MCP server.

The server holds two secrets that must never leak into tool results, logs or
error bodies: the Azure key and the shared MCP token. Everything that formats
an error or a log line goes through :func:`redact`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

DEFAULT_BASE_URL = "https://hymafoundry.openai.azure.com/openai/v1"
DEFAULT_MODEL = "gpt-6-luna"

# Input validation limits (SC-10).
TASK_MAX_CHARS = 8000
INPUT_MAX_FILES = 5
INPUT_MAX_FILE_BYTES = 10 * 1024 * 1024

# Output artifact limits (business rules).
OUTPUT_MAX_FILES = 20
OUTPUT_MAX_FILE_BYTES = 25 * 1024 * 1024

MIB = 1024 * 1024


def _default_artifact_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / "azure-ci-mcp" / "artifacts"


@dataclass(frozen=True)
class Config:
    azure_api_key: str
    azure_base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    mcp_token: str = ""
    librechat_url: str = "http://127.0.0.1:3080"
    attach_chat_files_default: bool = True
    mcp_host: str = "172.17.0.1"
    mcp_port: int = 8935
    download_host: str = "127.0.0.1"
    download_port: int = 8936
    artifact_dir: Path = field(default_factory=_default_artifact_dir)
    retention_days: int = 7
    # Keep a local copy of sandbox outputs (durable fallback + signed links).
    # Disable for a pure pass-through: outputs only travel inline to LibreChat.
    store_artifacts: bool = True
    default_timeout_s: int = 180
    max_timeout_s: int = 600
    max_concurrency: int = 4
    container_idle_ttl_s: int = 18 * 60
    task_max_chars: int = TASK_MAX_CHARS
    input_max_files: int = INPUT_MAX_FILES
    input_max_file_bytes: int = INPUT_MAX_FILE_BYTES
    output_max_files: int = OUTPUT_MAX_FILES
    output_max_file_bytes: int = OUTPUT_MAX_FILE_BYTES
    env_file: Path | None = None

    # -- derived URLs ---------------------------------------------------
    @property
    def download_origin(self) -> str:
        return f"http://{self.download_host}:{self.download_port}"

    @property
    def bridge_origin(self) -> str:
        # The LibreChat container reaches the host via the docker0 gateway.
        return f"http://host.docker.internal:{self.download_port}"

    def secrets(self) -> tuple[str, ...]:
        return tuple(s for s in (self.azure_api_key, self.mcp_token) if s)


def redact(text: str, secrets: tuple[str, ...]) -> str:
    """Mask every configured secret in *text* with ``***``."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, sep, value = line.partition("=")
        if not sep:
            continue
        value = value.strip().strip("'\"")
        values[key.strip()] = value
    return values


def load(env_file: Path | None = None) -> Config:
    """Build a Config from an env file merged over the process environment.

    Process environment wins over the env file, so a service unit can set the
    file path and an operator can still override a single value inline.
    """
    file_values: dict[str, str] = {}
    if env_file is None:
        raw = os.environ.get("AZCI_ENV_FILE", "")
        env_file = Path(raw) if raw else None
    if env_file is not None:
        file_values = _parse_env_file(env_file)

    def pick(name: str, default: str = "") -> str:
        return os.environ.get(name) or file_values.get(name) or default

    def pick_int(name: str, default: int) -> int:
        raw = pick(name, str(default))
        return int(raw) if raw else default

    return Config(
        azure_api_key=pick("AZURE_AI_KEY"),
        azure_base_url=pick("AZCI_AZURE_BASE_URL", DEFAULT_BASE_URL),
        model=pick("AZCI_MODEL", DEFAULT_MODEL),
        mcp_token=pick("AZCI_MCP_TOKEN"),
        librechat_url=pick("AZCI_LIBRECHAT_URL", "http://127.0.0.1:3080"),
        attach_chat_files_default=pick("AZCI_ATTACH_CHAT_FILES", "true").lower() != "false",
        mcp_host=pick("AZCI_MCP_HOST", "172.17.0.1"),
        mcp_port=pick_int("AZCI_MCP_PORT", 8935),
        download_host=pick("AZCI_DOWNLOAD_HOST", "127.0.0.1"),
        download_port=pick_int("AZCI_DOWNLOAD_PORT", 8936),
        artifact_dir=Path(pick("AZCI_ARTIFACT_DIR", str(_default_artifact_dir()))),
        retention_days=pick_int("AZCI_RETENTION_DAYS", 7),
        store_artifacts=pick("AZCI_STORE_ARTIFACTS", "true").lower() != "false",
        default_timeout_s=pick_int("AZCI_DEFAULT_TIMEOUT_S", 180),
        max_timeout_s=pick_int("AZCI_MAX_TIMEOUT_S", 600),
        max_concurrency=pick_int("AZCI_MAX_CONCURRENCY", 4),
        container_idle_ttl_s=pick_int("AZCI_CONTAINER_IDLE_TTL_S", 18 * 60),
        task_max_chars=pick_int("AZCI_TASK_MAX_CHARS", TASK_MAX_CHARS),
        input_max_files=pick_int("AZCI_INPUT_MAX_FILES", INPUT_MAX_FILES),
        input_max_file_bytes=pick_int("AZCI_INPUT_MAX_FILE_BYTES", INPUT_MAX_FILE_BYTES),
        output_max_files=pick_int("AZCI_OUTPUT_MAX_FILES", OUTPUT_MAX_FILES),
        output_max_file_bytes=pick_int("AZCI_OUTPUT_MAX_FILE_BYTES", OUTPUT_MAX_FILE_BYTES),
        env_file=env_file,
    )


def missing_required(config: Config) -> list[str]:
    """Name the required-but-unset fields, in a stable order."""
    missing = []
    if not config.azure_api_key:
        missing.append("AZURE_AI_KEY")
    if not config.mcp_token:
        missing.append("AZCI_MCP_TOKEN")
    return missing
