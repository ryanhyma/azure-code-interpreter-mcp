"""Artifact storage: save/find, signed tokens, size caps, retention sweep, retrieval."""

import os
import time

import httpx
import pytest

from azci_mcp.artifacts import ArtifactStore, stable_hash
from azci_mcp.azure import AzureError
from azci_mcp.config import Config


def store(**overrides) -> ArtifactStore:
    return ArtifactStore(Config(azure_api_key="k", mcp_token="t", **overrides))


def test_save_and_find_roundtrip(tmp_path):
    s = ArtifactStore(Config(azure_api_key="k", mcp_token="t", artifact_dir=tmp_path))
    art = s.save(user_hash="u", conversation="c", run_id="r", name="squares.csv", data=b"a,b\n1,2\n")
    assert art.size == 8
    assert art.mime_type == "text/csv"
    found = s.find(art.id)
    assert found is not None
    assert found.name == "squares.csv"
    assert found.user_hash == "u"
    assert found.conversation == "c"


def test_save_enforces_size_cap(tmp_path):
    s = ArtifactStore(Config(azure_api_key="k", mcp_token="t", artifact_dir=tmp_path, output_max_file_bytes=10))
    with pytest.raises(ValueError):
        s.save(user_hash="u", conversation="c", run_id="r", name="big.bin", data=b"x" * 11)


def test_count_in_run_counts_only_data_files(tmp_path):
    s = ArtifactStore(Config(azure_api_key="k", mcp_token="t", artifact_dir=tmp_path))
    s.save(user_hash="u", conversation="c", run_id="r", name="a.txt", data=b"1")
    s.save(user_hash="u", conversation="c", run_id="r", name="b.txt", data=b"2")
    assert s.count_in_run("u", "c", "r") == 2


def test_token_roundtrip_and_tamper(tmp_path):
    s = ArtifactStore(Config(azure_api_key="k", mcp_token="t", artifact_dir=tmp_path))
    art = s.save(user_hash="u", conversation="c", run_id="r", name="a.txt", data=b"1")
    token = s.token(art.id)
    assert s.verify(token) == art.id
    # tamper with the mac
    parts = token.split(".")
    bad = f"{parts[0]}.{parts[1]}.{'0' * 64}"
    assert s.verify(bad) is None


def test_token_expires(tmp_path):
    s = ArtifactStore(Config(azure_api_key="k", mcp_token="t", artifact_dir=tmp_path))
    art = s.save(user_hash="u", conversation="c", run_id="r", name="a.txt", data=b"1")
    expired = s.token(art.id, ttl_s=-10)
    assert s.verify(expired) is None


def test_find_refuses_non_hex_ids(tmp_path):
    s = ArtifactStore(Config(azure_api_key="k", mcp_token="t", artifact_dir=tmp_path))
    assert s.find("../../etc/passwd") is None
    assert s.find("abc") is None


def test_sweep_removes_old_artifacts(tmp_path):
    s = ArtifactStore(Config(azure_api_key="k", mcp_token="t", artifact_dir=tmp_path, retention_days=7))
    art = s.save(user_hash="u", conversation="c", run_id="r", name="old.txt", data=b"x")
    # backdate the data + meta files beyond retention
    old = time.time() - (8 * 86400)
    for p in [s._data_path(art), s._meta_path(art)]:
        os.utime(p, (old, old))
    assert s.sweep() >= 1
    assert s.find(art.id) is None


def test_stable_hash_deterministic_and_distinct():
    assert stable_hash("user-1") == stable_hash("user-1")
    assert stable_hash("user-1") != stable_hash("user-2")
    assert len(stable_hash("x")) == 24


async def test_download_container_file_returns_bytes():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"a,b\n1,2\n")

    s = ArtifactStore(Config(azure_api_key="k", mcp_token="t"))
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    data = await s.download_container_file(client, Config(azure_api_key="k", mcp_token="t"), "cntr", "file")
    assert data == b"a,b\n1,2\n"


async def test_download_container_file_raises_on_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "gone"}})

    s = ArtifactStore(Config(azure_api_key="k", mcp_token="t"))
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(AzureError) as exc:
        await s.download_container_file(client, Config(azure_api_key="k", mcp_token="t"), "cntr", "file")
    assert exc.value.status == 404
