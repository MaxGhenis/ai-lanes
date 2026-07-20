"""Human rendering: terminal table, morning-brief markdown section."""

from .util import fmt_clock, now_local, parse_iso

VERDICT_LABELS = {
    "ok": "OK",
    "limited": "LIMITED",
    "auth-revoked": "AUTH-REVOKED",
    "auth-suspect": "AUTH-SUSPECT",
    "no-auth": "NO-AUTH",
    "unknown": "UNKNOWN",
}


def _pct(w: dict | None) -> str:
    if not w or w.get("used_percent") is None:
        return "?"
    return f"{round(float(w['used_percent']))}%"


def _reset(w: dict | None, now) -> str:
    if not w:
        return "?"
    return fmt_clock(parse_iso(w.get("reset_at")), now)


def _short_home(home: str) -> str:
    return home.replace(str(__import__("pathlib").Path.home()), "~")


def table(snap: dict) -> str:
    now = (parse_iso(snap.get("generated_at")) or now_local()).astimezone()
    lines = [f"AI quota — {now.strftime('%Y-%m-%d %-I:%M%p %Z').lower()}", ""]
    lines.append("CODEX (ChatGPT accounts, one per CODEX_HOME)")
    header = f"  {'home':<11} {'account':<26} {'5h':>5} {'resets':<14} {'week':>5}  status"
    lines.append(header)
    for e in snap["codex"]["homes"]:
        w = e["windows"]
        label = VERDICT_LABELS.get(e["verdict"], e["verdict"])
        if e.get("duplicate_of"):
            label += f" (dup of {_short_home(e['duplicate_of'])})"
        src = ""
        if w["source"] == "observed" and w.get("as_of"):
            src = f"  [observed {fmt_clock(parse_iso(w['as_of']), now)}]"
        elif w["source"] == "none":
            src = "  [no data]"
        acct = e.get("email") or (e.get("account_id") or "?")[:12]
        lines.append(
            f"  {_short_home(e['home']):<11} {acct:<26} {_pct(w['primary']):>5}"
            f" {_reset(w['primary'], now):<14} {_pct(w['secondary']):>5}  {label}{src}"
        )
        for err in e["recent_errors"]["usage_limit"][:1]:
            lines.append(
                f"  {'':<11} last usage-limit error {fmt_clock(parse_iso(err['observed_at']), now)}"
                f" (retry {err['try_again']})"
            )
        for err in e["recent_errors"]["auth_revoked"][:1]:
            lines.append(
                f"  {'':<11} refresh-token-revoked error seen {fmt_clock(parse_iso(err['observed_at']), now)}"
            )
    fleet = snap["codex"]["fleet"]
    best = fleet.get("best_home")
    fleet_line = f"  fleet: {fleet['dispatchable_now']}/{fleet['total_homes']} dispatchable"
    if best:
        fleet_line += f" · best: {_short_home(best)}"
    if fleet.get("earliest_reset"):
        fleet_line += f" · earliest 5h reset: {fmt_clock(parse_iso(fleet['earliest_reset']), now)}"
    lines += [fleet_line, ""]

    c = snap["claude"]
    acct = c["account"].get("email") or "?"
    tier = c.get("tier") or c.get("subscription") or "?"
    lines.append(f"CLAUDE (active login: {acct}, tier {tier})")
    active_row = next((a for a in c.get("accounts") or [] if a.get("active")), {})
    live = active_row.get("live")
    sl = c.get("statusline")
    if live and live.get("five_hour_pct") is not None:
        wk = f" · week {live['seven_day_pct']}%" if live.get("seven_day_pct") is not None else ""
        models = " ".join(
            f"· {m} wk {round(p)}%" for m, p in (live.get("model_weeks") or {}).items()
        )
        lines.append(
            f"  5h window: {live['five_hour_pct']}% used"
            f" ({live['source']} {fmt_clock(parse_iso(live.get('as_of')), now)}){wk}"
            + (f" {models}" if models else "")
        )
    elif sl and sl.get("five_hour_pct") is not None:
        fresh = "live" if sl.get("fresh") else f"as of {fmt_clock(parse_iso(sl['updated_at']), now)}"
        wk = f" · week {sl['seven_day_pct']}%" if sl.get("seven_day_pct") is not None else ""
        lines.append(f"  5h window: {sl['five_hour_pct']}% used ({fresh}){wk}")
    else:
        probe_status = (c.get("oauth_probe") or {}).get("status", "?")
        hint = {
            "rate-limited": "usage endpoint 429 — account limited or throttled; retrying each cycle",
            "token-invalid": "keychain token stale — refreshes when a desktop session runs",
        }.get(probe_status, f"usage endpoint: {probe_status}")
        lines.append(f"  5h window: unknown — {hint}")
    active = c.get("active_limit")
    if active:
        lines.append(
            f"  ACTIVE LIMIT: {active['kind']} — resets {fmt_clock(parse_iso(active['reset_at']), now)}"
            f" (seen in {active.get('sessions', 1)} session(s))"
        )
    elif c["recent_errors"]:
        last = c["recent_errors"][0]
        lines.append(
            f"  last limit event: {last['kind']} at {fmt_clock(parse_iso(last['observed_at']), now)}"
            + (f", reset {fmt_clock(parse_iso(last['reset_at']), now)}" if last.get("reset_at") else "")
        )
    else:
        lines.append("  no limit errors observed in the last 24h")
    kc = c.get("keychain", {})
    probe = c.get("oauth_probe", {})
    if probe.get("status") == "token-invalid":
        lines.append(
            f"  keychain OAuth token: INVALID (expired {kc.get('expires_at', '?')[:10]})"
            " — informational; live sessions authenticate separately"
        )
    accounts = c.get("accounts") or []
    fleet_c = c.get("lanes") or {}
    lanes = fleet_c.get("lanes") or []
    if lanes:
        fleet_line = (
            f"  lanes: {fleet_c.get('dispatchable_now', 0)}/{fleet_c.get('enrolled', 0)} dispatchable"
        )
        if fleet_c.get("best"):
            fleet_line += f" · best: {fleet_c['best']}"
        if fleet_c.get("earliest_reset"):
            fleet_line += f" · earliest reset: {fmt_clock(parse_iso(fleet_c['earliest_reset']), now)}"
        lines.append(fleet_line)
        lines.append(f"  {'lane':<28} {'5h':>5} {'resets':<14} {'week':>5}  status")

        def lane_pct(v):
            return f"{round(float(v))}%" if v is not None else "?"

        for l in lanes:
            status = "OK" if l["verdict"] == "ok" else l["verdict"].upper()
            if l.get("active"):
                status += " (active login)"
            lines.append(
                f"  {l['email']:<28} {lane_pct(l.get('five_hour_used_percent')):>5}"
                f" {fmt_clock(parse_iso(l.get('five_hour_reset_at')), now):<14}"
                f" {lane_pct(l.get('weekly_used_percent')):>5}  {status}"
            )
    else:
        # Pre-lanes snapshots (or none enrolled with old data): legacy per-account lines.
        for a in accounts:
            if a["active"] or not a.get("enrolled"):
                continue
            p = a.get("probe") or {}
            if p.get("status") == "ok":
                fh = (p.get("five_hour") or {}).get("used_percent")
                sd = (p.get("seven_day") or {}).get("used_percent")
                detail = " · ".join(
                    s for s in (
                        f"5h {round(fh)}%" if fh is not None else None,
                        f"wk {round(sd)}%" if sd is not None else None,
                    ) if s
                ) or "probed ok (no window fields)"
            else:
                detail = f"probe {p.get('status', '?')}"
            lines.append(f"  {a['email']:<28} {detail}")
    unenrolled = [a for a in accounts if not a["active"] and not a.get("enrolled")]
    if unenrolled:
        lines.append(
            f"  not enrolled ({len(unenrolled)}): "
            + ", ".join(a["email"].split("@")[1] for a in unenrolled)
            + "  — enroll: claude setup-token | ai-lanes enroll <email>"
        )
    return "\n".join(lines)


def brief_md(snap: dict) -> str:
    """Compact Markdown capacity summary."""
    now = parse_iso(snap.get("generated_at")) or now_local()
    lines = ["## AI capacity"]
    fleet = snap["codex"]["fleet"]
    parts = [f"codex: {fleet['dispatchable_now']}/{fleet['total_homes']} lanes dispatchable"]
    if fleet.get("best_home"):
        parts.append(f"best {_short_home(fleet['best_home'])}")
    if fleet.get("earliest_reset"):
        parts.append(f"earliest reset {fmt_clock(parse_iso(fleet['earliest_reset']), now)}")
    lines.append("- " + " · ".join(parts))
    problems = []
    for e in snap["codex"]["homes"]:
        if e["verdict"] in ("auth-revoked", "auth-suspect", "no-auth"):
            problems.append(f"{_short_home(e['home'])} {e['verdict']}")
    if snap["codex"]["duplicates"]:
        dups = ", ".join(
            " + ".join(_short_home(h) for h in d["homes"]) for d in snap["codex"]["duplicates"]
        )
        problems.append(f"DUPLICATE account bindings: {dups} (revocation trap)")
    if problems:
        lines.append("- ⚠ codex auth: " + "; ".join(problems) + " — fix: `CODEX_HOME=<home> codex login`")
    c = snap["claude"]
    sl = c.get("statusline") or {}
    if c.get("active_limit"):
        a = c["active_limit"]
        lines.append(
            f"- claude: LIMITED ({a['kind']}) — resets {fmt_clock(parse_iso(a['reset_at']), now)}"
        )
    elif sl.get("five_hour_pct") is not None:
        wk = f", week {sl['seven_day_pct']}%" if sl.get("seven_day_pct") is not None else ""
        stamp = "" if sl.get("fresh") else f" (as of {fmt_clock(parse_iso(sl.get('updated_at')), now)})"
        lines.append(f"- claude: 5h {sl['five_hour_pct']}%{wk}{stamp}")
    else:
        lines.append("- claude: usage unknown (no fresh statusline capture)")
    fleet_c = c.get("lanes") or {}
    if fleet_c.get("enrolled"):
        lane_parts = [
            f"claude lanes: {fleet_c.get('dispatchable_now', 0)}/{fleet_c['enrolled']} dispatchable"
        ]
        if fleet_c.get("best"):
            lane_parts.append(f"best {fleet_c['best']}")
        if fleet_c.get("earliest_reset"):
            lane_parts.append(f"earliest reset {fmt_clock(parse_iso(fleet_c['earliest_reset']), now)}")
        lines.append("- " + " · ".join(lane_parts))
    return "\n".join(lines)
