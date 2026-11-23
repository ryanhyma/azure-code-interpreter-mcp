# Demo & verification — Azure Code Interpreter MCP

What was built and how it was proven, so a cold reader can reproduce or re-verify.

## Stack

- `azure-ci-mcp` — Python 3.12+ MCP server (streamable-http). MCP on `172.17.0.1:8935`
  (docker0 bridge, for LibreChat), downloads on `127.0.0.1:8936` (loopback, browser).
- systemd user unit `azure-ci-mcp.service`, env at `~/.config/azure-ci-mcp/env` (0600).
- LibreChat `librechat.yaml` gains an `azureci` server; an agent "Azure Code Interpreter"
  (provider `local27b`, tool `run_code_mcp_azureci`) is the chat entry point.

## Verified end-to-end

| Claim | Evidence |
|---|---|
| `run_code` exposed to LibreChat | `[MCP][azureci] Tools: run_code`, "4 servers, 41 tools" |
| Non-Azure model computes via sandbox | local 27B agent -> `run_code` -> Azure sandbox -> 42, 579, 1024, 42925 |
| One container per conversation | MCP log: same conversation -> `container=cntr_...` reused; new conversation -> `container=fresh` |
| Charts render natively | a `plot.png` run returned 2 PNG attachments in the response |
| Boundary (SC-14) | `tests/boundary.sh`: only bridge serves MCP, only loopback serves downloads, LAN refused |
| Secrets contained | key/token live only in the service env; error paths are redacted |

## Reproduce

```bash
cd ~/dev/azure-ci-mcp
.venv/bin/azure-ci-mcp doctor          # config + reachability
.venv/bin/pytest                        # 49 unit tests
systemctl --user restart azure-ci-mcp   # (deployment)
bash tests/boundary.sh                  # listeners + LAN refusal
```

A direct sandbox round-trip, without LibreChat:

```bash
AZCI_ENV_FILE=~/.config/azure-ci-mcp/env .venv/bin/python - <<'PY'
import asyncio, httpx
from azci_mcp.config import load
from azci_mcp import azure
async def main():
    async with httpx.AsyncClient(timeout=60) as c:
        r = await azure.run(c, load(None), code="print(7*6)")
    print(r.stdout, r.answer)
asyncio.run(main())
PY
```

## The LibreChat fork patch (non-image attachments)

**Validated on the full `v0.8.9` tag** (commit `d4e85d2`): patch applies clean, packages build,
`parsers.test.ts` 163/163 and `preflight.spec.ts` 14/14 pass, and a real v0.8.9 server returned
`v089.csv | text/csv | file_id ...` as a native attachment with a spreadsheet preview.
The file is `deploy/librechat-generic-mcp-artifact-bridge-v0.8.9.patch` (identical to the
generic patch, kept under a versioned name for the fork merge).

Images already render natively through LibreChat's existing MCP image path. For
**non-image** files (CSV/XLSX/PDF/...), apply the generic MCP artifact bridge:

```bash
git -C ~/dev/librechat apply ~/dev/azure-ci-mcp/deploy/librechat-generic-mcp-artifact-bridge.patch
```

What it does (two small hooks, server-agnostic):

1. `packages/api/src/mcp/parsers.ts` — any MCP tool returning
   `structuredContent.artifacts[]` (`{name, bytes(base64)}`) is forwarded into the tool
   result's artifact with `__librechat_file_authoring: true` + `files` (bytes as
   `preparedBuffer`).
2. `packages/api/src/files/code/preflight.ts` — honours a pre-supplied `preparedBuffer`
   instead of a Code API download.
3. `api/server/controllers/agents/callbacks.js` — the image branch no longer returns early
   when a code-artifact file list is present, so a run that writes a chart **and** a CSV
   delivers both instead of dropping the files.
4. `api/server/routes/azci.js` (mounted at `/api/azci`) — the internal chat-file bridge. Gated by
   `X-Azci-Token`; resolves the latest user message's `files`, then streams the bytes. The MCP
   calls it with the user id it already received, so a chat attachment lands in `/mnt/data`
   automatically (`attach_chat_files`, default true).

Then rebuild LibreChat (`npm run build:packages`, client build, docker image) and any
MCP server emitting `structuredContent.artifacts` gets native file attachments.

## Document round-trip (verified live)

- Attached `contract.docx`; `run_code` read it with python-docx, changed the liability cap to
  `$250,000`, and `contract-revised.docx` came back as a native attachment.
- A second turn asked for a 60-day termination; the model opened `contract-revised.docx` from
  `/mnt/data` in the **same container**, wrote `contract-v3.docx`, and both revisions appeared as
  attachments. No duplicate imports, no re-upload.
- Sandbox document libraries confirmed present: python-docx, openpyxl, pypdf, PyPDF2, python-pptx,
  pandas, matplotlib, pdfplumber, fitz.

## Test inventory

| Suite | Count |
|---|---|
| MCP unit tests (`pytest`) | 59 |
| LibreChat `azci` bridge route (jest) | 10 |
| LibreChat MCP files section + McpSection (jest) | 49 |
| LibreChat parsers + preflight (jest) | 175 |
| Playwright E2E `azci-on-demand` (real provider) | 1 |

## Agent-level files (add once, use everywhere)

Upload once to the agent's **File Context** (or File Search) and every MCP tool can read it; no
per-tool duplication. The agent id travels as `X-LibreChat-Agent-Id` (from `{{LIBRECHAT_BODY_AGENT_ID}}`)
and `/api/azci/files` unions the agent's `tool_resources.context` / `file_search` files with the
conversation's chat attachments.

Verified: a `contract.docx` added only to the agent's File Context appeared in `/mnt/data` for a
`run_code` call in a conversation with no attachments.

## On-demand files

`run_code` does **not** copy anything into `/mnt/data` by default. The result lists what is
available (MCP pool + chat attachments), and the model loads only what it needs:

```python
run_code(code="...", attach_files=["contract.docx"])   # just these
run_code(code="...", attach_files="all")               # everything
run_code(code="...")                                   # nothing; manifest only
```

Verified live: first turn reported an empty `/mnt/data` plus "one available DOCX (contract.docx)";
the follow-up with `attach_files` loaded it. Each container still imports a file only once.

## Hydrated file names

Azure stores an uploaded input as `/mnt/data/<hash>-<filename>`. After an on-demand load the server
prepends a tiny hydration block that copies it to `/mnt/data/<filename>`, so the model can open the
name the user asked for. The preamble is harness code and is not echoed in the reported `code`.

Playwright check (live): turn 1 listed `/mnt/data` empty + "contract.docx available"; turn 2 with
`attach_files` opened `/mnt/data/contract.docx` on the first attempt and printed all four paragraphs.

## Running the E2E

```bash
AZCI_E2E=1 npx playwright test -c e2e/playwright.azci.config.ts
```

It logs in as the test account, selects the agent, asserts turn 1 lists the file without loading it,
then turn 2 loads it with `attach_files` and prints its contents. Skipped unless `AZCI_E2E=1`;
override with `AZCI_AGENT_NAME`, `AZCI_FILE`, `AZCI_EXPECT`, `AZCI_USER`, `AZCI_PASSWORD`.
The config deliberately omits the default global setup because that setup deletes its user.

## Artifact storage toggle

`AZCI_STORE_ARTIFACTS` (default `true`) controls the local 7-day copy and signed links. With `false`
the server is pass-through: outputs go inline to LibreChat, the download listener on `127.0.0.1:8936`
is not started, and no fallback link exists.

## Known limitations

- Input `files` are inert in v1 (chat attachments are not auto-forwarded).
- Only the `gpt-6-luna` deployment; executor mode (the calling model writes the code).
- MCP artifact bytes are not run through LibreChat's PII inspection in the bridge path.
- Download links use `localhost` — fine on the host, needs a host name for remote browsers.
