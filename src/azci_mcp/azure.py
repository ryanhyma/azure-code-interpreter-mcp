"""Azure Responses API client for the hosted code interpreter.

Executor mode: the caller's model writes the Python; this module sends it to
Azure under a "run this exact code" instruction and parses the sandbox stdout,
the assistant answer, file citations and token usage.

All error surfaces are redacted here: status and a safe message, never the key
or request headers.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field

import httpx

from .config import Config, redact

EXECUTOR_INSTRUCTION = (
    "Run the following Python code exactly as written. Do not modify it. "
    "Print nothing except the code's own output."
)


@dataclass(frozen=True)
class InputFile:
    name: str
    mime_type: str
    data: bytes  # raw bytes; encoded as a data URL when sent
    source_id: str = ""  # chat file id, used only for per-container dedupe

    def data_url(self) -> str:
        b64 = base64.b64encode(self.data).decode()
        return f"data:{self.mime_type};base64,{b64}"


@dataclass(frozen=True)
class Citation:
    container_id: str
    file_id: str
    filename: str


@dataclass
class RunResult:
    run_id: str = ""
    status: str = ""
    code: str = ""
    stdout: str = ""
    answer: str = ""
    container_id: str = ""
    citations: list[Citation] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    no_code_run: bool = False


class AzureError(Exception):
    """An Azure failure that should not be retried again (or was already retried)."""

    def __init__(self, status: int, message: str, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


def _encode_input_files(files: list[InputFile]) -> list[dict]:
    return [
        {"type": "input_file", "filename": f.name, "file_data": f.data_url()}
        for f in files
    ]


def build_request_body(
    config: Config,
    *,
    code: str,
    task: str | None = None,
    files: list[InputFile] | None = None,
    container_id: str | None = None,
) -> dict:
    """One Responses request in executor mode. No secrets: the key travels in headers."""
    parts: list[dict] = []
    text = EXECUTOR_INSTRUCTION
    if task:
        text = f"{task}\n\n{text}"
    text = f"{text}\n\n```python\n{code}\n```"
    parts.append({"type": "input_text", "text": text})
    if files:
        parts.extend(_encode_input_files(files))

    container: object = {"type": "auto"} if not container_id else container_id
    return {
        "model": config.model,
        "input": [{"role": "user", "content": parts}],
        "tools": [{"type": "code_interpreter", "container": container}],
        "include": ["code_interpreter_call.outputs"],
        "store": False,
    }


def parse_response(data: dict) -> RunResult:
    """Pull code, logs, answer and citations out of a Responses payload."""
    result = RunResult(
        run_id=data.get("id", ""),
        status=data.get("status", ""),
        usage=data.get("usage") or {},
    )
    output = data.get("output") or []
    for item in output:
        if item.get("type") == "code_interpreter_call":
            result.code = item.get("code") or ""
            result.container_id = item.get("container_id") or ""
            for out in item.get("outputs") or []:
                if out.get("type") == "logs":
                    result.stdout += out.get("logs") or ""
        elif item.get("type") == "message":
            for block in item.get("content") or []:
                if block.get("type") == "output_text":
                    result.answer = block.get("text") or ""
                for ann in block.get("annotations") or []:
                    if ann.get("type") == "container_file_citation":
                        result.citations.append(
                            Citation(
                                container_id=ann.get("container_id", ""),
                                file_id=ann.get("file_id", ""),
                                filename=ann.get("filename", ""),
                            )
                        )
    result.no_code_run = not result.code and not result.citations and not result.stdout
    return result


def _error_message(data: dict | None, status: int) -> str:
    err = (data or {}).get("error") or {}
    message = err.get("message") or f"Azure returned HTTP {status}"
    code = err.get("code") or err.get("type")
    if code:
        message = f"{code}: {message}"
    return message


def _is_container_gone(status: int, body: dict | None) -> bool:
    if status != 404:
        return False
    message = _error_message(body, status).lower()
    return "container" in message and ("not found" in message or "expired" in message)


def _retry_delay_ms(headers: httpx.Headers) -> float:
    raw = headers.get("retry-after-ms") or headers.get("retry-after")
    if not raw:
        return 1.0
    try:
        return float(raw) / 1000.0
    except ValueError:
        return 1.0


async def execute(client: httpx.AsyncClient, config: Config, body: dict) -> dict:
    """POST /responses once, mapping non-2xx into an AzureError with a redacted message."""
    secrets = config.secrets()
    url = f"{config.azure_base_url.rstrip('/')}/responses"
    headers = {"Authorization": f"Bearer {config.azure_api_key}", "Content-Type": "application/json"}
    response = await client.post(url, json=body, headers=headers)
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if response.status_code >= 400:
        raise AzureError(
            response.status_code,
            redact(_error_message(payload, response.status_code), secrets),
        )
    return payload


async def run(
    client: httpx.AsyncClient,
    config: Config,
    *,
    code: str,
    task: str | None = None,
    files: list[InputFile] | None = None,
    container_id: str | None = None,
    timeout_s: float | None = None,
) -> RunResult:
    """Run one code snippet with a single retry on 429/5xx and container-expiry recovery."""
    secrets = config.secrets()
    body = build_request_body(config, code=code, task=task, files=files, container_id=container_id)
    attempt = 0
    while True:
        attempt += 1
        try:
            request = client.build_request(
                "POST",
                f"{config.azure_base_url.rstrip('/')}/responses",
                json=body,
                headers={"Authorization": f"Bearer {config.azure_api_key}"},
                timeout=timeout_s or config.default_timeout_s,
            )
            response = await client.send(request)
            payload = None
            try:
                payload = response.json()
            except ValueError:
                pass

            status = response.status_code
            if status >= 400:
                if attempt == 1 and (status == 429 or status >= 500):
                    await _sleep(_retry_delay_ms(response.headers))
                    continue
                if attempt == 1 and _is_container_gone(status, payload):
                    body = build_request_body(
                        config, code=code, task=task, files=files, container_id=None
                    )
                    continue
                raise AzureError(status, redact(_error_message(payload, status), secrets))
            return parse_response(payload)
        except httpx.TimeoutException:
            raise AzureError(0, "Azure request timed out")
        except httpx.RequestError as exc:
            raise AzureError(0, redact(f"Azure request failed: {exc.__class__.__name__}", secrets))


@dataclass(frozen=True)
class ContainerFile:
    file_id: str
    path: str
    source: str


async def list_container_files(
    client: httpx.AsyncClient, config: Config, container_id: str
) -> list[ContainerFile]:
    """List every file in a container. Code-generated files carry source="assistant"."""
    url = f"{config.azure_base_url.rstrip('/')}/containers/{container_id}/files"
    headers = {"Authorization": f"Bearer {config.azure_api_key}"}
    response = await client.get(url, headers=headers, timeout=config.default_timeout_s)
    if response.status_code != 200:
        raise AzureError(
            response.status_code,
            redact(f"failed to list container files: HTTP {response.status_code}", config.secrets()),
        )
    data = response.json()
    return [
        ContainerFile(
            file_id=item.get("id", ""),
            path=item.get("path", ""),
            source=item.get("source", ""),
        )
        for item in (data or {}).get("data", [])
    ]


async def delete_container(client: httpx.AsyncClient, config: Config, container_id: str) -> bool:
    """Delete a container (best-effort). True when it is gone or already deleted."""
    url = f"{config.azure_base_url.rstrip('/')}/containers/{container_id}"
    headers = {"Authorization": f"Bearer {config.azure_api_key}"}
    response = await client.delete(url, headers=headers, timeout=30.0)
    return response.status_code < 400 or response.status_code == 404


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
