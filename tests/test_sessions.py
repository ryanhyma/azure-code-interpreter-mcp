"""Session registry: per-conversation container mapping, TTL idle, per-key locks."""

import time

from azci_mcp.config import Config
from azci_mcp.sessions import SessionRegistry


def registry() -> SessionRegistry:
    return SessionRegistry(Config(azure_api_key="k", mcp_token="t"))


async def test_put_get_drop_roundtrip():
    r = registry()
    key = r.key("u1", "c1")
    r.put(key, "cntr_1")
    assert r.get(key) == "cntr_1"
    r.drop(key)
    assert r.get(key) is None


async def test_idle_sessions_returns_idle_keys():
    r = registry()
    r.put(r.key("u1", "c1"), "cntr_1")
    r.put(r.key("u2", "c2"), "cntr_2")
    idle = r.idle_sessions(now=time.monotonic() + 2000)
    assert sorted(k for k, _ in idle) == [("u1", "c1"), ("u2", "c2")]


async def test_touch_keeps_session_out_of_idle():
    r = registry()
    key = r.key("u1", "c1")
    r.put(key, "cntr_1")
    r.touch(key)
    assert r.idle_sessions() == []


async def test_lock_for_is_shared_per_key_distinct_across_keys():
    r = registry()
    a1 = await r.lock_for(r.key("u", "c"))
    a2 = await r.lock_for(r.key("u", "c"))
    b = await r.lock_for(r.key("u", "other"))
    assert a1 is a2
    assert a1 is not b


async def test_drop_removes_lock_too():
    r = registry()
    key = r.key("u", "c")
    await r.lock_for(key)
    r.drop(key)
    fresh = await r.lock_for(key)
    assert fresh is not None
    assert r.count() == 0


async def test_imported_files_are_tracked_per_container():
    r = registry()
    key = r.key("u", "c")
    r.put(key, "cntr_1")
    r.add_imported(key, {"f1", "f2"})
    assert r.imported(key) == {"f1", "f2"}
    # Re-putting the same container keeps the set; a different one resets it.
    r.put(key, "cntr_1")
    assert r.imported(key) == {"f1", "f2"}
    r.put(key, "cntr_2")
    assert r.imported(key) == set()
