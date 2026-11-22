"""Chat-attachment bridge: list/fetch from LibreChat and include as sandbox input."""

import httpx

from azci_mcp.config import Config
from azci_mcp.librechat import fetch_chat_files, list_chat_files


def config(**overrides) -> Config:
    return Config(azure_api_key="k", mcp_token="tok", **overrides)


def transport(files: dict[str, bytes], list_status=200):
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((str(request.url), dict(request.headers)))
        if request.url.path == "/api/azci/files":
            if list_status != 200:
                return httpx.Response(list_status, json={"error": "no"})
            return httpx.Response(
                200,
                json={
                    "files": [
                        {"file_id": fid, "filename": name, "type": "text/csv", "bytes": len(data)}
                        for fid, (name, data) in files.items()
                    ]
                },
            )
        fid = request.url.path.rsplit("/", 1)[-1]
        if fid in files:
            return httpx.Response(200, content=files[fid][1])
        return httpx.Response(404)

    return calls, handler


async def test_list_chat_files_sends_token_and_user():
    calls, handler = transport({"f1": ("a.csv", b"x")})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    files = await list_chat_files(client, config(), "user-1", "conv-1")
    assert [f["filename"] for f in files] == ["a.csv"]
    url, headers = calls[0]
    assert "conversationId=conv-1" in url
    assert headers["x-azci-token"] == "tok"
    assert headers["x-librechat-user-id"] == "user-1"


async def test_list_chat_files_returns_empty_on_error():
    _calls, handler = transport({}, list_status=500)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await list_chat_files(client, config(), "u", "c") == []


async def test_fetch_chat_files_downloads_bytes():
    _calls, handler = transport({"f1": ("a.csv", b"name,score\nAda,10\n")})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    files = await fetch_chat_files(client, config(), "u", "c")
    assert len(files) == 1
    assert files[0].name == "a.csv"
    assert files[0].mime_type == "text/csv"
    assert files[0].data == b"name,score\nAda,10\n"


async def test_fetch_chat_files_caps_count():
    many = {f"f{i}": (f"f{i}.csv", b"x") for i in range(8)}
    _calls, handler = transport(many)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    files = await fetch_chat_files(client, config(input_max_files=3), "u", "c")
    assert len(files) == 3


async def test_fetch_chat_files_skips_named_duplicates():
    _calls, handler = transport({"f1": ("a.csv", b"x"), "f2": ("b.csv", b"y")})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    files = await fetch_chat_files(client, config(), "u", "c", skip_names={"a.csv"})
    assert [f.name for f in files] == ["b.csv"]


async def test_fetch_chat_files_skips_already_imported_ids():
    _calls, handler = transport({"f1": ("a.csv", b"x"), "f2": ("b.csv", b"y")})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    files = await fetch_chat_files(client, config(), "u", "c", skip_ids={"f1"})
    assert [f.name for f in files] == ["b.csv"]


async def test_agent_id_is_forwarded_to_the_bridge():
    calls, handler = transport({"f1": ("a.csv", b"x")})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await list_chat_files(client, config(), "u", "c", agent_id="agent-9")
    url, headers = calls[0]
    assert "agentId=agent-9" in url
    assert headers["x-librechat-agent-id"] == "agent-9"
