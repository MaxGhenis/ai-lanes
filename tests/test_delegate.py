import json
from datetime import timedelta
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from ai_lanes import config, delegate


@pytest.mark.parametrize(("prompt", "kind", "model"), [
    ("Review this patch", "judgment", "fable"),
    ("Draft an email for a client", "judgment", "fable"),
    ("Write an essay", "judgment", "fable"),
    ("Recommend a strategy", "judgment", "fable"),
    ("Assess and implement it", "judgment", "fable"),
    ("For each file, check imports", "sweep", "terra"),
    ("Extract ids across all rows", "sweep", "terra"),
    ("Count a batch of records", "sweep", "terra"),
    ("For each feature implement it", "build", "sol"),
    ("Implement the endpoint", "build", "sol"),
    ("Fix and test the bug", "build", "sol"),
    ("Refactor the parser", "build", "sol"),
])
def test_routing_table(prompt, kind, model):
    got, _ = delegate.classify(prompt)
    assert got == kind
    assert delegate.choose_model(got) == model


def test_overrides_and_haiku_only_explicit():
    kind, _ = delegate.classify("review this", "build")
    assert (kind, delegate.choose_model(kind, "terra")) == ("build", "terra")
    assert delegate.choose_model("judgment") != "haiku"
    assert delegate.choose_model("build", "haiku") == "haiku"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    config.save(
        {
            "accounts": [
                "alpha@example.com",
                "beta@example.com",
                "charlie@example.com",
                "delta@example.com",
            ],
            "enrolled": {
                email: f"claude-quota-{email}"
                for email in (
                    "alpha@example.com",
                    "beta@example.com",
                    "charlie@example.com",
                    "delta@example.com",
                )
            },
            "codex_homes": [],
        }
    )
    state = config.state_dir()
    monkeypatch.setattr(delegate, "_active_desktop_email", lambda: None)
    return state


def test_rotation_persists_skips_cooldown_and_expiry(isolated):
    assert delegate.pick_fable_lane() == "alpha@example.com"
    assert delegate.pick_fable_lane() == "beta@example.com"
    delegate.record_cooldown("charlie@example.com", delegate._now() + timedelta(hours=1))
    assert delegate.pick_fable_lane() == "delta@example.com"
    delegate.record_cooldown("charlie@example.com", delegate._now() - timedelta(seconds=1))
    assert delegate.pick_fable_lane() == "alpha@example.com"
    assert json.loads((isolated / "rotation.json").read_text())["last_used"] == "alpha@example.com"


def fake_run_factory(results, calls):
    def run(cmd, **kwargs):
        calls.append(cmd)
        if "status" in cmd:
            return CompletedProcess(cmd, 1, "", "")
        value = results.pop(0)
        return CompletedProcess(cmd, *value)
    return run


def test_rc4_cooldown_rotates_and_three_attempt_cap(isolated, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(delegate.subprocess, "run", fake_run_factory([(4, "", "limit resets 3:15pm")]*3, calls))
    assert delegate.main(["-m", "fable", "task", "-o", str(isolated / "out")]) == 3
    assert len(calls) == 3
    assert len(json.loads((isolated / "cooldowns.json").read_text())) == 3
    assert len((isolated / "decisions.jsonl").read_text().splitlines()) == 3


def test_rc5_long_cooldown_and_ritual(isolated, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(delegate.subprocess, "run", fake_run_factory([(5, "", "dead")]*3, calls))
    assert delegate.main(["-m", "fable", "task", "-o", str(isolated / "out")]) == 3
    err = capsys.readouterr().err
    assert "claude setup-token" in err and "claude-quota-alpha@example.com" in err
    until = next(iter(json.loads((isolated / "cooldowns.json").read_text()).values()))
    assert delegate.datetime.fromisoformat(until) > delegate._now() + timedelta(days=29)


def test_codex_no_lane_and_overflow(isolated, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(delegate.subprocess, "run", fake_run_factory([(1, "", "reset 4pm\n"), (0, "ok", "")], calls))
    out = isolated / "out"
    assert delegate.main(["implement x", "-o", str(out)]) == 3
    assert "reset 4pm" in capsys.readouterr().err
    calls.clear()
    monkeypatch.setattr(delegate.subprocess, "run", fake_run_factory([(1, "", "none\n"), (0, "ok", "")], calls))
    assert delegate.main(["--overflow", "implement x", "-o", str(out)]) == 0
    assert "cross-family" in capsys.readouterr().err


def test_preamble_defaults_off_dry_run_and_decision(isolated, monkeypatch, capsys):
    seen = []
    def run(cmd, **kwargs):
        if cmd[-1:] == ["--json"] or "status" in cmd:
            return CompletedProcess(cmd, 1, "", "")
        if "codex-pick" in cmd[0]:
            return CompletedProcess(cmd, 0, "/home/codex\n", "")
        seen.append((cmd, Path(cmd[cmd.index("-p") + 1]).read_text()))
        return CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(delegate.subprocess, "run", run)
    out = isolated / "out"
    assert delegate.main(["implement x", "-o", str(out)]) == 0
    assert "Standing orders" in seen[0][1] and seen[0][0][seen[0][0].index("-s") + 1] == "workspace-write"
    assert "-e" in seen[0][0] and "ultra" in seen[0][0]
    seen.clear()
    assert delegate.main(["-m", "fable", "--no-preamble", "review x", "-o", str(out)]) == 0
    assert seen[0][1] == "review x" and seen[0][0][seen[0][0].index("-s") + 1] == "read-only"
    seen.clear()
    assert delegate.main(["--dry-run", "-H", "/h", "implement x", "-o", str(out)]) == 0
    assert not seen and "codex-run" in capsys.readouterr().out
    assert len((isolated / "decisions.jsonl").read_text().splitlines()) == 3


def test_detach_requires_output():
    with pytest.raises(SystemExit) as exc:
        delegate.main(["-d", "task"])
    assert exc.value.code == 2
