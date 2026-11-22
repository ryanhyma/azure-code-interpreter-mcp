---
devos-contract: 1
---

# Azure Code Interpreter MCP

## Goal

Give any LibreChat model a code-execution tool that runs Python in an Azure-hosted sandbox through
the Responses API, reusing one sandbox per conversation, and delivering real stdout, charts and
generated files as native LibreChat attachments without exposing the Azure key or container ids.

## Scenarios

- Scenario SC-1: successful run / WHEN a tool caller asks for a computation / THEN the result carries the executed code, the sandbox stdout, the assistant answer, a run id and token usage.
- Scenario SC-2: chart returned inline / WHEN the executed code writes a PNG chart / THEN the tool result includes that image and LibreChat renders it in the chat.
- Scenario SC-3: generated file arrives as an attachment / WHEN the executed code writes a file / THEN LibreChat attaches it to the assistant message, its download serves the exact bytes, and the MCP signed link remains a working fallback that refuses a tampered or expired token.
- Scenario SC-4: one sandbox per conversation / WHEN two calls arrive for the same user and conversation / THEN both run in the same container and the second sees the first's files; a different conversation or user gets a different container.
- Scenario SC-5: input files / WHEN a caller supplies files with the task / THEN the executed code finds them under `/mnt/data` and the result reports which files were attached.
- Scenario SC-6: no code executed / WHEN the Azure model answers without invoking the code interpreter / THEN the tool reports `no_code_run` with the answer and no fabricated logs.
- Scenario SC-7: Azure failure / WHEN Azure returns 401, 403, 429 or 5xx / THEN the tool returns a readable error naming the status, retries a 429/5xx once, and never includes the key or request headers.
- Scenario SC-8: execution timeout / WHEN a run exceeds the caller's timeout / THEN the tool returns a timeout result carrying the run id, stops waiting, and leaves the conversation's container usable for the next call.
- Scenario SC-9: concurrency / WHEN calls overlap / THEN two calls in one conversation are serialized in order, while calls in different conversations run independently and never share files.
- Scenario SC-10: invalid input / WHEN the task is empty, over 8000 characters, or files exceed 5 files / 10 MB / malformed base64 / THEN validation fails before any Azure call and no cost is incurred.
- Scenario SC-11: artifact retention / WHEN an artifact is older than 7 days / THEN it is deleted and its download URL answers 404.
- Scenario SC-12: LibreChat discovery / WHEN LibreChat starts with the server configured / THEN `run_code` is listed for the `azureci` server, no OAuth flow is triggered, and the address is accepted by the MCP allowlist.
- Scenario SC-13: non-Azure model uses the tool / WHEN a LibreChat agent backed by the local 27B model calls `run_code` / THEN the chat answer contains the real computed value from Azure.
- Scenario SC-14: network boundary / WHEN the host's LAN address and `0.0.0.0` are probed / THEN neither answers; only the docker0 bridge address serves MCP and only loopback serves downloads.
- Scenario SC-15: secret containment / WHEN server logs, tool results and error bodies are inspected / THEN the Azure key and the shared MCP token never appear.
- Scenario SC-16: LibreChat is current / WHEN the environment is verified / THEN the running image equals the registry's current `latest`, containers are healthy, and the Azure endpoint still lists `gpt-6-luna`.
- Scenario SC-17: identity headers / WHEN a tool call has a missing, foreign or wrong-token identity header / THEN it gets a one-off container or is refused, and can never reach another user's or conversation's container.
- Scenario SC-18: expired container recovery / WHEN Azure reports the mapped container expired or missing / THEN the server creates a replacement container, retries once, and the call succeeds.
- Scenario SC-19: attachment bridge failure / WHEN LibreChat cannot fetch or store a generated file / THEN the run still succeeds, the file remains downloadable from the MCP link, and the failure is reported without failing the chat turn.
- Scenario SC-20: chat file into the sandbox / WHEN a conversation has a user-attached file and `run_code` runs / THEN the file is in `/mnt/data` before the code executes.
- Scenario SC-21: one import per container / WHEN several `run_code` calls run in one live container / THEN each chat file is imported once and later calls reuse it, with no duplicates.
- Scenario SC-22: attachment survives a follow-up without one / WHEN the latest user message has no attachment but an earlier one does / THEN the earlier file is still imported.
- Scenario SC-23: re-import after expiry / WHEN Azure replaces an expired container / THEN the chat files are re-imported into the replacement and the call succeeds.
- Scenario SC-24: files attached once per agent / WHEN files are added to the agent's dedicated MCP files resource / THEN every MCP tool call can read them without attaching them per tool and without injecting them into the prompt.
- Scenario SC-25: files are on demand / WHEN a run does not name files / THEN nothing is copied into `/mnt/data`, the result lists what is available, and a later run loads only the named files (or all) under their plain names.

## Business rules

- Generated files are delivered as native LibreChat attachments through the fork bridge; the signed
  MCP link is the fallback when the bridge cannot store one.
- Files are loaded on request; each container imports a given file once, normalized to `/mnt/data/<name>`, and `attach_files` selects by name or `all`.
- Agent-level files live in a dedicated `mcp` resource: attached once, available to every MCP tool, never injected into the model context.
- A container is keyed server-side by `(LibreChat user id, conversation id)`; the model never supplies
  either identifier and never sees the Azure container id.
- A conversation's container is reused until 20 minutes idle, then deleted (best effort) and replaced.
- Every `run_code` call makes one Azure Responses request, plus at most one retry on 429/5xx and one
  replacement retry after container expiry.
- The Azure key and the shared MCP token live only in the MCP service.
- Artifacts are retained for 7 days and capped at 25 MB per file and 20 files per run.
- The default execution timeout is 180 s and the maximum is 600 s.
- Only the `gpt-6-luna` deployment is used.

## Behavior delta

**Added** (capability: `code-execution`):
- A LibreChat-visible `run_code` tool that runs model-written Python in Azure's hosted code interpreter.
- Per-conversation sandbox reuse, keyed by LibreChat-injected identity headers.
- Native file attachments for generated artifacts, plus signed links as a fallback.

**Modified** (capability: `librechat-mcp-integration`):
- Current LibreChat has `vision`, `ppt` and `ppt-oidc` MCP servers only → add `azureci` with static
  token headers and request headers carrying user and conversation ids, no OAuth.
- MCP tool results gain a generic artifact contract (`structuredContent.artifacts`) forwarded into the
  code-attachment pipeline, so any artifact-builder MCP server delivers native attachments.

**Removed:** (none)

## Functional constraints

- Transport is MCP streamable-http; the tool must work for any LibreChat provider, not only Azure models.
- The service must not depend on LibreChat internals or its database; identity arrives only in headers.
- Timestamps are UTC ISO-8601; sizes are bytes.
- Server instructions stay under ~700 characters; the full package list is never sent automatically.

**Open assumptions:** standalone repo at `~/dev/azure-ci-mcp`; download links use `localhost` for the
browser and `host.docker.internal` for LibreChat; chat attachments are not auto-forwarded; one Azure
deployment; the LibreChat patch is developed against the upstream `v0.8.9` checkout in `~/dev/librechat`
and the user merges it into their fork later.

## Out of scope

- Native hosted `code_interpreter` on LibreChat's own Azure endpoint (no MCP).
- Model selection or other Azure deployments.
- Per-user quotas or billing on the MCP server.
- Jobs longer than 10 minutes, custom package installs, or GPU workloads.
- Preserving sandbox memory after the 20-minute idle expiry.
- Public internet exposure of either listener.

## Technical Requirements

New repo `~/dev/azure-ci-mcp`, Python 3.12+, `mcp>=2.3`. One process, two listeners: MCP on
`172.17.0.1:8935`, downloads on `127.0.0.1:8936`; no 0.0.0.0 bind. Azure contract (verified
2026-11-21):

- `POST https://hymafoundry.openai.azure.com/openai/v1/responses`, `Authorization: Bearer $AZURE_AI_KEY`.
- Body: model + `tools:[{type:"code_interpreter",container:…}]` + `include:["code_interpreter_call.outputs"]`
  + `store:false`; without `include`, stdout is `null`.
- Files arrive as `container_file_citation` annotations (`container_id`, `file_id`, `filename`).
  `GET /containers/{cid}/files`, `GET /containers/{cid}/files/{fid}/content`, and
  `DELETE /containers/{cid}` (verified: `container.deleted`, then 404).
- Reuse: `"container": "cntr_…"`; any access resets the 20-minute idle timer, an expired id answers
  404 and is replaced with one retry. Input files use an `input_file` part with `filename` and
  `file_data: "data:<mime>;base64,…"`, visible under `/mnt/data`.

LibreChat config: bridge URL, `requiresOAuth: false`, `X-Azci-Token` header, and
`requestHeaders: {X-LibreChat-User-Id: '{{LIBRECHAT_USER_ID}}', X-LibreChat-Conversation-Id: '{{LIBRECHAT_BODY_CONVERSATIONID}}'}`.
`getMCPRequestScope()` requires `conversationId`, and the placeholders force a user-scoped connection
(`packages/api/src/mcp/utils.ts:402`, `:597`). Catalog discovery sends no request headers, so
`tools/list` must succeed without them.

Tool schema: `run_code(code [required], task?, files? [inert in v1], timeout_s?)`. Executor mode sends
the code under a "run this exact code" instruction, so `gpt-6-luna` executes but does not author it.

Artifact delivery: the MCP saves every output and returns `structuredContent.artifacts[]`
(`{name, mimeType, bytes, downloadUrl, browserUrl}`), serving `http://localhost:8936/f/<id>?t=<hmac>`
to the browser and `http://host.docker.internal:8936/...` to LibreChat. Images also ride as MCP image
content and stay on LibreChat's existing image path, so the bridge attaches non-image files only.

LibreChat fork patch (generic MCP artifacts + chat-file bridge):

- Contract: any MCP tool may return `structuredContent.artifacts[]` = `{name, mimeType, bytes (base64) | downloadUrl}`.
- `createMCPStructuredTool` forwards `structuredContent.artifacts[]` into the result's `artifact` with
  `__librechat_file_authoring: true`, reusing the code-artifact opt-in; `preflightCodeOutputBatch`
  honours a pre-supplied `preparedBuffer` so no Code API download is needed (SC-19).
- `api/server/routes/azci.js` (`/api/azci`, mounted before JWT-protected `/api/files`) lists files
  from the last 20 user messages and streams bytes, gated by `X-Azci-Token` + user id.

## Local Craft

- **mcp 2.x renamed the server class**: `mcp.server.mcpserver.MCPServer`, not `FastMCP`; serving is
  `mcp.run(transport="streamable-http", host=…, port=…)` (`~/dev/vision-mcp/vision_mcp/cli.py:128`).
  Custom HTTP routes use `@server.custom_route(path, methods=[…])` (`mcp/server/mcpserver/server.py:1031`).
  A tool can read the live HTTP request through `Context.request_context.request`, which is where the
  `mcp-session-id` and identity headers live.
- **Sibling deployment pattern is the exemplar**: `~/.config/systemd/user/vision-mcp-http.service`
  runs a venv CLI with `--host 172.17.0.1`, `Restart=on-failure`, and documents the boundary test.
- **LibreChat needs four settings together**: address allowlist, `requiresOAuth:false`, a generous
  `timeout`, and the header blocks. Missing the allowlist is a silent connect failure.
- **Key hygiene**: the LibreChat `.env` is not shell-safe (`UID` is readonly); the service uses its own
  `0600` env file, never sources `.env`.
- **Containers are not user-isolated.** Microsoft: "Containers aren't isolated per agent or per user
  within a project… Treat container IDs, and output items that contain them, as sensitive." Hence the
  server-side `(user, conversation)` map and the token gate; the model never gets an id.
- **Cost shape**: Code Interpreter is $0.033/session with a 5-minute minimum; `gpt-6-luna` is
  $0.10/M input and $0.50/M output. A typical 1k-in/200-out run is ~$0.0332, so the session fee is
  ~99 %. Per-conversation reuse is what keeps a multi-call chat at one session fee.
- **LibreChat's artifact pipeline already has an opt-in hook.** `isCodeArtifactToolOutput` accepts any
  artifact with `__librechat_file_authoring === true` (`packages/api/src/agents/tools.ts:87`), and
  `processCodeOutput` accepts a `preparedBuffer` (`packages/api/src/files/code/process.ts:133`), so the
  patch forwards `structuredContent.artifacts` into that channel.
- **Image payload limit**: LibreChat rejects oversized MCP image blobs (`packages/api/src/mcp/parsers.ts:98`);
  inline images ≤5 MB, otherwise a download link.

### Stack facts

<!-- detect-archetype.py --facts, 2026-11-21: empty repo -->
Archetypes: none detected. No manifest, no declared checks, no preview command.

## Design Convergence

- **Direct MVP**: `run_code` reads `(user, conversation)` from headers, reuses one Azure container per
  conversation until 20 min idle, saves artifacts, and a small LibreChat fork hook turns the returned
  descriptors into native attachments.
- **Failed Items**: none; the attachment gap is closed by the fork patch (task group 7).
- **Adopted Additions**: a static token header → forged-identity finding → nothing smaller stops another bridge process forging ids; a generic artifact hook → the no-upload-API finding.
- **Rejected Additions**: a model-supplied session id (LibreChat injects the real ids); a browser UI; a custom container API; background polling; Blob storage.

## Boundaries

- **Always** (in scope): new repo, systemd user unit, the LibreChat fork patch, `librechat.yaml`
  `azureci` entry + allowlist + headers, LibreChat rebuild/restart and verification, notes in
  `~/.okf/notes`.
- **Ask First**: changing LibreChat's upstream base or config version; binding any listener to
  `0.0.0.0`; forwarding chat attachments; adding another Azure deployment.

## Code Map

- `pyproject.toml`, `README.md`: packaging, entry point, operator docs.
- `src/azci_mcp/config.py`: env/config load, redaction, limits, token.
- `src/azci_mcp/azure.py`: request build, input-file encoding, response parsing, retry/error mapping,
  expiry recovery.
- `src/azci_mcp/sessions.py`: `(user, conversation)` → container map, per-conversation lock, TTL sweep.
- `src/azci_mcp/librechat.py`: list/fetch conversation + MCP-pool files via the fork bridge.
- `src/azci_mcp/artifacts.py`: download from Azure, save, signed tokens, retention sweep.
- `src/azci_mcp/server.py`: `run_code` tool, header auth/identity, result formatting, `/health`, `/f/{id}`.
- `src/azci_mcp/cli.py`: `serve`, `doctor`, `run` (manual test).
- `tests/test_azure.py`, `test_sessions.py`, `test_artifacts.py`, `test_server.py`, `test_live.py`,
  `tests/boundary.sh`.
- `deploy/azure-ci-mcp.service`, `deploy/env.example`.
- `~/dev/librechat/librechat.yaml`: `azureci` server, allowlist, token + request headers.
- `~/dev/librechat/packages/api/src/mcp/tools.ts`: forward `structuredContent.artifacts` as a tool artifact.
- `~/dev/librechat/packages/api/src/files/code/preflight.ts`: honour a pre-supplied `preparedBuffer`.
- `~/dev/librechat` tests: `packages/api/src/mcp/__tests__/`, `packages/api/src/files/code/`.

## Implementation Plan

### Overview
Total: 8 task groups, in dependency order.

### Task Group 1: Package, config and CLI
**Files:** `pyproject.toml`, `src/azci_mcp/__init__.py`, `config.py`, `cli.py`, `README.md`
**Complexity:** Simple
- Create the venv-installable package with a console script; load `AZURE_AI_KEY`, base URL, model,
  limits, paths and `AZCI_MCP_TOKEN` from an env file; expose `serve`, `doctor`, `run`.
- `doctor` verifies key/token presence, writable artifact dir, and Azure reachability without a model call.
- Tests: config precedence, missing key/token, redacted `repr`.
**Scenarios covered:** none (foundation).
**Acceptance Criteria:** `python -m azci_mcp doctor` exits non-zero on a missing key with no traceback.

### Task Group 2: Azure Responses client
**Files:** `src/azci_mcp/azure.py`, `tests/test_azure.py`, `tests/fixtures/*.json`
**Complexity:** Complex
- Build the request (task, optional code hint, inline files, explicit container id when reusing),
  send it with retry on 429/5xx honouring `retry-after-ms`, map errors, parse code_interpreter calls,
  logs, answers, citations and usage, and recover once from an expired/missing container.
- Tests: fixtures for logs/citations/images/no-code/expired; error mapping; retry-once; input limits.
**Scenarios covered:** SC-1, SC-2, SC-5, SC-6, SC-7, SC-18.
**Acceptance Criteria:** parser tests use recorded fixtures only (no network).

### Task Group 3: Artifacts and signed links
**Files:** `src/azci_mcp/artifacts.py`, `tests/test_artifacts.py`
**Complexity:** Medium
- Download cited container files, enforce caps, store under
  `~/.local/share/azure-ci-mcp/artifacts/<user-hash>/<conversation>/<run>/`, mint HMAC tokens bound to
  id + expiry, serve bytes as attachments, sweep files older than 7 days.
- Tests: round-trip, tamper/expiry refusal, 25 MB cap, retention sweep, path traversal.
**Scenarios covered:** SC-3, SC-11.
**Acceptance Criteria:** a token cannot address another artifact id, user or conversation.

### Task Group 4: Sessions, MCP surface and identity
**Files:** `src/azci_mcp/sessions.py`, `src/azci_mcp/server.py`, `tests/test_sessions.py`, `tests/test_server.py`
**Complexity:** Complex
- Register `run_code` with the documented schema; require `X-Azci-Token`; read user + conversation
  headers; map and reuse containers with a per-conversation lock; one-off container when identity is
  absent; TTL sweep deletes idle containers (delete failure ignored); return images ≤5 MB; `/health`;
  concurrency cap 4; redact secrets.
- Tests: in-process MCP client, reused vs separate containers, two users never share, missing/foreign
  headers, wrong token, serialized same-conversation calls, timeout, cleanup failure, secret scan.
**Scenarios covered:** SC-4, SC-8, SC-9, SC-10, SC-15, SC-17, SC-20, SC-21, SC-22, SC-23, SC-24, SC-25.
**Acceptance Criteria:** a call carrying another user's header pair cannot reach that user's container;
invalid input never reaches the Azure client (assert a fake client is untouched).

### Task Group 5: Deployment and boundary
**Files:** `deploy/azure-ci-mcp.service`, `deploy/env.example`, `tests/boundary.sh`
**Complexity:** Medium
- Install in a venv, write the `0600` env file, enable a systemd user unit with linger, serve MCP on
  `172.17.0.1:8935` and downloads on `127.0.0.1:8936`.
- Tests: `ss -ltn` shows only those bindings; bridge health 200; LAN IP and 0.0.0.0 refused; restart
  survives and re-answers.
**Scenarios covered:** SC-14.
**Acceptance Criteria:** `systemctl --user is-active` is active after a restart.

### Task Group 6: LibreChat integration and proof
**Files:** `~/dev/librechat/librechat.yaml`, `docs/demo.md`
**Complexity:** Complex
- Add `azureci` with `headers` (token) and `requestHeaders` (user + conversation), add the address to
  `mcpSettings.allowedAddresses`, add `AZCI_MCP_TOKEN` to the compose environment, restart.
- Prove: `/api/mcp/tools` exposes `run_code`; a local-27B chat returns a sandbox-computed value; two
  calls in one chat reuse one container while a new chat gets a new one; two users do not share; log
  the received identity headers; screenshot a chart with Playwright.
- Verify the running image digest equals registry `latest` and `/api/endpoints` still routes
  `gpt-6-luna` to the Responses API.
**Scenarios covered:** SC-12, SC-13, SC-16.
**Acceptance Criteria:** the 27B answer contains 42925 computed by the sandbox; header log shows the
real conversation id; second chat starts a new container.

### Task Group 7: LibreChat generic MCP artifact bridge
**Files:** `~/dev/librechat/packages/api/src/mcp/tools.ts`,
`~/dev/librechat/packages/api/src/files/code/preflight.ts`, LibreChat tests
**Complexity:** Medium
- Implement the generic contract: any MCP tool returning `structuredContent.artifacts[]` gets its files
  forwarded into the result's `artifact` with `__librechat_file_authoring: true` and a prepared buffer,
  stored through `processCodeOutput` and emitted with `writeAttachment`; images stay on the existing
  path; a fetch/store failure logs and leaves the signed link, the turn succeeds.
- Tests: generic server-agnostic forwarding, bytes and downloadUrl paths, size guard, successful
  attachment, failure fallback, no duplicate image attachments.
**Scenarios covered:** SC-19.
**Acceptance Criteria:** two different MCP servers emitting `structuredContent.artifacts` both produce
native attachments; the azureci CSV/XLSX appear in the fork UI and stay downloadable from the MCP link.

### Task Group 8: Documentation and decision record
**Files:** `README.md`, `docs/demo.md`, `~/.okf/notes/decisions.md`, `~/.okf/notes/index.md`
**Complexity:** Simple
- Record the endpoint contract, header contract, deployment, boundary test, limits, cost per call and
  the LibreChat attachment limitation; append the decision block and index row.
**Scenarios covered:** none (documentation).
**Acceptance Criteria:** a cold reader can redeploy from the README alone.

## Implementation Notes

- 2026-11-21 (pre-approval): per-run containers became per-conversation reuse; native attachments entered
  scope and the bridge was generalized to a server-agnostic `structuredContent.artifacts` contract;
  executor mode was fixed. SC-3/4/9 amended, SC-17/18/19 added, task group 7 added.

## Change Log
