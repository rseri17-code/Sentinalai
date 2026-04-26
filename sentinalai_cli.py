#!/usr/bin/env python3
"""SentinalAI CLI — terminal interface for SRE power users.

Usage:
  sentinalai investigate INC-12378
  sentinalai investigate INC-12378 --watch
  sentinalai status payment-service
  sentinalai status payment-service --json
  sentinalai handoff --from alice --to bob
  sentinalai predict payment-service --hours 4
  sentinalai postmortem INC-12378
  sentinalai postmortem INC-12378 --publish
  sentinalai replay INC-12378
  sentinalai sentinel --services payment-service,cart-service
  sentinalai version

Configure via env vars or ~/.sentinalai:
  SENTINALAI_API_URL   (default: http://localhost:8081)
  SENTINALAI_TOKEN     (JWT token)
  SENTINALAI_CHANNEL   (Slack channel for alerts)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# ANSI color helpers — no external deps
# ---------------------------------------------------------------------------

_NO_COLOR = not sys.stdout.isatty() or os.getenv("NO_COLOR")


def _c(code: str, text: str) -> str:
    if _NO_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def red(t: str) -> str:    return _c("31", t)
def green(t: str) -> str:  return _c("32", t)
def yellow(t: str) -> str: return _c("33", t)
def blue(t: str) -> str:   return _c("34", t)
def cyan(t: str) -> str:   return _c("36", t)
def bold(t: str) -> str:   return _c("1", t)
def dim(t: str) -> str:    return _c("2", t)


def _severity_color(sev: str) -> str:
    s = str(sev).upper()
    if s in ("CRITICAL", "5", "4"):
        return red(s)
    if s in ("HIGH", "3"):
        return yellow(s)
    if s in ("MEDIUM", "2"):
        return blue(s)
    return green(s)


def _confidence_bar(confidence: int | float, width: int = 12) -> str:
    pct = max(0, min(100, int(confidence)))
    filled = round(pct / 100 * width)
    bar = "█" * filled + "░" * (width - filled)
    color = green if pct >= 80 else (yellow if pct >= 50 else red)
    return f"[{color(bar)}] {pct}%"


def _urgency_color(urgency: str) -> str:
    u = str(urgency).upper()
    return {"BREACHED": red, "IMMINENT": red, "WARNING": yellow, "WATCH": blue}.get(u, cyan)(u)


def _hr(char: str = "─", width: int = 70) -> str:
    return dim(char * width)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

API_URL = os.getenv("SENTINALAI_API_URL", "http://localhost:8081")
TOKEN = os.getenv("SENTINALAI_TOKEN", "")


def _headers() -> dict:
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if TOKEN:
        h["Authorization"] = f"Bearer {TOKEN}"
    return h


def _get(path: str, params: dict | None = None) -> dict:
    try:
        import httpx
        r = httpx.get(f"{API_URL}{path}", headers=_headers(), params=params, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        print(red(f"  API error: {exc}"), file=sys.stderr)
        sys.exit(1)


def _post(path: str, body: dict) -> dict:
    try:
        import httpx
        r = httpx.post(f"{API_URL}{path}", headers=_headers(), json=body, timeout=120)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        print(red(f"  API error: {exc}"), file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# investigate
# ---------------------------------------------------------------------------

def cmd_investigate(args: argparse.Namespace) -> int:
    incident_id = args.incident_id.upper()
    print(bold(f"\n  SentinalAI — Investigating {incident_id}"))
    print(_hr())
    print(f"  Started at {_now()}")
    print()

    data = _post("/api/v1/investigations", {"incident_id": incident_id, "priority": "high"})
    investigation_id = data.get("investigation_id", "unknown")
    print(f"  {dim('Investigation:')} {cyan(investigation_id)}")
    print(f"  {dim('Ops Center:  ')} {API_URL}/investigations/{investigation_id}")
    print()

    if not args.watch:
        print(dim("  Use --watch to stream live progress. Investigation running in background."))
        return 0

    # Stream live progress
    print(bold("  Live Progress"))
    print(_hr())

    try:
        import httpx
        with httpx.Client(timeout=180) as client:
            with client.stream("GET", f"{API_URL}/api/v1/investigations/{investigation_id}/events") as stream:
                for line in stream.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue

                    et = event.get("event_type", "")
                    payload = event.get("payload", {})
                    ts = _now()

                    if et == "incident.classified":
                        svc = payload.get("service", "?")
                        itype = payload.get("incident_type", "?")
                        print(f"  {dim(ts)}  {blue('CLASSIFIED')}  {bold(svc)} → {itype}")

                    elif et == "tool.called":
                        worker = payload.get("worker", "?")
                        action = payload.get("action", "?")
                        print(f"  {dim(ts)}  {cyan('CALLING')}    {worker}.{action}()")

                    elif et == "tool.responded":
                        worker = payload.get("worker", "?")
                        count = payload.get("result_count", 0)
                        elapsed = payload.get("elapsed_ms", 0)
                        print(f"  {dim(ts)}  {green('RECEIVED')}   {worker} → {count} results in {elapsed}ms")

                    elif et == "hypothesis.selected":
                        name = payload.get("hypothesis_name", "?")
                        conf = payload.get("confidence", 0)
                        print(f"  {dim(ts)}  {yellow('HYPOTHESIS')}  {bold(name)} ({_confidence_bar(conf, 6)})")

                    elif et == "budget.warning":
                        remaining = payload.get("remaining", 0)
                        print(f"  {dim(ts)}  {yellow('BUDGET')}     {remaining} tool calls remaining")

                    elif et == "investigation.completed":
                        result = payload.get("result", {})
                        _print_rca(result, investigation_id, args.json_output)
                        return 0

                    elif et == "investigation.failed":
                        print(f"\n  {red('FAILED')}  {payload.get('error', 'Unknown error')}")
                        return 1

    except KeyboardInterrupt:
        print(f"\n  {dim('Interrupted. Investigation continues in background.')}")
        print(f"  {dim('Ops Center:')} {API_URL}/investigations/{investigation_id}")
        return 0

    return 0


def _print_rca(result: dict, investigation_id: str, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(result, indent=2))
        return

    root_cause = result.get("root_cause", "Unknown")
    confidence = result.get("confidence", 0)
    service = result.get("affected_service", "?")
    severity = result.get("severity", "?")
    elapsed = result.get("elapsed_ms", 0) // 1000
    remediation = result.get("remediation", {})
    immediate = remediation.get("immediate_actions", [])
    fix = remediation.get("permanent_fix", "")

    print()
    print(_hr("═"))
    print(f"  {bold('ROOT CAUSE FOUND')}")
    print(_hr("═"))
    print()
    print(f"  {bold('Root Cause:')}  {root_cause}")
    print(f"  {bold('Service:  ')}  {bold(service)}")
    print(f"  {bold('Severity: ')}  {_severity_color(severity)}")
    print(f"  {bold('Confidence:')} {_confidence_bar(confidence)}")
    print(f"  {bold('Time:     ')}  {elapsed}s")
    print()

    if immediate:
        print(f"  {bold('Immediate Actions:')}")
        for action in immediate[:3]:
            print(f"    • {action}")
        print()

    if fix:
        print(f"  {bold('Proposed Fix:')}")
        print(f"    {fix[:300]}")
        print()

    print(f"  {bold('Evidence:')}")
    timeline = result.get("evidence_timeline", [])
    for entry in timeline[:5]:
        t = entry.get("time", "?")
        ev = entry.get("event", "?")[:80]
        src = dim(entry.get("source", "?"))
        print(f"    {dim(t[:19])}  {ev}  {src}")
    if len(timeline) > 5:
        print(f"    {dim(f'... and {len(timeline) - 5} more events')}")

    print()
    print(f"  {dim('Full report:')} {API_URL}/investigations/{investigation_id}")
    print(_hr())


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def cmd_status(args: argparse.Namespace) -> int:
    service = args.service
    print(bold(f"\n  SentinalAI — Status: {service}"))
    print(_hr())

    data = _get("/api/v1/investigations", {"service": service, "limit": 5})
    investigations = data.get("investigations", [])

    if not investigations:
        print(f"  {dim('No recent investigations for')} {service}")
        return 0

    if args.json_output:
        print(json.dumps(investigations, indent=2))
        return 0

    for inv in investigations:
        iid = inv.get("incident_id", "?")
        status = inv.get("status", "?").upper()
        conf = inv.get("confidence", 0)
        rc = inv.get("root_cause", "—")[:70]
        sev = inv.get("severity", "?")

        status_color = green if status == "COMPLETED" else (red if status == "FAILED" else yellow)
        print(f"  {bold(iid)}  [{status_color(status)}]  {_severity_color(sev)}")
        print(f"  {dim('Root cause:')} {rc}")
        print(f"  {dim('Confidence:')} {_confidence_bar(conf, 8)}")
        print()

    print(_hr())
    return 0


# ---------------------------------------------------------------------------
# handoff
# ---------------------------------------------------------------------------

def cmd_handoff(args: argparse.Namespace) -> int:
    outgoing = args.outgoing or os.getenv("USER", "outgoing-sre")
    incoming = args.incoming or "incoming-sre"

    print(bold(f"\n  SentinalAI — Shift Handoff: {outgoing} → {incoming}"))
    print(_hr())

    data = _post("/api/v1/handoff", {
        "outgoing_engineer": outgoing,
        "incoming_engineer": incoming,
    })

    if args.json_output:
        print(json.dumps(data, indent=2))
        return 0

    summary = data.get("summary", "")
    if summary:
        print()
        print(f"  {summary}")
        print()

    fragile = data.get("fragile_services", [])
    if fragile:
        print(bold("  Fragile Services"))
        print(_hr("·"))
        for svc in fragile[:5]:
            risk = svc.get("risk_level", "elevated").upper()
            count = svc.get("incident_count_7d", 0)
            name = svc.get("service", "?")
            color = red if risk == "CRITICAL" else (yellow if risk == "HIGH" else blue)
            print(f"  [{color(risk)}]  {bold(name)}  —  {count} incidents in 7d")
        print()

    active = data.get("active_investigations", [])
    if active:
        print(bold("  Active Investigations"))
        print(_hr("·"))
        for inv in active[:3]:
            iid = inv.get("incident_id", "?")
            status = inv.get("status", "open")
            print(f"  {cyan(iid)}  {dim(status)}")
        print()

    upcoming = data.get("upcoming_risk", [])
    if upcoming:
        print(bold("  Upcoming Changes"))
        print(_hr("·"))
        for c in upcoming[:5]:
            risk = c.get("risk_level", "medium").upper()
            svc = c.get("service", "?")
            ct = c.get("change_type", "?")
            at = c.get("scheduled_at", "?")[:16]
            color = red if risk == "HIGH" else (yellow if risk == "MEDIUM" else green)
            print(f"  [{color(risk)}]  {bold(svc)}  {ct}  at {dim(at)}")
        print()

    guidance = data.get("conditional_guidance", [])
    if guidance:
        print(bold("  If/Then Guidance"))
        print(_hr("·"))
        for g in guidance[:3]:
            trigger = g.get("trigger", "?")
            action = g.get("action", "?")
            runbook = g.get("runbook_hint", "")
            print(f"  {yellow('IF:')} {trigger}")
            print(f"  {green('DO:')} {action}")
            if runbook:
                print(f"  {dim('REF:')} {runbook}")
            print()

    print(_hr())
    return 0


# ---------------------------------------------------------------------------
# predict
# ---------------------------------------------------------------------------

def cmd_predict(args: argparse.Namespace) -> int:
    service = args.service
    hours = args.hours

    print(bold(f"\n  SentinalAI — Predictive Signals: {service} (next {hours}h)"))
    print(_hr())

    data = _get("/api/v1/intelligence/predict", {"service": service, "hours": hours})
    alerts = data.get("alerts", [])

    if not alerts:
        print(f"  {green('No anomalous signals detected.')} {service} looks healthy.")
        print(_hr())
        return 0

    if args.json_output:
        print(json.dumps(alerts, indent=2))
        return 0

    for alert in alerts:
        urgency = alert.get("urgency", "WATCH")
        metric = alert.get("metric_name", "?")
        current = alert.get("current_value", 0)
        threshold = alert.get("threshold", 100)
        breach = alert.get("minutes_to_breach")
        action = alert.get("recommended_action", "")

        pct = round(current / threshold * 100) if threshold else 0
        breach_str = f"{int(breach)}min to breach" if breach else "BREACHED"

        print(f"  [{_urgency_color(urgency)}]  {bold(metric)}")
        print(f"  Current: {current:.2f}  Threshold: {threshold:.2f}  ({pct}%)")
        print(f"  {_confidence_bar(pct, 10)}  {dim(breach_str)}")
        if action:
            print(f"  {dim('Action:')} {action}")
        print()

    print(_hr())
    return 0


# ---------------------------------------------------------------------------
# postmortem
# ---------------------------------------------------------------------------

def cmd_postmortem(args: argparse.Namespace) -> int:
    incident_id = args.incident_id.upper()
    print(bold(f"\n  SentinalAI — Postmortem: {incident_id}"))
    print(_hr())

    data = _post("/api/v1/postmortem", {"incident_id": incident_id})

    if args.json_output:
        print(json.dumps(data, indent=2))
        return 0

    print(f"  {bold('Status:')}          {yellow(data.get('status', 'draft').upper())}")
    print(f"  {bold('Service:')}         {data.get('affected_service', '?')}")
    print(f"  {bold('Severity:')}        {_severity_color(data.get('severity', '?'))}")
    print(f"  {bold('Duration:')}        {data.get('duration_minutes', '?')} minutes")
    print()
    print(f"  {bold('Executive Summary:')}")
    print(f"  {data.get('executive_summary', '—')[:300]}")
    print()

    five_whys = data.get("five_whys", [])
    if five_whys:
        print(bold("  5 Whys:"))
        for i, why in enumerate(five_whys, 1):
            print(f"  {dim(str(i) + '.')} {why}")
        print()

    action_items = data.get("action_items", [])
    if action_items:
        print(bold("  Action Items:"))
        for ai in action_items[:5]:
            priority = ai.get("priority", "P3")
            title = ai.get("title", "?")
            due = ai.get("due_days", "?")
            p_color = red if priority == "P1" else (yellow if priority == "P2" else green)
            print(f"  [{p_color(priority)}]  {title}  {dim(f'(due {due}d)')}")
        print()

    if args.publish:
        print(dim("  Publishing to Confluence..."))
        result = _post(f"/api/v1/postmortem/{data.get('report_id', 'unknown')}/publish", {})
        url = result.get("confluence_url", "")
        if url:
            print(f"  {green('Published:')} {url}")
        else:
            print(yellow("  Publishing not configured (set CONFLUENCE_* env vars)"))

    print(_hr())
    return 0


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------

def cmd_replay(args: argparse.Namespace) -> int:
    incident_id = args.incident_id.upper()
    print(bold(f"\n  SentinalAI — Replay: {incident_id}"))
    print(_hr())

    data = _post("/api/v1/replay", {"incident_id": incident_id, "mode": "step"})
    url = f"{API_URL}/investigations/{data.get('investigation_id', '')}"
    print(f"  Replay started: {cyan(url)}")
    print(dim("  Open the Ops Center to step through the investigation."))
    print(_hr())
    return 0


# ---------------------------------------------------------------------------
# sentinel
# ---------------------------------------------------------------------------

def cmd_sentinel(args: argparse.Namespace) -> int:
    services = [s.strip() for s in args.services.split(",") if s.strip()]
    interval = args.interval

    print(bold(f"\n  SentinalAI — Sentinel Loop"))
    print(_hr())
    print(f"  Watching: {', '.join(bold(s) for s in services)}")
    print(f"  Interval: {interval}s")
    print(f"  Press Ctrl+C to stop")
    print()

    from supervisor.sentinel_loop import SentinelLoop
    loop = SentinelLoop(services=services, poll_interval=interval)
    loop.start()

    try:
        while loop.is_running():
            stats = loop.stats()
            print(
                f"\r  {dim(_now())}  "
                f"Cycles: {stats['cycles_completed']}  "
                f"Alerts posted: {stats['alerts_posted']}  ",
                end="",
                flush=True,
            )
            time.sleep(5)
    except KeyboardInterrupt:
        print()
        loop.stop()
        stats = loop.stats()
        print(f"  Stopped. {stats['cycles_completed']} cycles, {stats['alerts_posted']} alerts posted.")

    print(_hr())
    return 0


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------

def cmd_version(_args: argparse.Namespace) -> int:
    print(bold("\n  SentinalAI CLI"))
    print(f"  API: {dim(API_URL)}")
    print(f"  Auth: {green('configured') if TOKEN else yellow('no token (set SENTINALAI_TOKEN)')}")
    print()
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sentinalai",
        description="SentinalAI — Autonomous SRE Intelligence CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  sentinalai investigate INC-12378 --watch\n"
            "  sentinalai status payment-service\n"
            "  sentinalai handoff --from alice --to bob\n"
            "  sentinalai predict payment-service --hours 4\n"
            "  sentinalai postmortem INC-12378 --publish\n"
            "  sentinalai sentinel --services payment-service,cart-service\n"
        ),
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # investigate
    p_inv = sub.add_parser("investigate", help="Start autonomous root cause analysis")
    p_inv.add_argument("incident_id", help="Incident ID (e.g. INC-12378)")
    p_inv.add_argument("--watch", action="store_true", help="Stream live progress to terminal")
    p_inv.add_argument("--json", dest="json_output", action="store_true", help="Output JSON")

    # status
    p_status = sub.add_parser("status", help="Show recent investigation history for a service")
    p_status.add_argument("service", help="Service name")
    p_status.add_argument("--json", dest="json_output", action="store_true")

    # handoff
    p_handoff = sub.add_parser("handoff", help="Generate shift handoff intelligence brief")
    p_handoff.add_argument("--from", dest="outgoing", default="", help="Outgoing engineer")
    p_handoff.add_argument("--to", dest="incoming", default="", help="Incoming engineer")
    p_handoff.add_argument("--json", dest="json_output", action="store_true")

    # predict
    p_predict = sub.add_parser("predict", help="Run predictive signal detection")
    p_predict.add_argument("service", help="Service name")
    p_predict.add_argument("--hours", type=int, default=4, help="Forecast horizon (default: 4)")
    p_predict.add_argument("--json", dest="json_output", action="store_true")

    # postmortem
    p_pm = sub.add_parser("postmortem", help="Generate blameless postmortem draft")
    p_pm.add_argument("incident_id", help="Incident ID")
    p_pm.add_argument("--publish", action="store_true", help="Publish to Confluence")
    p_pm.add_argument("--json", dest="json_output", action="store_true")

    # replay
    p_replay = sub.add_parser("replay", help="Replay a past investigation deterministically")
    p_replay.add_argument("incident_id", help="Incident ID")

    # sentinel
    p_sentinel = sub.add_parser("sentinel", help="Run the proactive sentinel monitoring loop")
    p_sentinel.add_argument(
        "--services", default=os.getenv("SENTINEL_SERVICES", ""),
        help="Comma-separated service list"
    )
    p_sentinel.add_argument(
        "--interval", type=int, default=int(os.getenv("SENTINEL_POLL_INTERVAL_SECONDS", "60")),
        help="Poll interval in seconds"
    )

    # version
    sub.add_parser("version", help="Show version and config")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    handlers = {
        "investigate": cmd_investigate,
        "status":      cmd_status,
        "handoff":     cmd_handoff,
        "predict":     cmd_predict,
        "postmortem":  cmd_postmortem,
        "replay":      cmd_replay,
        "sentinel":    cmd_sentinel,
        "version":     cmd_version,
    }

    handler = handlers.get(args.command)
    if not handler:
        parser.print_help()
        return 1

    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
