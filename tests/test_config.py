"""Config loading, precedence, and secret redaction."""

from pathlib import Path

from azci_mcp.config import (
    Config,
    _parse_env_file,
    load,
    missing_required,
    redact,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
)


def test_parse_env_file_handles_exports_quotes_and_comments(tmp_path: Path):
    f = tmp_path / "env"
    f.write_text("# comment\nAZURE_AI_KEY='abc123'\nexport AZCI_MCP_TOKEN=\"tok\"\nAZCI_MODEL=gpt-6-luna\n")
    values = _parse_env_file(f)
    assert values == {"AZURE_AI_KEY": "abc123", "AZCI_MCP_TOKEN": "tok", "AZCI_MODEL": "gpt-6-luna"}


def test_missing_required_names_key_and_token(monkeypatch):
    monkeypatch.delenv("AZURE_AI_KEY", raising=False)
    monkeypatch.delenv("AZCI_MCP_TOKEN", raising=False)
    config = load(None)
    assert missing_required(config) == ["AZURE_AI_KEY", "AZCI_MCP_TOKEN"]


def test_missing_required_empty_when_both_set(monkeypatch):
    monkeypatch.setenv("AZURE_AI_KEY", "k")
    monkeypatch.setenv("AZCI_MCP_TOKEN", "t")
    config = load(None)
    assert missing_required(config) == []


def test_process_env_wins_over_env_file(tmp_path, monkeypatch):
    f = tmp_path / "env"
    f.write_text("AZCI_MODEL=from-file\nAZURE_AI_KEY=file-key\n")
    monkeypatch.setenv("AZCI_MODEL", "from-env")
    monkeypatch.setenv("AZURE_AI_KEY", "env-key")
    monkeypatch.setenv("AZCI_MCP_TOKEN", "t")
    config = load(f)
    assert config.model == "from-env"
    assert config.azure_api_key == "env-key"


def test_defaults_when_unset(monkeypatch):
    monkeypatch.setenv("AZURE_AI_KEY", "k")
    monkeypatch.setenv("AZCI_MCP_TOKEN", "t")
    config = load(None)
    assert config.azure_base_url == DEFAULT_BASE_URL
    assert config.model == DEFAULT_MODEL
    assert config.mcp_port == 8935
    assert config.download_port == 8936
    assert config.default_timeout_s == 180
    assert config.max_timeout_s == 600


def test_redact_masks_all_secrets():
    config = Config(azure_api_key="KEY-123", mcp_token="TOK-456")
    out = redact("used KEY-123 then TOK-456", config.secrets())
    assert "KEY-123" not in out
    assert "TOK-456" not in out
    assert out == "used *** then ***"


def test_download_origin_and_bridge_origin():
    config = Config(azure_api_key="k", mcp_token="t")
    assert config.download_origin == "http://127.0.0.1:8936"
    assert config.bridge_origin == "http://host.docker.internal:8936"
