import io
import json
import sys

import pytest

from ai_lanes import claude, cli, config, secret_store


def test_help_exposes_generic_watch_without_removed_couplings(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])

    assert exc.value.code == 0
    help_text = capsys.readouterr().out.lower()
    assert "watch" in help_text
    assert "brief" not in help_text
    assert "statusline" not in help_text
    assert "launchd" not in help_text


def test_enroll_uses_configured_prefix_store_and_accounts_file(monkeypatch, capsys):
    config.save(
        {
            "accounts": ["alpha@example.com"],
            "enrolled": {},
            "codex_homes": [],
            "secret_name_prefix": "lane-token-",
            "notify_cmd": ["notify-tool"],
        }
    )
    stored = []
    monkeypatch.setattr(sys, "stdin", io.StringIO("test-token\n"))
    monkeypatch.setattr(
        claude,
        "probe_oauth_usage",
        lambda token: {
            "status": "ok",
            "five_hour": {"used_percent": 12},
            "seven_day": {"used_percent": 34},
        },
    )
    monkeypatch.setattr(secret_store, "set", lambda name, value: stored.append((name, value)) or True)

    assert cli.main(["enroll", "alpha@example.com"]) == 0

    assert stored == [("lane-token-alpha@example.com", "test-token")]
    saved = config.load(strict=True)
    assert saved["enrolled"] == {"alpha@example.com": "lane-token-alpha@example.com"}
    assert saved["notify_cmd"] == ["notify-tool"]
    rendered = config.accounts_path().read_text()
    assert json.loads(rendered) == saved
    captured = capsys.readouterr()
    assert "test-token" not in captured.out + captured.err


def test_enroll_store_failure_does_not_mark_account_enrolled(monkeypatch, capsys):
    config.save({"accounts": ["beta@example.com"], "enrolled": {}, "codex_homes": []})
    monkeypatch.setattr(sys, "stdin", io.StringIO("test-token\n"))
    monkeypatch.setattr(claude, "probe_oauth_usage", lambda token: {"status": "ok"})
    monkeypatch.setattr(secret_store, "set", lambda name, value: False)

    assert cli.main(["enroll", "beta@example.com"]) == 1

    assert config.load(strict=True)["enrolled"] == {}
    captured = capsys.readouterr()
    assert "secret store failed" in captured.err.lower()
    assert "test-token" not in captured.out + captured.err


def test_enroll_rejects_bad_token_before_secret_store(monkeypatch, capsys):
    config.save({"accounts": ["beta@example.com"], "enrolled": {}, "codex_homes": []})
    monkeypatch.setattr(sys, "stdin", io.StringIO("test-token\n"))
    monkeypatch.setattr(claude, "probe_oauth_usage", lambda token: {"status": "token-invalid"})
    monkeypatch.setattr(secret_store, "set", lambda *args: pytest.fail("invalid token was stored"))

    assert cli.main(["enroll", "beta@example.com"]) == 1
    assert config.load(strict=True)["enrolled"] == {}
    assert "rejected" in capsys.readouterr().err.lower()
