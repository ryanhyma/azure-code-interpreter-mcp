"""Per-conversation container sessions and per-conversation serialization."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from .config import Config


@dataclass
class Session:
    container_id: str
    last_active: float
    imported_files: set[str] = field(default_factory=set)


Key = tuple[str, str]


class SessionRegistry:
    """Maps (user id, conversation id) -> container id, server-side only.

    The model never sees a container id. Idle sessions are returned by
    :meth:`idle_sessions` so the caller can delete the Azure container and drop
    the mapping; a failed delete never keeps the session alive, because Azure's
    20-minute idle expiry discards the container regardless.
    """

    def __init__(self, config: Config):
        self._sessions: dict[Key, Session] = {}
        self._locks: dict[Key, asyncio.Lock] = {}
        self._guard = asyncio.Lock()
        self.idle_ttl_s = config.container_idle_ttl_s

    @staticmethod
    def key(user_id: str, conversation_id: str) -> Key:
        return (user_id, conversation_id)

    async def lock_for(self, key: Key) -> asyncio.Lock:
        async with self._guard:
            return self._locks.setdefault(key, asyncio.Lock())

    def get(self, key: Key) -> str | None:
        session = self._sessions.get(key)
        return session.container_id if session else None

    def put(self, key: Key, container_id: str) -> None:
        existing = self._sessions.get(key)
        if existing and existing.container_id == container_id:
            existing.last_active = time.monotonic()
            return
        # A different container starts with an empty import set.
        self._sessions[key] = Session(container_id=container_id, last_active=time.monotonic())

    def imported(self, key: Key) -> set[str]:
        session = self._sessions.get(key)
        return set(session.imported_files) if session else set()

    def add_imported(self, key: Key, file_ids: set[str]) -> None:
        session = self._sessions.get(key)
        if session:
            session.imported_files.update(file_ids)

    def touch(self, key: Key) -> None:
        session = self._sessions.get(key)
        if session:
            session.last_active = time.monotonic()

    def idle_sessions(self, now: float | None = None) -> list[tuple[Key, str]]:
        now = now if now is not None else time.monotonic()
        idle: list[tuple[Key, str]] = []
        for key, session in list(self._sessions.items()):
            if now - session.last_active > self.idle_ttl_s:
                idle.append((key, session.container_id))
        return idle

    def drop(self, key: Key) -> None:
        self._sessions.pop(key, None)
        self._locks.pop(key, None)

    def count(self) -> int:
        return len(self._sessions)
