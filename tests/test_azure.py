"""Azure client: request building, response parsing, error mapping and retries."""

import json
from pathlib import Path

import httpx
import pytest

from azci_mcp.azure import (
    AzureError,
    Citation,
    InputFile,
    RunResult,
    build_request_body,
    parse_response,
    run,
    _is_container_gone,
    _error_message,
)
from azci_mcp.config import Config, DEFAULT_BASE_URL, DEFAULT_MODEL

FIXTURES = Path(__file__).parent / "fixtures"


def config(**overrides) -> Config:
    return Config(azure_api_key="test-key", mcp_token="test-token", **overrides)


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# --- request building ------------------------------------------------------

def test_build_request_body_executor_mode():
    body = build_request_body(config(), code="print(1)")
    assert body["model"] == DEFAULT_MODEL
    assert body["store"] is False
    assert body["include"] == ["code_interpreter_call.outputs"]
    assert body["tools"] == [{"type": "code_interpreter", "container": {"type": "auto"}}]
    input_text = body["input"][0]["content"][0]["text"]
    assert "print(1)" in input_text
    assert "exactly as written" in input_text


def test_build_request_body_reuses_container():
    body = build_request_body(config(), code="x", container_id="cntr_123")
    assert body["tools"][0]["container"] == "cntr_123"


def test_build_request_body_embeds_files_as_data_urls():
    files = [InputFile(name="data.csv", mime_type="text/csv", data=b"a,b\n1,2\n")]
    body = build_request_body(config(), code="read it", files=files)
    parts = body["input"][0]["content"]
    file_part = [p for p in parts if p["type"] == "input_file"][0]
    assert file_part["filename"] == "data.csv"
    assert file_part["file_data"].startswith("data:text/csv;base64,")


def test_input_file_data_url_is_base64():
    f = InputFile(name="x.txt", mime_type="text/plain", data=b"hello")
    assert f.data_url() == "data:text/plain;base64,aGVsbG8="


# --- parsing ---------------------------------------------------------------

def test_parse_response_logs_and_citation():
    result = parse_response(fixture("logs_and_citation.json"))
    assert result.run_id == "resp_sum"
    assert result.code == "print(sum(n**2 for n in range(1, 51)))\n"
    assert result.stdout == "42925\n"
    assert result.answer == "The sum of squares is 42925."
    assert result.citations == [Citation("cntr_abc", "cfile_csv", "squares.csv")]
    assert result.usage["total_tokens"] == 948
    assert result.no_code_run is False


def test_parse_response_no_code_run():
    result = parse_response(fixture("no_code.json"))
    assert result.answer == "I can answer without running code."
    assert result.no_code_run is True
    assert result.code == ""


def test_parse_response_image_citation():
    result = parse_response(fixture("image_citation.json"))
    assert result.citations[0].filename == "plot.png"
    assert result.stdout == "done\n"


# --- error mapping ---------------------------------------------------------

def test_error_message_maps_code():
    assert _error_message({"error": {"message": "boom", "code": "x"}}, 400) == "x: boom"
    assert _error_message(None, 500) == "Azure returned HTTP 500"


def test_is_container_gone_detects_404_not_found():
    assert _is_container_gone(404, {"error": {"message": "Container with id 'cntr_old' not found."}})
    assert not _is_container_gone(404, {"error": {"message": "Deployment not found"}})
    assert not _is_container_gone(400, {"error": {"message": "Container expired"}})


# --- retries (async, mock transport) --------------------------------------

def _response(status: int, body: dict, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers or {})


async def test_run_retries_once_on_429():
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return _response(429, {"error": {"message": "rate limited"}}, {"retry-after-ms": "1"})
        return _response(200, fixture("no_code.json"))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await run(client, config(), code="x")
    assert calls["n"] == 2
    assert result.no_code_run is True


async def test_run_recovers_from_expired_container():
    bodies = []

    async def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if bodies[-1]["tools"][0]["container"] == "cntr_stale":
            return _response(404, {"error": {"message": "Container with id 'cntr_stale' not found."}})
        return _response(200, fixture("no_code.json"))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await run(client, config(), code="x", container_id="cntr_stale")
    assert result.run_id == "resp_nocode"
    assert bodies[0]["tools"][0]["container"] == "cntr_stale"
    assert bodies[1]["tools"][0]["container"] == {"type": "auto"}


async def test_run_maps_401_without_key_in_message():
    async def handler(request: httpx.Request) -> httpx.Response:
        return _response(401, fixture("error_401.json"))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(AzureError) as exc:
        await run(client, config(), code="x")
    assert exc.value.status == 401
    assert "test-key" not in exc.value.message
    assert "incorrect api key" in exc.value.message.lower()
