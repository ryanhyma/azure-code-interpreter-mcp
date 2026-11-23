# Azure Code Interpreter MCP

An MCP server that gives **any** LibreChat model a code-execution tool backed by Azure's hosted
code interpreter (Responses API). One sandbox per conversation, files loaded on demand, and generated
files delivered as native LibreChat attachments.

The calling model writes the Python (executor mode); Azure's `gpt-6-luna` runs it in a sandboxed
container and returns stdout, charts and files. The Azure key and container ids never leave the
service.

## How it works

```text
LibreChat agent ──HTTP + X-Azci-Token──► MCP server ──HTTPS──► Azure OpenAI /responses
   user + chat id headers                  │                     code_interpreter container
                                           ├─ /mnt/data files (on demand)
                                           └─ artifacts + signed links
```

- **One container per `(user, conversation)`**, deleted after the 20-minute idle TTL. The model never
  sees a container id; Azure's own expiry is the backstop.
- **Only new outputs** from the current run are returned, so a later turn never re-attaches older files.
- **Files are on demand.** `run_code` lists what is available; nothing is copied until the model asks
  with `attach_files=[...]` (or `"all"`). Each file is imported once per container.
- **Executor mode.** The model supplies the code, and Azure runs it verbatim. Charts come back as MCP
  images; other files become native attachments through the LibreChat fork patch.
- **Isolation.** Bridge-only MCP listener; a shared secret gates tool calls; identity comes from
  LibreChat-injected headers, never from the model.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
```

## Configure

Write a `0600` env file (`deploy/env.example` is a template) and point the service at it with
`AZCI_ENV_FILE`. Required:

| Variable | Meaning |
|---|---|
| `AZURE_AI_KEY` | Azure OpenAI key for the `hymafoundry` resource |
| `AZCI_MCP_TOKEN` | Shared secret LibreChat sends as `X-Azci-Token` |

Optional: `AZCI_AZURE_BASE_URL`, `AZCI_MODEL`, `AZCI_MCP_HOST`, `AZCI_MCP_PORT`,
`AZCI_DOWNLOAD_HOST`, `AZCI_DOWNLOAD_PORT`, `AZCI_ARTIFACT_DIR`, `AZCI_RETENTION_DAYS`,
`AZCI_DEFAULT_TIMEOUT_S`, `AZCI_MAX_TIMEOUT_S`, `AZCI_MAX_CONCURRENCY`,
`AZCI_CONTAINER_IDLE_TTL_S`, `AZCI_LIBRECHAT_URL`, `AZCI_ATTACH_CHAT_FILES`,
`AZCI_STORE_ARTIFACTS` (default `true`; `false` = pass-through: inline bytes only, no local copy,
signed link or download listener).

## Run

```bash
.venv/bin/azure-ci-mcp doctor     # config + reachability
.venv/bin/azure-ci-mcp run        # one manual run
.venv/bin/azure-ci-mcp serve      # MCP streamable-http
```

`serve` binds MCP to `172.17.0.1:8935` (docker0 gateway, for LibreChat) and downloads to
`127.0.0.1:8936` (host browser only). Never bind `0.0.0.0`.

## Tool

`run_code(code, task=None, files=None, attach_files="none", timeout_s=0)`

- `code` — Python to run verbatim (required).
- `attach_files` — names to copy into `/mnt/data`, or `"all"`; default `"none"` so the result shows a
  manifest only. Loaded files are normalized to `/mnt/data/<name>`.
- Returns stdout, the answer, inline images, generated files, the remaining manifest, and usage.
- With `AZCI_STORE_ARTIFACTS=false`, outputs are only inlined (no `downloadUrl`, no `/f/` listener).

## LibreChat integration

The fork patch (`deploy/librechat-generic-mcp-artifact-bridge-v0.8.9.patch`) adds:

- a generic `structuredContent.artifacts` → native attachment bridge;
- `/api/azci/files` for chat attachments and the agent-level **MCP files** pool;
- an **MCP files** section in the Agent Builder (files added once, never injected into the prompt).

Deploy with the systemd unit in `deploy/`; see `docs/demo.md` for the full verification record.

## Tests

| Suite | Count |
|---|---|
| `pytest` (clients, sessions, artifacts, server, hydration) | 65 |
| LibreChat fork: parsers + preflight | 175 |
| LibreChat fork: `/api/azci` route | 10 |
| LibreChat fork: MCP files UI + McpSection | 49 |
| Playwright E2E (real provider) | 1 |

```bash
.venv/bin/pytest
```

## Docs

- `docs/demo.md` — end-to-end verification, deployment and the fork patch.
- `docs/code-interpreter-environment.md` — measured sandbox packages and supported formats.
- `devos/specs/2026-11-21-azure-code-interpreter-mcp/` — contract, plan and decisions.
