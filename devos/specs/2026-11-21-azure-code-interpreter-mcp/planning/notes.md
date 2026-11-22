---
title: "Azure Code Interpreter MCP — interview record"
---

# Interview record — Azure Code Interpreter MCP

## Q&A

- **User, 2026-11-21:** "build an MCP server for librechat that uses azure code interpreter through
  responses API, look at weave and DevOS to start, the LibreChat should be setup and running with
  latest and the setup model is Lina on azure."
- Recon answer (verified on the box): LibreChat runs the current `librechat-dev:latest` image
  (digest `sha256:07e5b90…`, created 2026-11-20); the only Azure deployment is `gpt-6-luna` on
  `hymafoundry.openai.azure.com/openai/v1`. Code interpreter works and returns stdout only with
  `include:["code_interpreter_call.outputs"]`.
- **User, 2026-11-21 (security round):** "how does interpreter work for isolation of users… how would
  files remain valid [after 20 min]… is it cheaper to move artifacts into LibreChat or leave in
  cloud storage."
- Answer recorded from Microsoft: containers are sandboxed VMs but **not isolated per user within a
  project**; anyone who can name an active container can read its files. Container ids are sensitive.
  Code Interpreter is **$0.033/session** (5-minute minimum); `gpt-6-luna` is $0.10/M input and
  $0.50/M output.
- **User, 2026-11-21 (this round):** "could the container id be stored MCP side and other identifier
  for chat session passed by tool call to the MCP server then apply mapping?" and "a bridge will need
  to be created to save returned artifacts."
- Finding: LibreChat **does** support per-chat identity headers for MCP. `requestHeaders` accepts
  `{{LIBRECHAT_USER_ID}}` and `{{LIBRECHAT_BODY_CONVERSATIONID}}`; `getMCPRequestScope()` marks the
  server request-scoped and requires `conversationId`; `requiresUserScopedConnection()` then forces a
  per-user MCP connection. `packages/data-provider/src/mcp.ts:497`, `packages/api/src/mcp/utils.ts:402`.
  So the chat id does not have to come from the model, and cannot be forged by it.
- Finding: there is no supported way for an MCP server to upload a file into a user's LibreChat
  attachments. `POST /api/files` needs that user's LibreChat JWT; the MCP only gets a bearer for
  itself. MCP `resource` blobs are summarized, images become artifacts
  (`packages/api/src/mcp/parsers.ts:149`). Native attachment delivery needs a LibreChat patch.

## Decisions

- **User, 2026-11-21 (answers):** (1) tests ship along the way — confirmed, every task group has them.
  (2) Language: executor mode fixed; artifacts land in LibreChat's attachment store. (3) The fork is
  NOT on this server: patch the upstream `v0.8.9` checkout in `~/dev/librechat` and the user merges
  later. (4) "SDD includes tests along the way" — yes.
- Executor mode is the contract: `code` is required, run verbatim; `task` is optional context.
- Language stays Python 3.12+ (not Node): the workload is I/O-bound, so there is no meaningful
  performance difference; Python matches the existing vision-mcp/ppt-mcp pattern and the verified
  `mcp` 2.3 API. Node would be equally fine and is a maintainability choice, not a performance one.
- Artifact → LibreChat: the fork bridge persists each file through `processCodeOutput` + `writeAttachment`,
  making it a DB-backed attachment with a `/api/files/<id>/download` URL (LibreChat has no literal
  "downloads" folder). The MCP signed link is only the fallback.
- **User, 2026-11-21:** the bridge must be generic for MCP tools — other artifact builders will exist.
  Contract: any MCP tool may return `structuredContent.artifacts[]` and get native attachments;
  `azureci` is only the first emitter. No azureci-specific magic key.

## Open assumptions

- Container id stays server-side; key the map by `(user id, conversation id)` taken from headers.
- One container per conversation, reused while active, 20-minute idle TTL, best-effort delete on
  expiry. A missing/foreign identity header gets a one-off container, never someone else's.
- A static shared secret header (`X-Azci-Token`) gates tool calls, so another process on the docker
  bridge cannot forge identity headers.
- Artifacts stay in the MCP store with signed 7-day links; LibreChat-native attachment delivery is a
  separate decision (needs a LibreChat patch).
- Artifacts are stored MCP-side with signed links **and** delivered as native LibreChat attachments
  through a small fork patch. The user confirmed the fork already exists, so the patch ships in this
  spec and is migrated like their other deviations.
- The fork patch has two hooks: forward `structuredContent.artifacts` as a tool artifact, then store
  each one through `processCodeOutput({preparedBuffer})` and emit it with `writeAttachment`. No new
  LibreChat API endpoint and no user JWT are needed.

## Open assumptions

- A1: standalone repo at `~/dev/azure-ci-mcp`. Owner: user.
- A2: download links use `localhost:8936` (host browser). Owner: user.
- A3: chat attachments are not auto-forwarded into the sandbox in v1. Owner: user.
- A4: only `gpt-6-luna`. Owner: user.
- A5: the fork patch is rebased onto the user's LibreChat base and rebuilt into their image; the MCP
  download origin (`host.docker.internal:8936`) is allowlisted in the patch. Owner: user.
