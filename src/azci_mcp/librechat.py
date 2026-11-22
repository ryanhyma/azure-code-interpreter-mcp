"""Pull a conversation's chat attachments from LibreChat's internal bridge route.

LibreChat stores uploaded files without a conversation id; the link lives on the
latest user message's `files` array. The fork route `/api/azci/files` resolves
that server-side and is gated by the shared MCP token plus the user id the MCP
already received in its own headers.
"""

from __future__ import annotations

import logging

import httpx

from .azure import InputFile
from .config import Config

logger = logging.getLogger("azci_mcp")


def _headers(config: Config, user_id: str, agent_id: str = "") -> dict[str, str]:
    headers = {
        "X-Azci-Token": config.mcp_token,
        "X-LibreChat-User-Id": user_id,
        "Accept": "application/json",
    }
    if agent_id:
        headers["X-LibreChat-Agent-Id"] = agent_id
    return headers


async def list_chat_files(
    client: httpx.AsyncClient,
    config: Config,
    user_id: str,
    conversation_id: str,
    agent_id: str = "",
) -> list[dict]:
    url = f"{config.librechat_url.rstrip('/')}/api/azci/files"
    try:
        response = await client.get(
            url,
            params={"conversationId": conversation_id, "agentId": agent_id} if agent_id else {"conversationId": conversation_id},
            headers=_headers(config, user_id, agent_id),
            timeout=15.0,
        )
    except httpx.RequestError as exc:
        logger.warning("chat file list failed: %s", exc.__class__.__name__)
        return []
    if response.status_code != 200:
        logger.warning("chat file list returned HTTP %s", response.status_code)
        return []
    return response.json().get("files") or []


async def available_files(
    client: httpx.AsyncClient,
    config: Config,
    user_id: str,
    conversation_id: str,
    agent_id: str = "",
) -> list[dict]:
    """List the files a call could load, without downloading any bytes."""
    entries = await list_chat_files(client, config, user_id, conversation_id, agent_id)
    return [
        {
            "name": entry.get("filename") or entry.get("file_id"),
            "file_id": entry.get("file_id"),
            "mime_type": entry.get("type") or "application/octet-stream",
            "bytes": entry.get("bytes"),
        }
        for entry in entries
        if entry.get("file_id") and (entry.get("filename") or entry.get("file_id"))
    ]


async def fetch_chat_files(
    client: httpx.AsyncClient,
    config: Config,
    user_id: str,
    conversation_id: str,
    *,
    agent_id: str = "",
    skip_names: set[str] | None = None,
    skip_ids: set[str] | None = None,
    names: list[str] | None = None,
) -> list[InputFile]:
    """Download the conversation's attachments, bounded by the input limits."""
    listed = await list_chat_files(client, config, user_id, conversation_id, agent_id)
    files: list[InputFile] = []
    used = 0
    skip = skip_names or set()
    already = skip_ids or set()
    wanted = set(names) if names is not None else None
    for entry in listed:
        if len(files) >= config.input_max_files:
            break
        name = entry.get("filename") or entry.get("file_id")
        file_id = entry.get("file_id")
        if not name or not file_id or name in skip or file_id in already:
            continue
        if wanted is not None and name not in wanted:
            continue
        url = f"{config.librechat_url.rstrip('/')}/api/azci/files/{file_id}"
        try:
            response = await client.get(
                url,
                params={"conversationId": conversation_id, "agentId": agent_id} if agent_id else {"conversationId": conversation_id},
                headers=_headers(config, user_id, agent_id),
                timeout=30.0,
            )
        except httpx.RequestError as exc:
            logger.warning("chat file fetch failed for %s: %s", name, exc.__class__.__name__)
            continue
        if response.status_code != 200 or not response.content:
            logger.warning("chat file fetch for %s returned HTTP %s", name, response.status_code)
            continue
        data = response.content
        if used + len(data) > config.input_max_file_bytes * config.input_max_files:
            break
        used += len(data)
        files.append(
            InputFile(
                name=name,
                mime_type=entry.get("type") or "application/octet-stream",
                data=data,
                source_id=file_id,
            )
        )
    return files
