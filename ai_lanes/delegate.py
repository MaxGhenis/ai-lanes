"""Content- and capacity-aware dispatch to the hardened agent runners."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

from . import config
from .util import atomic_write_json

JUDGMENT_PATTERNS = (
    r"review", r"adjudicat", r"referee", r"judge", r"assess", r"critique",
    r"decide", r"verdict", r"write", r"draft", r"prose", r"essay", r"email",
    r"blog post", r"voice", r"wdyt", r"strategy", r"design doc",
    r"recommend",
)
SWEEP_PATTERNS = (r"for each", r"per-file", r"per-item", r"per-row", r"batch of", r"enumerate", r"across all")
MECHANICAL_PATTERNS = (r"verify", r"count", r"list", r"extract", r"check")
BUILD_PATTERNS = (r"implement", r"fix", r"refactor", r"port", r"migrate", r"wire", r"test")

MODEL_FAMILY = {"fable": "claude", "haiku": "claude", "sol": "codex", "terra": "codex"}
MODEL_NAMES = {
    "fable": "claude-fable-5", "haiku": "claude-haiku-4-5-20251001",
    "sol": "gpt-5.6-sol", "terra": "gpt-5.6-terra",
}
PREAMBLE_WRITE = ("Standing orders: commit after every coherent step; create and maintain a committed "
                  "PROGRESS.md (state/done/next) from the start; write your final report to the output file.")
PREAMBLE_AUDIT = "Frame this as a defensive correctness and completeness audit."


def _now() -> datetime:
    return datetime.now().astimezone()


def _state_dir() -> Path:
    return config.state_dir()


def _accounts_file() -> Path:
    return config.accounts_path()


def _discover_tool(name: str, env_var: str) -> str:
    """Resolve an override, this checkout's bin/, then PATH."""
    override = os.environ.get(env_var)
    if override:
        return override
    local = Path(__file__).resolve().parent.parent / "bin" / name
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError(f"{name} not found in repository bin or PATH")


def _reenroll_ritual(email: str) -> str:
    secret_name = config.secret_name_for(email, require_enrolled=False)
    return (
        f"Re-enroll lane {email}: run `claude setup-token` while signed into {email}, "
        f"then run `ai-lanes enroll {email}` (secret item `{secret_name}`)."
    )


def _matches(prompt: str, patterns: Sequence[str]) -> list[str]:
    return [p for p in patterns if re.search(p, prompt, re.IGNORECASE)]


def classify(prompt: str, forced: str | None = None) -> tuple[str, dict[str, list[str]]]:
    signals = {
        "judgment": _matches(prompt, JUDGMENT_PATTERNS),
        "sweep": _matches(prompt, SWEEP_PATTERNS),
        "mechanical": _matches(prompt, MECHANICAL_PATTERNS),
        "build": _matches(prompt, BUILD_PATTERNS),
    }
    if forced:
        return forced, signals
    if signals["judgment"]:
        return "judgment", signals
    if signals["sweep"] and signals["mechanical"]:
        return "sweep", signals
    return "build", signals


def choose_model(task_class: str, explicit: str | None = None) -> str:
    return explicit or {"judgment": "fable", "sweep": "terra", "build": "sol"}[task_class]


def _load_cooldowns() -> dict[str, str]:
    try:
        value = json.loads((_state_dir() / "cooldowns.json").read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cooldowns(data: dict[str, str]) -> None:
    path = _state_dir() / "cooldowns.json"
    atomic_write_json(path, data)


def _rotation() -> dict[str, str]:
    try:
        return json.loads((_state_dir() / "rotation.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _set_last_used(email: str) -> None:
    path = _state_dir() / "rotation.json"
    atomic_write_json(path, {"last_used": email})


def _enrolled() -> list[str]:
    enrolled = config.load().get("enrolled", {})
    return list(enrolled) if isinstance(enrolled, dict) else []


def _active_desktop_email() -> str | None:
    try:
        binary = _discover_tool("ai-lanes", "DELEGATE_AI_LANES")
        cp = subprocess.run([binary, "status", "--cached", "--json"], capture_output=True, text=True)
        if cp.returncode:
            return None
        data = json.loads(cp.stdout)
        # Accommodate both snapshot layouts and future additive changes.
        for row in data.get("claude", {}).get("accounts", data.get("claude_accounts", [])):
            if row.get("active"):
                return row.get("email")
        return data.get("claude", {}).get("active_email")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def pick_fable_lane(exclude: set[str] | None = None) -> str | None:
    """Pick and persist one optimistic Claude lane; the sole replaceable seam."""
    exclude = exclude or set()
    now = _now()
    cooldowns = _load_cooldowns()
    live = []
    for email in _enrolled():
        try:
            until = datetime.fromisoformat(cooldowns.get(email, ""))
        except ValueError:
            until = now - timedelta(seconds=1)
        if email not in exclude and until <= now:
            live.append(email)
    if not live:
        return None
    last = _rotation().get("last_used")
    if last in live:
        pos = (live.index(last) + 1) % len(live)
        live = live[pos:] + live[:pos]
    active = _active_desktop_email()
    if len(live) > 1 and live[0] == active:
        live.append(live.pop(0))
    picked = live[0]
    _set_last_used(picked)
    return picked


def record_cooldown(email: str, until: datetime) -> None:
    data = _load_cooldowns()
    data[email] = until.isoformat()
    _save_cooldowns(data)


def _limited_until(text: str) -> datetime:
    match = re.search(r"resets\s+(\d{1,2}):(\d{2})\s*(am|pm)", text, re.IGNORECASE)
    now = _now()
    if not match:
        return now + timedelta(minutes=60)
    hour, minute, meridiem = int(match[1]), int(match[2]), match[3].lower()
    hour = hour % 12 + (12 if meridiem == "pm" else 0)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return candidate if candidate > now else candidate + timedelta(days=1)


def _append_decision(record: dict[str, Any]) -> None:
    path = _state_dir() / "decisions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="delegate")
    p.add_argument("-t", choices=("judgment", "build", "sweep"))
    p.add_argument("-m", choices=("fable", "sol", "terra", "haiku"))
    resources = p.add_mutually_exclusive_group()
    resources.add_argument("-a", metavar="EMAIL")
    resources.add_argument("-H", metavar="CODEX_HOME")
    p.add_argument("-C", default=os.getcwd())
    p.add_argument("-o")
    p.add_argument("-s", choices=("read-only", "workspace-write"))
    p.add_argument("-d", action="store_true")
    p.add_argument("-b")
    p.add_argument("--overflow", action="store_true")
    p.add_argument("--no-preamble", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--why", action="store_true")
    p.add_argument("--status", action="store_true")
    source = p.add_mutually_exclusive_group()
    source.add_argument("-p", metavar="PROMPTFILE")
    source.add_argument("prompt", nargs="?")
    return p


def _status() -> int:
    cooldowns, last, now = _load_cooldowns(), _rotation().get("last_used"), _now()
    print(f"Claude lanes (last_used={last or '-'}):")
    for email in _enrolled():
        try:
            until = datetime.fromisoformat(cooldowns.get(email, ""))
        except ValueError:
            until = now
        print(f"  {email}: " + (f"cooldown until {until.isoformat()}" if until > now else "available"))
    try:
        cp = subprocess.run(
            [_discover_tool("codex-pick", "DELEGATE_CODEX_PICK"), "--json", "--all"],
            text=True,
            capture_output=True,
        )
        sys.stdout.write(cp.stdout)
        sys.stderr.write(cp.stderr)
    except OSError as exc:
        print(f"delegate: codex-pick unavailable: {exc}", file=sys.stderr)
    return 0


def _prompt_text(args: argparse.Namespace, parser: argparse.ArgumentParser) -> str:
    if args.p:
        try:
            return Path(args.p).read_text()
        except OSError as exc:
            parser.error(str(exc))
    if args.prompt is None:
        parser.error("one of -p PROMPTFILE or PROMPT_TEXT is required")
    return args.prompt


def _codex_home() -> tuple[str | None, str]:
    try:
        cp = subprocess.run(
            [_discover_tool("codex-pick", "DELEGATE_CODEX_PICK")],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return None, str(exc) + "\n"
    return (cp.stdout.strip() if cp.returncode == 0 and cp.stdout.strip() else None), cp.stderr


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.status:
        return _status()
    if args.d and not args.o:
        parser.error("-d requires -o")
    prompt = _prompt_text(args, parser)
    task_class, signals = classify(prompt, args.t)
    model = choose_model(task_class, args.m)
    family = MODEL_FAMILY[model]
    if args.a and family != "claude":
        parser.error("-a is only valid with fable or haiku")
    if args.H and family != "codex":
        parser.error("-H is only valid with sol or terra")
    sandbox = args.s or ("workspace-write" if task_class == "build" else "read-only")
    overrides = {k: v for k, v in {"class": args.t, "model": args.m, "lane": args.a, "home": args.H, "sandbox": args.s}.items() if v is not None}

    temp_paths: list[str] = []
    output = args.o
    if not output:
        fd, output = tempfile.mkstemp(prefix="delegate-output-", suffix=".md")
        os.close(fd); temp_paths.append(output)
    contents = prompt
    preamble = []
    if not args.no_preamble:
        if sandbox == "workspace-write":
            preamble.append(PREAMBLE_WRITE)
        if task_class == "judgment":
            preamble.append(PREAMBLE_AUDIT)
    if preamble:
        contents = "\n".join(preamble) + "\n\n" + prompt
    fd, merged = tempfile.mkstemp(prefix="delegate-prompt-", suffix=".md")
    with os.fdopen(fd, "w") as f:
        f.write(contents)
    temp_paths.append(merged)

    tried: set[str] = set()
    attempts = 0
    result = 3
    while True:
        lane_or_home: str | None
        if family == "codex":
            lane_or_home = args.H
            pick_error = ""
            if not lane_or_home:
                lane_or_home, pick_error = _codex_home()
            if not lane_or_home:
                sys.stderr.write(pick_error)
                if args.overflow and task_class == "build":
                    print("delegate: WARNING Codex capacity exhausted; overflowing build cross-family to fable", file=sys.stderr)
                    family, model = "claude", "fable"
                    continue
                result = 3
                cmd: list[str] = []
            else:
                try:
                    runner = _discover_tool("codex-run", "DELEGATE_CODEX_RUN")
                except FileNotFoundError as exc:
                    print(f"delegate: {exc}", file=sys.stderr)
                    cmd = []
                    result = 3
                else:
                    cmd = [runner, "-H", lane_or_home, "-m", MODEL_NAMES[model],
                           "-C", args.C, "-p", merged, "-o", output, "-s", sandbox]
                    if model == "sol": cmd += ["-e", "ultra"]
                    if args.b: cmd += ["-b", args.b]
                    result = 0 if args.dry_run else subprocess.run(cmd).returncode
        else:
            lane_or_home = args.a or pick_fable_lane(tried)
            if not lane_or_home or attempts >= 3:
                cmd = []
                result = 3
            else:
                tried.add(lane_or_home); attempts += 1
                try:
                    runner = _discover_tool("claude-lane", "DELEGATE_CLAUDE_LANE")
                except FileNotFoundError as exc:
                    print(f"delegate: {exc}", file=sys.stderr)
                    cmd = []
                    result = 3
                else:
                    cmd = [runner, "-a", lane_or_home, "-m", MODEL_NAMES[model],
                           "-C", args.C, "-p", merged, "-o", output, "-s", sandbox]
                    if args.d: cmd.append("-d")
                    if args.b: cmd += ["-b", args.b]
                    cp = None if args.dry_run else subprocess.run(cmd, capture_output=True, text=True)
                    if cp is None:
                        result = 0
                    else:
                        sys.stdout.write(cp.stdout); sys.stderr.write(cp.stderr)
                        result = cp.returncode
                        if result == 4:
                            record_cooldown(lane_or_home, _limited_until(cp.stderr + cp.stdout))
                        elif result == 5:
                            record_cooldown(lane_or_home, _now() + timedelta(days=30))
                            print(_reenroll_ritual(lane_or_home), file=sys.stderr)
                        if result in (4, 5) and not args.a and attempts < 3:
                            attempt_record = {"ts": _now().isoformat(), "class": task_class, "model": model,
                                              "lane/home": lane_or_home, "signals matched": signals,
                                              "overrides": overrides, "cmd": cmd}
                            _append_decision(attempt_record)
                            if args.why:
                                print("delegate decision: " + json.dumps(attempt_record, sort_keys=True), file=sys.stderr)
                            continue
                        if result in (4, 5):
                            result = 3

        record = {"ts": _now().isoformat(), "class": task_class, "model": model,
                  "lane/home": lane_or_home, "signals matched": signals, "overrides": overrides, "cmd": cmd}
        _append_decision(record)
        if args.why:
            print("delegate decision: " + json.dumps(record, sort_keys=True), file=sys.stderr)
        if args.dry_run:
            print(" ".join(__import__("shlex").quote(part) for part in cmd))
        elif result == 0 and not args.o and Path(output).exists():
            sys.stdout.write(Path(output).read_text())
        for path in temp_paths:
            try: Path(path).unlink()
            except OSError: pass
        return result


if __name__ == "__main__":
    raise SystemExit(main())
