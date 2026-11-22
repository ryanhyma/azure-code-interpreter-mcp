"""CLI: doctor (verify config + reachability), run (one task), serve (MCP streamable-http)."""

from __future__ import annotations

import argparse
import socket
import sys
from pathlib import Path

from .config import load, missing_required, redact


def _probe_azure_reachable(base_url: str, timeout_s: float = 5.0) -> tuple[bool, str]:
    """TCP-connect to the Azure host. No auth, no model call: proves reachability only."""
    host = base_url.split("://", 1)[-1].split("/", 1)[0].split(":")[0]
    port = 443
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True, f"reachable ({host}:{port})"
    except OSError as exc:
        return False, f"unreachable ({host}:{port}): {exc.__class__.__name__}"


def cmd_doctor(args: argparse.Namespace) -> int:
    config = load(Path(args.env_file) if args.env_file else None)
    secrets = config.secrets()
    failures = 0

    missing = missing_required(config)
    for name in missing:
        print(f"MISSING: {name} is required", file=sys.stderr)
        failures += 1

    config.artifact_dir.mkdir(parents=True, exist_ok=True)
    probe = config.artifact_dir / ".write-probe"
    try:
        probe.write_text("ok")
        probe.unlink()
        print(f"artifact dir: writable ({config.artifact_dir})")
    except OSError as exc:
        print(f"artifact dir: NOT writable ({config.artifact_dir}): {exc}", file=sys.stderr)
        failures += 1

    ok, detail = _probe_azure_reachable(config.azure_base_url)
    print(f"azure: {detail}")
    if not ok:
        failures += 1

    masked = redact(config.azure_base_url, secrets)
    print(f"base url: {masked}  model: {config.model}")

    if failures:
        print(f"\n{failures} problem(s) found", file=sys.stderr)
        return 1
    print("\nok")
    return 0


def cmd_run(_args: argparse.Namespace) -> int:
    from .azure import run_task  # deferred: wired in the Azure client group

    return run_task()


def cmd_serve(_args: argparse.Namespace) -> int:
    from .server import serve  # deferred: wired in the MCP server group

    return serve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="azure-ci-mcp",
        description="Code execution in Azure's hosted sandbox, exposed as an MCP server.",
    )
    parser.add_argument("--env-file", default=None, help="path to the 0600 env file")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="verify config, artifact dir and Azure reachability")
    doctor.set_defaults(func=cmd_doctor)

    run = sub.add_parser("run", help="run one code snippet through the sandbox")
    run.set_defaults(func=cmd_run)

    serve = sub.add_parser("serve", help="start the MCP streamable-http server")
    serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
