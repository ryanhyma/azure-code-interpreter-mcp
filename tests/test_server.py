"""Server: tool registration, identity/auth, validation, result building, end-to-end runs."""

import json

import httpx
import pytest

from azci_mcp.artifacts import ArtifactStore
from azci_mcp.config import Config
from azci_mcp.sessions import SessionRegistry
from azci_mcp.server import (
    Runtime,
    authenticate,
    build_result,
    handle_run,
    mcp,
    validate_input,
)
from azci_mcp import server as server_mod


def config(token="tok", **overrides) -> Config:
    return Config(azure_api_key="k", mcp_token=token, **overrides)


def make_runtime(client, tmp_path, token="tok") -> Runtime:
    cfg = config(token, artifact_dir=tmp_path)
    return Runtime(
        config=cfg,
        client=client,
        store=ArtifactStore(cfg),
        sessions=SessionRegistry(cfg),
    )


def ok_response(container_id, run_id="resp_1", with_citation=True):
    annotations = []
    if with_citation:
        annotations = [
            {
                "type": "container_file_citation",
                "container_id": container_id,
                "file_id": "cfile_csv",
                "filename": "out.csv",
            }
        ]
    body = {
        "id": run_id,
        "status": "completed",
        "output": [
            {
                "type": "code_interpreter_call",
                "code": "print(42)",
                "container_id": container_id,
                "outputs": [{"type": "logs", "logs": "42\n"}],
            },
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": "42", "annotations": annotations}
                ],
            },
        ],
        "usage": {"total_tokens": 10},
    }
    return httpx.Response(200, json=body)


def make_handler():
    state = {"responses": [], "fresh": 0, "gets": [], "deletes": []}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = json.loads(request.content)
            state["responses"].append(body)
            container = body["tools"][0]["container"]
            if isinstance(container, str):
                cid = container
            else:
                state["fresh"] += 1
                cid = f"cntr_fresh_{state['fresh']}"
            return ok_response(cid)
        if request.method == "GET":
            state["gets"].append(str(request.url.path))
            if request.url.path.endswith("/files"):
                return httpx.Response(
                    200,
                    json={"object": "list", "data": [
                        {"id": "cfile_csv", "path": "/mnt/data/out.csv", "source": "assistant"}
                    ]},
                )
            return httpx.Response(200, content=b"a,b\n1,2\n")
        if request.method == "DELETE":
            state["deletes"].append(str(request.url.path))
            return httpx.Response(200, json={"deleted": True})
        return httpx.Response(404)

    return state, handler


# --- registration + schema -------------------------------------------------

async def test_run_code_tool_is_registered_with_required_code():
    tools = await mcp.list_tools()
    names = [t.name for t in tools]
    assert "run_code" in names
    tool = next(t for t in tools if t.name == "run_code")
    schema = tool.input_schema or {}
    assert "code" in schema.get("properties", {})
    assert "code" in schema.get("required", [])
    assert "ctx" not in schema.get("properties", {})


# --- identity + validation -------------------------------------------------

def test_authenticate_rejects_missing_and_wrong_token():
    assert authenticate({}, config()).allowed is False
    assert authenticate({"x-azci-token": "nope"}, config()).allowed is False


def test_authenticate_accepts_valid_token_and_identity():
    ident = authenticate(
        {"x-azci-token": "tok", "x-librechat-user-id": "u", "x-librechat-conversation-id": "c"},
        config(),
    )
    assert ident.allowed is True
    assert ident.user_id == "u"
    assert ident.conversation_id == "c"


def test_validate_input_rejects_empty_code():
    assert validate_input(config(), "", None, None) is not None


def test_validate_input_rejects_oversized_input():
    assert validate_input(config(), "x" * 8001, None, None) is not None


def test_validate_input_rejects_malformed_base64():
    assert validate_input(config(), "x", None, [{"name": "a.csv", "data": "!!not-base64!!"}]) is not None


def test_validate_input_rejects_too_many_files():
    files = [{"name": f"{i}.csv", "data": "YQ=="} for i in range(6)]
    assert validate_input(config(), "x", None, files) is not None


def test_validate_input_accepts_valid_file():
    assert validate_input(config(), "x", None, [{"name": "a.csv", "data": "YQ=="}]) is None


# --- result building -------------------------------------------------------

def test_build_result_includes_inline_image_and_artifact_entries(tmp_path):
    cfg = config(artifact_dir=tmp_path)
    store = ArtifactStore(cfg)
    # a small PNG and a CSV
    png = bytes.fromhex("89504e470d0a1a0a")  # PNG magic prefix; not a real image, but mime decides
    csv = b"a,b\n1,2\n"
    art_png = store.save(user_hash="u", conversation="c", run_id="r", name="plot.png", data=png, mime_type="image/png")
    art_csv = store.save(user_hash="u", conversation="c", run_id="r", name="out.csv", data=csv, mime_type="text/csv")
    result = build_result(cfg, store, run_id="resp", code="x", stdout="42\n", answer="42", artifacts=[art_png, art_csv])
    content_types = [c.type for c in result.content]
    assert "image" in content_types
    assert result.structured_content is not None
    names = [a["name"] for a in result.structured_content["artifacts"]]
    # The inlined image is not repeated in the artifact list (the image path stores it);
    # the non-image file still travels through the bridge.
    assert names == ["out.csv"]
    entry = next(a for a in result.structured_content["artifacts"] if a["name"] == "out.csv")
    assert entry["mimeType"] == "text/csv"
    assert "downloadUrl" in entry and "browserUrl" in entry


# --- end-to-end runs -------------------------------------------------------

async def test_handle_run_reuses_container_per_conversation(tmp_path):
    state, handler = make_handler()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rt = make_runtime(client, tmp_path)
    headers = {"x-azci-token": "tok", "x-librechat-user-id": "u1", "x-librechat-conversation-id": "c1"}
    await handle_run(rt, headers, code="print(1)")
    await handle_run(rt, headers, code="print(2)")
    assert state["responses"][0]["tools"][0]["container"] == {"type": "auto"}
    assert state["responses"][1]["tools"][0]["container"] == "cntr_fresh_1"
    assert rt.sessions.count() == 1


async def test_handle_run_different_users_never_share(tmp_path):
    state, handler = make_handler()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rt = make_runtime(client, tmp_path)
    h1 = {"x-azci-token": "tok", "x-librechat-user-id": "u1", "x-librechat-conversation-id": "c1"}
    h2 = {"x-azci-token": "tok", "x-librechat-user-id": "u2", "x-librechat-conversation-id": "c1"}
    await handle_run(rt, h1, code="print(1)")
    await handle_run(rt, h2, code="print(2)")
    assert state["responses"][1]["tools"][0]["container"] == {"type": "auto"}  # fresh for u2
    assert rt.sessions.count() == 2


async def test_handle_run_wrong_token_makes_no_azure_call(tmp_path):
    state, handler = make_handler()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rt = make_runtime(client, tmp_path)
    result = await handle_run(rt, {"x-azci-token": "wrong"}, code="print(1)")
    assert result.is_error is True
    assert state["responses"] == []


async def test_handle_run_invalid_input_makes_no_azure_call(tmp_path):
    state, handler = make_handler()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rt = make_runtime(client, tmp_path)
    result = await handle_run(rt, {"x-azci-token": "tok"}, code="")
    assert result.is_error is True
    assert state["responses"] == []


async def test_handle_run_attaches_chat_files_as_input(tmp_path):
    """A conversation's chat attachment is downloaded and sent to Azure as an input file."""
    seen = {"post": None}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/azci/files":
            return httpx.Response(200, json={"files": [
                {"file_id": "f1", "filename": "scores.csv", "type": "text/csv", "bytes": 5}
            ]})
        if request.url.path == "/api/azci/files/f1":
            return httpx.Response(200, content=b"a,b\n1,2\n")
        if request.method == "POST":
            seen["post"] = json.loads(request.content)
            body = seen["post"]
            cid = body["tools"][0]["container"]
            cid = cid if isinstance(cid, str) else "cntr_fresh_1"
            return ok_response(cid)
        if request.method == "GET":
            if request.url.path.endswith("/files"):
                return httpx.Response(200, json={"object": "list", "data": []})
            return httpx.Response(200, content=b"")
        return httpx.Response(200, json={"deleted": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rt = make_runtime(client, tmp_path)
    headers = {"x-azci-token": "tok", "x-librechat-user-id": "u1", "x-librechat-conversation-id": "c1"}
    await handle_run(rt, headers, code="print(open('/mnt/data/scores.csv').read())", attach_files="all")
    parts = seen["post"]["input"][0]["content"]
    file_parts = [p for p in parts if p["type"] == "input_file"]
    assert len(file_parts) == 1
    assert file_parts[0]["filename"] == "scores.csv"
    assert file_parts[0]["file_data"].startswith("data:text/csv;base64,")


async def test_agent_id_header_reaches_the_file_bridge(tmp_path):
    urls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/azci/files":
            urls.append(str(request.url))
            return httpx.Response(200, json={"files": []})
        if request.method == "POST":
            body = json.loads(request.content)
            cid = body["tools"][0]["container"]
            return ok_response(cid if isinstance(cid, str) else "cntr_fresh_1")
        if request.method == "GET":
            if request.url.path.endswith("/files"):
                return httpx.Response(200, json={"object": "list", "data": []})
            return httpx.Response(200, content=b"")
        return httpx.Response(200, json={"deleted": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rt = make_runtime(client, tmp_path)
    headers = {
        "x-azci-token": "tok",
        "x-librechat-user-id": "u1",
        "x-librechat-conversation-id": "c1",
        "x-librechat-agent-id": "agent-1",
    }
    await handle_run(rt, headers, code="print(1)")
    assert urls and "agentId=agent-1" in urls[0]


async def test_chat_files_are_not_reimported_into_a_live_container(tmp_path):
    """The same chat file is sent once per container, not on every call."""
    posts = []
    gets = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/azci/files":
            return httpx.Response(200, json={"files": [
                {"file_id": "f1", "filename": "scores.csv", "type": "text/csv", "bytes": 12}
            ]})
        if request.url.path.startswith("/api/azci/files/"):
            return httpx.Response(200, content=b"a,b\n1,2\n")
        if request.method == "POST":
            body = json.loads(request.content)
            posts.append(body)
            cid = body["tools"][0]["container"]
            cid = cid if isinstance(cid, str) else "cntr_fresh_1"
            return ok_response(cid)
        if request.method == "GET":
            if request.url.path.endswith("/files"):
                return httpx.Response(200, json={"object": "list", "data": []})
            return httpx.Response(200, content=b"")
        return httpx.Response(200, json={"deleted": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rt = make_runtime(client, tmp_path)
    headers = {"x-azci-token": "tok", "x-librechat-user-id": "u1", "x-librechat-conversation-id": "c1"}

    await handle_run(rt, headers, code="print(1)", attach_files="all")
    await handle_run(rt, headers, code="print(2)", attach_files="all")

    def input_files(body):
        return [p for p in body["input"][0]["content"] if p["type"] == "input_file"]

    assert len(input_files(posts[0])) == 1
    assert input_files(posts[1]) == []


async def test_chat_files_are_reimported_after_container_replacement(tmp_path):
    """If Azure reports a different container (expiry), the files are sent again."""
    posts = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/azci/files":
            return httpx.Response(200, json={"files": [
                {"file_id": "f1", "filename": "scores.csv", "type": "text/csv", "bytes": 12}
            ]})
        if request.url.path.startswith("/api/azci/files/"):
            return httpx.Response(200, content=b"a,b\n1,2\n")
        if request.method == "POST":
            body = json.loads(request.content)
            posts.append(body)
            container = body["tools"][0]["container"]
            # Azure answers with a different container than the expired id it was given.
            return ok_response("cntr_replacement" if container == {"type": "auto"} else "cntr_second")
        if request.method == "GET":
            if request.url.path.endswith("/files"):
                return httpx.Response(200, json={"object": "list", "data": []})
            return httpx.Response(200, content=b"")
        return httpx.Response(200, json={"deleted": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rt = make_runtime(client, tmp_path)
    headers = {"x-azci-token": "tok", "x-librechat-user-id": "u1", "x-librechat-conversation-id": "c1"}

    await handle_run(rt, headers, code="print(1)", attach_files="all")
    await handle_run(rt, headers, code="print(2)", attach_files="all")

    def input_files(body):
        return [p for p in body["input"][0]["content"] if p["type"] == "input_file"]

    assert len(input_files(posts[0])) == 1
    # Second call requested "all" but the file was already in the container, so nothing was
    # sent; Azure then reported the container replaced, which empties the import set.
    assert len(posts) == 2
    assert input_files(posts[1]) == []


async def test_files_are_on_demand_by_default(tmp_path):
    """Nothing is imported unless requested; the manifest names what is available."""
    posts = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/azci/files":
            return httpx.Response(200, json={"files": [
                {"file_id": "f1", "filename": "scores.csv", "type": "text/csv", "bytes": 12}
            ]})
        if request.url.path.startswith("/api/azci/files/"):
            return httpx.Response(200, content=b"a,b\n1,2\n")
        if request.method == "POST":
            body = json.loads(request.content)
            posts.append(body)
            cid = body["tools"][0]["container"]
            return ok_response(cid if isinstance(cid, str) else "cntr_fresh_1")
        if request.method == "GET":
            if request.url.path.endswith("/files"):
                return httpx.Response(200, json={"object": "list", "data": []})
            return httpx.Response(200, content=b"")
        return httpx.Response(200, json={"deleted": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rt = make_runtime(client, tmp_path)
    headers = {"x-azci-token": "tok", "x-librechat-user-id": "u1", "x-librechat-conversation-id": "c1"}
    result = await handle_run(rt, headers, code="print(1)")

    body = posts[0]
    assert [p for p in body["input"][0]["content"] if p["type"] == "input_file"] == []
    text = next(c.text for c in result.content if hasattr(c, "text"))
    assert "available files" in text and "scores.csv" in text


async def test_attach_files_by_name_loads_only_that_file(tmp_path):
    posts = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/azci/files":
            return httpx.Response(200, json={"files": [
                {"file_id": "f1", "filename": "a.csv", "type": "text/csv", "bytes": 1},
                {"file_id": "f2", "filename": "b.csv", "type": "text/csv", "bytes": 1},
            ]})
        if request.url.path.startswith("/api/azci/files/"):
            return httpx.Response(200, content=b"x")
        if request.method == "POST":
            body = json.loads(request.content)
            posts.append(body)
            cid = body["tools"][0]["container"]
            return ok_response(cid if isinstance(cid, str) else "cntr_fresh_1")
        if request.method == "GET":
            if request.url.path.endswith("/files"):
                return httpx.Response(200, json={"object": "list", "data": []})
            return httpx.Response(200, content=b"")
        return httpx.Response(200, json={"deleted": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rt = make_runtime(client, tmp_path)
    headers = {"x-azci-token": "tok", "x-librechat-user-id": "u1", "x-librechat-conversation-id": "c1"}
    await handle_run(rt, headers, code="print(1)", attach_files=["b.csv"])

    files = [p for p in posts[0]["input"][0]["content"] if p["type"] == "input_file"]
    assert [f["filename"] for f in files] == ["b.csv"]


async def test_handle_run_anonymous_deletes_container(tmp_path):
    state, handler = make_handler()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rt = make_runtime(client, tmp_path)
    result = await handle_run(rt, {"x-azci-token": "tok"}, code="print(1)")
    assert result.is_error is False
    assert state["deletes"] != []
    assert rt.sessions.count() == 0


async def test_handle_run_retrieves_file_via_listing_without_citation(tmp_path):
    """The model sometimes writes a file but does not cite it; the listing must still deliver it."""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = json.loads(request.content)
            cid = body["tools"][0]["container"] if isinstance(body["tools"][0]["container"], str) else "cntr_fresh_1"
            return ok_response(cid, with_citation=False)
        if request.method == "GET":
            if request.url.path.endswith("/files"):
                return httpx.Response(200, json={"object": "list", "data": [
                    {"id": "cfile_x", "path": "/mnt/data/report.csv", "source": "assistant"}
                ]})
            return httpx.Response(200, content=b"a,b\n1,2\n")
        if request.method == "DELETE":
            return httpx.Response(200, json={"deleted": True})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rt = make_runtime(client, tmp_path)
    result = await handle_run(rt, {"x-azci-token": "tok"}, code="print(1)")
    arts = (result.structured_content or {}).get("artifacts", [])
    assert [a["name"] for a in arts] == ["report.csv"]
    assert arts[0]["mimeType"] == "text/csv"


def test_hydrate_loaded_names_copies_hash_prefixed_files():
    from azci_mcp.server import hydrate_loaded_names

    out = hydrate_loaded_names("print(open('/mnt/data/a.docx').read())", ["a.docx"])
    assert "glob('/mnt/data/*-' + _azci_name)" in out
    assert out.endswith("print(open('/mnt/data/a.docx').read())")


def test_hydrate_is_a_noop_without_names():
    from azci_mcp.server import hydrate_loaded_names

    assert hydrate_loaded_names("print(1)", []) == "print(1)"
