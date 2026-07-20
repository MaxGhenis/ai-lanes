"""ai-lanes CLI.

  ai-lanes                      # human table (live probes)
  ai-lanes status --json        # machine-readable snapshot
  ai-lanes status --cached      # last watchdog snapshot, no network
  ai-lanes pick                 # best CODEX_HOME on stdout (rc=1 if none)
  ai-lanes pick --json --all    # full ranking with exclusion context
  ai-lanes claude-pick          # best enrolled Claude lane (email) on stdout
  ai-lanes claude-pick --json --all  # full lane ranking with exclusions
  ai-lanes errors --hours 48    # observed limit/auth errors, both providers
  ai-lanes watch [--dry-run]    # one monitor/alert cycle (scheduler-friendly)
  ai-lanes enroll <email>       # store a Claude lane token (from claude setup-token)

Orchestrator one-liners:
  CODEX_HOME=$(ai-lanes pick) codex-run ...   # or: codex-pick
  claude-lane -A ...                          # auto-lane via claude-pick
"""

import argparse
import json
import sys

from . import claude, codex, config, paths, render, secret_store, snapshot, watchdog
from .util import load_json, strip_private


def _load_snapshot(cached: bool, live_timeout: float = 15.0) -> dict:
    if cached:
        snap = load_json(paths.snapshot_path())
        if snap:
            return snap
        print("ai-lanes: no cached snapshot yet; probing live", file=sys.stderr)
    # Small error window interactively; the watchdog covers the long window.
    return snapshot.build(live=True, timeout=live_timeout, errors_hours=6)


def cmd_status(args) -> int:
    snap = _load_snapshot(args.cached)
    if args.json:
        print(json.dumps(snap, indent=1))
    else:
        print(render.table(snap))
        if args.cached:
            print(f"\n(cached snapshot from {snap.get('generated_at')}; use without --cached for live)")
    return 0


def cmd_pick(args) -> int:
    snap = _load_snapshot(args.cached)
    handicap = 0.0 if args.no_handicap else args.handicap
    ranked = snapshot.rank_for_dispatch(
        snap["codex"]["homes"], handicap=handicap, min_headroom=args.min_headroom
    )
    if args.json:
        out = {
            "generated_at": snap["generated_at"],
            "best": ranked[0]["home"] if ranked else None,
            "ranked": ranked if args.all else ranked[:1],
            "excluded": [
                {
                    "home": e["home"],
                    "verdict": e["verdict"],
                    "duplicate_of": e.get("duplicate_of"),
                    "five_hour_used_percent": (e["windows"].get("primary") or {}).get("used_percent"),
                }
                for e in snap["codex"]["homes"]
                if e["home"] not in {r["home"] for r in ranked}
            ],
        }
        print(json.dumps(out, indent=1))
        return 0 if ranked else 1
    if not ranked:
        earliest = snap["codex"]["fleet"].get("earliest_reset")
        print(
            "ai-lanes pick: no dispatchable codex home"
            + (f" (earliest 5h reset {earliest})" if earliest else ""),
            file=sys.stderr,
        )
        return 1
    best = ranked[0]
    stale = " [stale data]" if best.get("stale") else ""
    print(best["home"])
    print(
        f"  {best.get('email') or best.get('account_id', '?')} · 5h {best['five_hour_used_percent']:.0f}%"
        f" used · week {best['weekly_used_percent']:.0f}%{stale}",
        file=sys.stderr,
    )
    if args.all:
        for r in ranked[1:]:
            print(
                f"  next: {r['home']} ({r.get('email') or '?'} · 5h {r['five_hour_used_percent']:.0f}%)",
                file=sys.stderr,
            )
    return 0


def _claude_lane_rows(cached: bool, timeout: float = 15.0) -> tuple[str | None, list[dict]]:
    """(generated_at, account rows) for lane ranking. The live path probes only
    the Claude side — dispatch shouldn't pay for codex probes."""
    if cached:
        snap = load_json(paths.snapshot_path())
        if snap:
            return snap.get("generated_at"), (snap.get("claude") or {}).get("accounts") or []
        print("ai-lanes: no cached snapshot yet; probing live", file=sys.stderr)
    from .util import iso, now_local

    return iso(now_local()), claude.accounts_report(claude.identity().get("email"), timeout=timeout)


def cmd_claude_pick(args) -> int:
    generated_at, rows = _claude_lane_rows(args.cached)
    handicap = 0.0 if args.no_handicap else args.handicap
    ranked = claude.rank_lanes(rows, handicap=handicap, min_headroom=args.min_headroom)
    fleet = claude.lanes_fleet(rows, handicap=handicap, min_headroom=args.min_headroom)
    if args.json:
        out = {
            "generated_at": generated_at,
            "best": ranked[0]["email"] if ranked else None,
            "ranked": ranked if args.all else ranked[:1],
            "excluded": [
                {"email": l["email"], "verdict": l["verdict"], "reset_at": l.get("reset_at")}
                for l in fleet["lanes"]
                if l["verdict"] != "ok"
            ],
            "enrolled": fleet["enrolled"],
            "earliest_reset": fleet["earliest_reset"],
        }
        print(json.dumps(out, indent=1))
        return 0 if ranked else 1
    if not ranked:
        if fleet["enrolled"] == 0:
            print(
                "ai-lanes claude-pick: no lanes enrolled — enroll with: "
                "claude setup-token, then ai-lanes enroll <email>",
                file=sys.stderr,
            )
        else:
            blocked = "; ".join(
                f"{l['email']} {l['verdict']}" for l in fleet["lanes"] if l["verdict"] != "ok"
            )
            print(
                "ai-lanes claude-pick: no dispatchable claude lane"
                + (f" (earliest reset {fleet['earliest_reset']})" if fleet.get("earliest_reset") else "")
                + (f" — {blocked}" if blocked else ""),
                file=sys.stderr,
            )
        return 1

    def _detail(r):
        fh = r.get("five_hour_used_percent")
        wk = r.get("weekly_used_percent")
        return (
            f"5h {fh:.0f}%" if fh is not None else "5h ?"
        ) + " used · " + (f"week {wk:.0f}%" if wk is not None else "week ?") + (
            " [active login]" if r.get("active") else ""
        )

    best = ranked[0]
    print(best["email"])
    print(f"  {best['email']} · {_detail(best)}", file=sys.stderr)
    if args.all:
        for r in ranked[1:]:
            print(f"  next: {r['email']} ({_detail(r)})", file=sys.stderr)
    return 0


def cmd_errors(args) -> int:
    out = {
        "codex": {
            str(h): codex.recent_limit_errors(h, hours=args.hours)
            for h in paths.codex_homes()
        },
        "claude": claude.transcript_limit_events(hours=args.hours),
    }
    if args.json:
        print(json.dumps(strip_private(out), indent=1))
        return 0
    for home, errs in out["codex"].items():
        for e in errs["usage_limit"]:
            print(f"codex {home}: usage limit at {e['observed_at']} (retry {e['try_again']})")
        for e in errs["auth_revoked"]:
            print(f"codex {home}: REFRESH TOKEN REVOKED at {e['observed_at']}")
    for e in out["claude"]:
        reset = f", resets {e['reset_at']}" if e.get("reset_at") else ""
        print(f"claude: {e['kind']} at {e['observed_at']} x{e['count']}{reset}")
    if not any(v["usage_limit"] or v["auth_revoked"] for v in out["codex"].values()) and not out["claude"]:
        print(f"no limit/auth errors observed in the last {args.hours}h")
    return 0


def cmd_watch(args) -> int:
    summary = watchdog.run(dry_run=args.dry_run)
    print(json.dumps(summary))
    return 0


def cmd_enroll(args) -> int:
    """Store a per-account Claude OAuth token (from `claude setup-token`) so the
    watchdog can probe that account's limits. Token is read from stdin — never
    from argv — validated against the usage endpoint before storing."""
    import getpass
    email = args.email
    try:
        cfg = config.load(strict=True)
    except config.ConfigError as exc:
        print(f"ai-lanes enroll: {exc}", file=sys.stderr)
        return 2
    accounts = cfg.get("accounts", [])
    enrolled = cfg.get("enrolled")
    if not isinstance(accounts, list) or (enrolled is not None and not isinstance(enrolled, dict)):
        print(f"ai-lanes enroll: invalid roster schema in {config.accounts_path()}", file=sys.stderr)
        return 2
    roster = sorted(account for account in accounts if isinstance(account, str) and "@" in account)
    if email not in roster:
        print(f"ai-lanes enroll: {email} is not in the roster ({len(roster)} accounts); "
              f"add it to {claude.roster_config_path()} first", file=sys.stderr)
        return 2
    if sys.stdin.isatty():
        token = getpass.getpass(f"Paste setup-token for {email} (input hidden): ").strip()
    else:
        token = sys.stdin.read().strip()
    if not token:
        print("ai-lanes enroll: empty token", file=sys.stderr)
        return 2
    probe = claude.probe_oauth_usage(token)
    if probe.get("status") != "ok":
        print(f"ai-lanes enroll: token REJECTED by usage endpoint ({probe.get('status')}) — "
              "not storing. Is it fresh, and for the right account?", file=sys.stderr)
        return 1
    secret = config.secret_name_for(email)
    if not secret_store.set(secret, token):
        print("ai-lanes enroll: secret store failed", file=sys.stderr)
        return 1
    cfg.setdefault("enrolled", {})[email] = secret
    config.save(cfg)
    extracted = {k: probe.get(k) for k in ("five_hour", "seven_day") if probe.get(k)}
    print(f"enrolled {email} -> secret store item {secret}; probe ok"
          + (f" {json.dumps(extracted)}" if extracted else " (no window fields recognized — raw kept in snapshots)"))
    return 0


def cmd_secret(args) -> int:
    """Internal bridge used by the hardened shell runner."""
    name = config.secret_name_for(args.email, require_enrolled=True)
    if name is None:
        print(
            f"ai-lanes secret: {args.email} is not enrolled in {config.accounts_path()}",
            file=sys.stderr,
        )
        return 1
    value = secret_store.get(name)
    if value is None:
        print(f"ai-lanes secret: item unavailable for {args.email}", file=sys.stderr)
        return 1
    print(value)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ai-lanes", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")

    p_status = sub.add_parser("status", help="per-account quota + auth table")
    p_status.add_argument("--json", action="store_true")
    p_status.add_argument("--cached", action="store_true", help="use last watchdog snapshot (no network)")

    p_pick = sub.add_parser("pick", help="best CODEX_HOME for dispatch")
    p_pick.add_argument("--json", action="store_true")
    p_pick.add_argument("--all", action="store_true", help="show full ranking")
    p_pick.add_argument("--cached", action="store_true")
    p_pick.add_argument("--min-headroom", type=float, default=snapshot.DEFAULT_MIN_HEADROOM,
                        help="minimum 5h-window headroom %% to qualify (default 5)")
    p_pick.add_argument("--handicap", type=float, default=10.0,
                        help="score penalty for the primary home ~/.codex (default 10)")
    p_pick.add_argument("--no-handicap", action="store_true",
                        help="rank purely by usage (may burn the primary account's window)")

    p_cpick = sub.add_parser("claude-pick", help="best enrolled Claude lane (email) for dispatch")
    p_cpick.add_argument("--json", action="store_true")
    p_cpick.add_argument("--all", action="store_true", help="show full ranking")
    p_cpick.add_argument("--cached", action="store_true", help="use last watchdog snapshot (no network)")
    p_cpick.add_argument("--min-headroom", type=float, default=claude.DEFAULT_MIN_HEADROOM,
                         help="minimum headroom %% required in EVERY window (default 5)")
    p_cpick.add_argument("--handicap", type=float, default=claude.ACTIVE_HANDICAP,
                         help="score penalty for the active desktop-login account (default 10)")
    p_cpick.add_argument("--no-handicap", action="store_true",
                         help="rank purely by usage (may burn the active login's window)")

    p_errors = sub.add_parser("errors", help="observed limit/auth errors")
    p_errors.add_argument("--hours", type=float, default=24)
    p_errors.add_argument("--json", action="store_true")

    p_watch = sub.add_parser("watch", help="one snapshot + alert cycle (for cron or another scheduler)")
    p_watch.add_argument("--dry-run", action="store_true", help="print alerts instead of sending")

    p_enroll = sub.add_parser("enroll", help="store a Claude account token for quota probing")
    p_enroll.add_argument("email", help="account email (must be in accounts.json roster)")

    p_secret = sub.add_parser("secret", help=argparse.SUPPRESS)
    p_secret.add_argument("action", choices=("get-for-account",))
    p_secret.add_argument("email")

    argv = list(sys.argv[1:] if argv is None else argv)
    known = {"status", "pick", "claude-pick", "errors", "watch", "enroll", "secret"}
    if not argv or (argv[0] not in known and argv[0] not in ("-h", "--help")):
        argv = ["status", *argv]
    args = parser.parse_args(argv)
    handlers = {
        "status": cmd_status,
        "pick": cmd_pick,
        "claude-pick": cmd_claude_pick,
        "errors": cmd_errors,
        "watch": cmd_watch,
        "enroll": cmd_enroll,
        "secret": cmd_secret,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
