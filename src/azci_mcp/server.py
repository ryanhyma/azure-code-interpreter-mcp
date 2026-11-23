"""MCP server: the `run_code` tool, identity headers, artifact delivery, downloads."""

from __future__ import annotations

import asyncio
import base64
import hmac
from dataclasses import dataclass, field
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp_types import CallToolResult, ImageContent, TextContent
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .artifacts import Artifact, ArtifactStore, stable_hash
from .librechat import available_files, fetch_chat_files
from .azure import AzureError, InputFile, delete_container, list_container_files, run
from .config import Config, load, redact
from pathlib import Path
from .sessions import SessionRegistry
import logging

logger = logging.getLogger("azci_mcp")

IMAGE_MIME = {"image/png", "image/jpeg", "image/gif", "image/webp"}
MAX_INLINE_IMAGE_BYTES = 5 * 1024 * 1024

mcp = MCPServer(
    "azureci",
    version="0.1.0",
    instructions=(
        "Run Python code in an Azure-hosted sandbox (executor mode: pass the code, it is run "
        "verbatim). Returns the exact stdout, the assistant answer, generated files as LibreChat "
        "attachments, and charts inline."
    ),
)


class FileInput(BaseModel):
    name: str
    data: str  # base64-encoded bytes
    mime_type: str = "application/octet-stream"


@dataclass
class Identity:
    allowed: bool
    user_id: str = ""
    conversation_id: str = ""
    agent_id: str = ""
    reason: str = ""


@dataclass
class OutputFile:
    """A sandbox output ready to render, stored or pass-through."""

    name: str
    mime_type: str
    size: int
    data: bytes
    artifact: Artifact | None = None


@dataclass
class Runtime:
    config: Config
    client: httpx.AsyncClient
    store: ArtifactStore
    sessions: SessionRegistry
    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(4))


_runtime: Runtime | None = None


async def _get_runtime() -> Runtime:
    global _runtime
    if _runtime is None:
        config = load(None)
        _runtime = Runtime(
            config=config,
            client=httpx.AsyncClient(timeout=config.default_timeout_s),
            store=ArtifactStore(config),
            sessions=SessionRegistry(config),
            semaphore=asyncio.Semaphore(config.max_concurrency),
        )
    return _runtime


def authenticate(headers: Any, config: Config) -> Identity:
    token = headers.get("x-azci-token", "") if headers is not None else ""
    if not config.mcp_token or not hmac.compare_digest(str(token), config.mcp_token):
        return Identity(allowed=False, reason="missing or wrong X-Azci-Token")
    return Identity(
        allowed=True,
        user_id=(headers.get("x-librechat-user-id") or "") if headers is not None else "",
        conversation_id=(headers.get("x-librechat-conversation-id") or "") if headers is not None else "",
        agent_id=(headers.get("x-librechat-agent-id") or "") if headers is not None else "",
    )


def validate_input(config: Config, code: str, task: str | None, files: list | None) -> str | None:
    if not code or not code.strip():
        return "code is required"
    combined = len(code) + len(task or "")
    if combined > config.task_max_chars:
        return f"input exceeds {config.task_max_chars} characters"
    if files:
        if len(files) > config.input_max_files:
            return f"too many input files (max {config.input_max_files})"
        for item in files:
            name = (item or {}).get("name", "?")
            try:
                raw = base64.b64decode((item or {}).get("data", ""), validate=True)
            except Exception:
                return f"file {name!r} has malformed base64"
            if len(raw) > config.input_max_file_bytes:
                return f"file {name!r} exceeds {config.input_max_file_bytes} bytes"
    return None


def _clamp_timeout(config: Config, timeout_s: int) -> int:
    if timeout_s and timeout_s > 0:
        return min(timeout_s, config.max_timeout_s)
    return config.default_timeout_s



def hydrate_loaded_names(code: str, names: list[str]) -> str:
    """Copy hash-prefixed imported files to their plain names before the user's code runs.

    Azure stores an uploaded input as `/mnt/data/<hash>-<filename>`. Models reasonably try
    `/mnt/data/<filename>` and then guess; normalizing here makes the requested name stable.
    """
    if not names:
        return code
    quoted = ", ".join(repr(name) for name in names)
    preamble = (
        "import glob as _azci_glob, os as _azci_os, shutil as _azci_shutil\n"
        f"for _azci_name in [{quoted}]:\n"
        "    _azci_dst = _azci_os.path.join('/mnt/data', _azci_name)\n"
        "    if not _azci_os.path.exists(_azci_dst):\n"
        "        for _azci_src in _azci_glob.glob('/mnt/data/*-' + _azci_name):\n"
        "            _azci_shutil.copyfile(_azci_src, _azci_dst)\n"
        "            break\n"
    )
    return preamble + code

def _error(message: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=message)], is_error=True)


def _image_extension(name: str) -> str | None:
    lower = name.lower()
    for ext, mime in {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.items():
        if lower.endswith(ext):
            return mime
    return None


def build_result(
    config: Config,
    store: ArtifactStore,
    *,
    run_id: str,
    code: str,
    stdout: str,
    answer: str,
    files: list[OutputFile],
    available: list[dict] | None = None,
) -> CallToolResult:
    """Text + inline images + the generic structuredContent.artifacts contract.

    With `store_artifacts` off the bytes still travel inline, but no local copy or
    signed link is produced and the download listener is not started.
    """
    content: list = []
    lines = [f"run {run_id}"]
    if stdout:
        lines.append(f"stdout:\n{stdout.rstrip()}")
    if answer:
        lines.append(f"answer:\n{answer.rstrip()}")

    artifact_entries: list[dict] = []
    file_lines: list[str] = []
    for item in files:
        if item.artifact is not None:
            token = store.token(item.artifact.id)
            browser = f"{config.download_origin}/f/{item.artifact.id}?t={token}"
            bridge = f"{config.bridge_origin}/f/{item.artifact.id}?t={token}"
        else:
            browser = bridge = None
        inlined = item.mime_type in IMAGE_MIME and item.size <= MAX_INLINE_IMAGE_BYTES
        if inlined:
            # Delivered as an image block; LibreChat's image path already stores a
            # downloadable file record, so listing it here too would duplicate it.
            content.append(
                ImageContent(
                    type="image",
                    data=base64.b64encode(item.data).decode(),
                    mime_type=item.mime_type,
                )
            )
        file_lines.append(f"- {item.name} → {browser}" if browser else f"- {item.name}")
        if not inlined:
            entry = {
                "name": item.name,
                "mimeType": item.mime_type,
                "bytes": base64.b64encode(item.data).decode(),
                "size": item.size,
            }
            if bridge:
                entry["downloadUrl"] = bridge
                entry["browserUrl"] = browser
            artifact_entries.append(entry)
    if file_lines:
        lines.append("files:\n" + "\n".join(file_lines))
    if available:
        lines.append(
            "available files (not loaded; call run_code with attach_files to load):\n"
            + "\n".join(f"- {entry['name']} ({entry['mime_type']})" for entry in available)
        )
    content.insert(0, TextContent(type="text", text="\n\n".join(lines)))
    return CallToolResult(content=content, structured_content={"artifacts": artifact_entries})


async def handle_run(
    runtime: Runtime,
    headers: Any,
    *,
    code: str,
    task: str | None = None,
    files: list | None = None,
    timeout_s: int = 0,
    attach_files: list[str] | str = "none",
) -> CallToolResult:
    config = runtime.config
    identity = authenticate(headers, config)
    if not identity.allowed:
        return _error(f"unauthorized: {identity.reason}")

    problem = validate_input(config, code, task, files)
    if problem:
        return _error(problem)

    base_files = [
        InputFile(name=f["name"], mime_type=f.get("mime_type", "application/octet-stream"), data=base64.b64decode(f["data"]))
        for f in (files or [])
    ]
    input_files = list(base_files)

    keyed = bool(identity.user_id and identity.conversation_id)
    key = SessionRegistry.key(identity.user_id, identity.conversation_id) if keyed else None

    lock = await runtime.sessions.lock_for(key) if key else None
    if lock:
        await lock.acquire()
    try:
        async with runtime.semaphore:
            container_id = runtime.sessions.get(key) if key else None

            # Files are on demand: nothing is copied into the sandbox unless the caller asks
            # for it by name (`attach_files`) or asks for everything (`all`). The manifest in
            # the result tells the model what is available.
            imported_before = runtime.sessions.imported(key) if (key and container_id) else set()
            catalog: list[dict] = []
            if keyed:
                catalog = await available_files(
                    runtime.client,
                    config,
                    identity.user_id,
                    identity.conversation_id,
                    identity.agent_id,
                )
            known_names = {f.name for f in input_files}
            requested = attach_files
            wanted_names: list[str] | None = None
            load_all = requested == "all"
            if isinstance(requested, list):
                wanted_names = [name for name in requested if name not in known_names]
                load_all = False

            chat_files: list[InputFile] = []
            if keyed and (load_all or wanted_names):
                chat_files = await fetch_chat_files(
                    runtime.client,
                    config,
                    identity.user_id,
                    identity.conversation_id,
                    agent_id=identity.agent_id,
                    skip_names=known_names,
                    skip_ids=imported_before,
                    names=None if load_all else wanted_names,
                )
                if chat_files:
                    logger.info("loaded %d file(s) into the sandbox on request", len(chat_files))
                    input_files.extend(chat_files)
            logger.info(
                "run_code user=%s conversation=%s keyed=%s container=%s",
                stable_hash(identity.user_id) if keyed else "anon",
                identity.conversation_id or "one-off",
                keyed,
                container_id or "fresh",
            )
            timeout = _clamp_timeout(config, timeout_s)
            exec_code = hydrate_loaded_names(code, [f.name for f in chat_files])
            try:
                result = await run(
                    runtime.client,
                    config,
                    code=exec_code,
                    task=task,
                    files=input_files,
                    container_id=container_id,
                    timeout_s=timeout,
                )
            except AzureError as exc:
                return _error(redact(str(exc), config.secrets()))

            replaced = bool(container_id) and result.container_id != container_id
            if replaced and keyed and chat_files:
                # Azure replaced the expired container, so the files it held are gone.
                # Re-import the same files into the new container and retry once.
                chat_files = await fetch_chat_files(
                    runtime.client,
                    config,
                    identity.user_id,
                    identity.conversation_id,
                    agent_id=identity.agent_id,
                    skip_names={f.name for f in base_files},
                    skip_ids=set(),
                    names=[f.name for f in chat_files],
                )
                if chat_files:
                    input_files = list(base_files) + chat_files
                    try:
                        result = await run(
                            runtime.client,
                            config,
                            code=exec_code,
                            task=task,
                            files=input_files,
                            container_id=result.container_id,
                            timeout_s=timeout,
                        )
                    except AzureError as exc:
                        return _error(redact(str(exc), config.secrets()))
                    replaced = False
            if key and result.container_id:
                runtime.sessions.put(key, result.container_id)
                if replaced:
                    # The old container expired: the new one holds only this call's files.
                    runtime.sessions.add_imported(key, set())
                runtime.sessions.add_imported(key, {f.source_id for f in chat_files if f.source_id})
            elif key:
                runtime.sessions.touch(key)

            user_hash = stable_hash(identity.user_id) if keyed else stable_hash("anonymous")
            conversation = stable_hash(identity.conversation_id) if keyed else "one-off"

            # Retrieve every code-generated file, not just the ones the model cited:
            # the model sometimes writes a file and prints its path without annotating it.
            generated: list[tuple[str, str, str]] = []  # (container_id, file_id, name)
            if result.container_id:
                try:
                    listed = await list_container_files(runtime.client, config, result.container_id)
                    generated = [
                        (result.container_id, f.file_id, Path(f.path).name or f.file_id)
                        for f in listed
                        if f.source == "assistant"
                    ]
                except AzureError:
                    generated = []
            if not generated:
                generated = [(c.container_id, c.file_id, c.filename) for c in result.citations]

            outputs: list[OutputFile] = []
            seen: set[str] = set()
            for container_id, file_id, name in generated:
                if file_id in seen:
                    continue
                seen.add(file_id)
                data = await runtime.store.download_container_file(
                    runtime.client, config, container_id, file_id
                )
                mime_type = _image_extension(name)
                artifact = None
                if config.store_artifacts:
                    artifact = runtime.store.save(
                        user_hash=user_hash,
                        conversation=conversation,
                        run_id=result.run_id or "run",
                        name=name,
                        data=data,
                        mime_type=mime_type,
                    )
                outputs.append(
                    OutputFile(
                        name=name,
                        mime_type=artifact.mime_type if artifact else (mime_type or "application/octet-stream"),
                        size=artifact.size if artifact else len(data),
                        data=data,
                        artifact=artifact,
                    )
                )

            if not keyed and result.container_id:
                await delete_container(runtime.client, config, result.container_id)

            return build_result(
                config,
                runtime.store,
                run_id=result.run_id,
                code=code,
                stdout=result.stdout,
                answer=result.answer,
                files=outputs,
                available=[
                    entry
                    for entry in catalog
                    if entry["name"] not in {o.name for o in outputs}
                    and entry["name"] not in known_names
                ],
            )
    finally:
        if lock:
            lock.release()


def _headers(ctx: Context | None) -> Any:
    try:
        if ctx is not None and ctx.request_context is not None and ctx.request_context.request is not None:
            return ctx.request_context.request.headers
    except Exception:
        pass
    return {}


@mcp.tool()
async def run_code(
    code: str,
    task: str | None = None,
    files: list[FileInput] | None = None,
    timeout_s: int = 0,
    attach_files: list[str] | str = "none",
    ctx: Context = None,  # noqa: B008 — injected by the MCP runtime
) -> CallToolResult:
    """Run Python code in an Azure-hosted sandbox.

    Args:
        code: Python source to run, verbatim (executor mode).
        task: Optional one-line context for the run.
        files: Optional input files (base64 `data`), visible to the code under /mnt/data.
        timeout_s: 0 for the default (180 s), clamped to the 600 s maximum.
        attach_files: names to copy into /mnt/data before running, or "all". Default "none":
            the result lists the available files so the model can request only what it needs.
    """
    runtime = await _get_runtime()
    return await handle_run(
        runtime,
        _headers(ctx),
        code=code,
        task=task,
        files=[f.model_dump() for f in files] if files else None,
        timeout_s=timeout_s,
        attach_files=attach_files,
    )


@mcp.custom_route("/health", methods=["GET"])
async def mcp_health(request: Request) -> Response:
    return JSONResponse({"status": "ok"})


def build_download_app(store: ArtifactStore, config: Config) -> Starlette:
    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def fetch(request: Request) -> Response:
        token = request.query_params.get("t", "")
        artifact_id = store.verify(token)
        if not artifact_id or artifact_id != request.path_params.get("artifact_id"):
            return Response("not found or expired", status_code=404)
        artifact = store.find(artifact_id)
        if artifact is None:
            return Response("not found", status_code=404)
        data = store._data_path(artifact).read_bytes()
        return Response(
            data,
            media_type=artifact.mime_type,
            headers={"Content-Disposition": f'attachment; filename="{artifact.name}"'},
        )

    return Starlette(routes=[Route("/health", health), Route("/f/{artifact_id}", fetch)])


async def _sweep_loop(runtime: Runtime, interval_s: int = 60) -> None:
    while True:
        await asyncio.sleep(interval_s)
        for key, container_id in runtime.sessions.idle_sessions():
            try:
                await delete_container(runtime.client, runtime.config, container_id)
            except Exception:
                pass
            runtime.sessions.drop(key)


async def _run_servers(config: Config) -> None:
    store = ArtifactStore(config)
    sessions = SessionRegistry(config)
    async with httpx.AsyncClient(timeout=config.default_timeout_s) as client:
        global _runtime
        _runtime = Runtime(
            config=config,
            client=client,
            store=store,
            sessions=sessions,
            semaphore=asyncio.Semaphore(config.max_concurrency),
        )
        sweep = asyncio.create_task(_sweep_loop(_runtime)) if config.store_artifacts else None
        try:
            mcp_app = mcp.streamable_http_app(streamable_http_path="/mcp", host=config.mcp_host)
            import uvicorn

            mcp_srv = uvicorn.Server(
                uvicorn.Config(mcp_app, host=config.mcp_host, port=config.mcp_port, log_level="info")
            )
            servers = [mcp_srv.serve()]
            if config.store_artifacts:
                download_app = build_download_app(store, config)
                dl_srv = uvicorn.Server(
                    uvicorn.Config(
                        download_app,
                        host=config.download_host,
                        port=config.download_port,
                        log_level="info",
                    )
                )
                servers.append(dl_srv.serve())
            await asyncio.gather(*servers)
        finally:
            if sweep is not None:
                sweep.cancel()
                try:
                    await sweep
                except asyncio.CancelledError:
                    pass


def serve() -> int:
    config = load(None)
    missing = [n for n in ("azure_api_key", "mcp_token") if not getattr(config, n)]
    if missing:
        print(f"missing required config: {missing}", file=__import__("sys").stderr)
        return 1
    asyncio.run(_run_servers(config))
    return 0
