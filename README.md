# Azure Code Interpreter MCP

An MCP server that gives **any** LibreChat model a code-execution tool backed by
Azure's hosted code interpreter (Responses API), reusing one sandbox per
conversation and delivering generated files as native LibreChat attachments.

The caller's model writes the Python (executor mode); Azure's `gpt-6-luna`
runs it in a sandboxed container and returns stdout, charts and files. The
Azure key and container ids never leave this service.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
```

## Configure

Write a `0600` env file (`deploy/env.example` is a template) and point the
service at it with `AZCI_ENV_FILE`, or export the values directly. Required:

| Variable | Meaning |
|---|---|
| `AZURE_AI_KEY` | Azure OpenAI key for `hymafoundry` |
| `AZCI_MCP_TOKEN` | Shared secret LibreChat sends as `X-Azci-Token` |

Optional overrides: `AZCI_AZURE_BASE_URL`, `AZCI_MODEL`, `AZCI_MCP_HOST`,
`AZCI_MCP_PORT`, `AZCI_DOWNLOAD_HOST`, `AZCI_DOWNLOAD_PORT`,
`AZCI_ARTIFACT_DIR`, `AZCI_RETENTION_DAYS`, `AZCI_DEFAULT_TIMEOUT_S`,
`AZCI_MAX_TIMEOUT_S`, `AZCI_MAX_CONCURRENCY`, `AZCI_CONTAINER_IDLE_TTL_S`.

## Run

```bash
.venv/bin/azure-ci-mcp doctor          # verify config + reachability
.venv/bin/azure-ci-mcp run             # one task through the sandbox
.venv/bin/azure-ci-mcp serve           # MCP streamable-http
```

`serve` binds MCP to `172.17.0.1:8935` (docker0 gateway, for LibreChat) and
downloads to `127.0.0.1:8936` (host browser). Never bind `0.0.0.0`.

## Test

```bash
.venv/bin/pytest
```
