"""Filesystem locations, all overridable via env for tests."""

import os
from pathlib import Path

HOME = Path.home()


def _env_path(name: str, default: Path) -> Path:
    v = os.environ.get(name)
    return Path(v).expanduser() if v else default


def codex_homes() -> list[Path]:
    """CODEX_HOME dirs in canonical dispatch order (~/.codex, ~/.codex-2, ...).

    AI_LANES_CODEX_HOMES (colon-separated) overrides discovery entirely.
    """
    override = os.environ.get("AI_LANES_CODEX_HOMES")
    if override:
        return [Path(p).expanduser() for p in override.split(":") if p]
    homes = [HOME / ".codex"] + [HOME / f".codex-{i}" for i in range(2, 10)]
    return [h for h in homes if h.is_dir()]


def primary_codex_home() -> Path:
    """The home shared with the Codex desktop app / ChatGPT app gauge."""
    homes = codex_homes()
    return homes[0] if homes else HOME / ".codex"


def state_dir() -> Path:
    return _env_path("AI_LANES_STATE_DIR", HOME / ".local" / "state" / "ai-lanes")


def claude_dir() -> Path:
    return _env_path("AI_LANES_CLAUDE_DIR", HOME / ".claude")


def claude_json() -> Path:
    return _env_path("AI_LANES_CLAUDE_JSON", HOME / ".claude.json")


def notify_bin() -> Path:
    return _env_path("AI_LANES_NOTIFY", HOME / ".local" / "bin" / "notify")


def snapshot_path() -> Path:
    return state_dir() / "snapshot.json"


def alerts_path() -> Path:
    return state_dir() / "alerts.json"


def history_path() -> Path:
    return state_dir() / "history.jsonl"


def brief_path() -> Path:
    return state_dir() / "brief.md"


def statusline_state_path() -> Path:
    return state_dir() / "claude-statusline.json"


def rollout_cache_path() -> Path:
    return state_dir() / "rollout-scan-cache.json"
