"""Slack API router — slash commands, events, and interactive actions.

Endpoints:
  POST /api/v1/slack/events      — Slack Event API (URL verification + events)
  POST /api/v1/slack/slash       — Slash command handler (/sre ...)
  POST /api/v1/slack/interactive — Block Kit button click handler

Slash commands supported:
  /sre investigate <incident_id>      — Start a new investigation
  /sre status <service>              — Get current service status
  /sre handoff [--from X] [--to Y]  — Generate shift handoff brief
  /sre predict <service>             — Run predictive signal detection
  /sre postmortem <incident_id>      — Generate / view postmortem
  /sre help                          — Show available commands

Set env vars: SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET, SLACK_DEFAULT_CHANNEL
"""

from __future__ import annotations

import json
import logging
import os
from typing import Annotated, Any

from fastapi import APIRouter, Form, Header, HTTPException, Request, Response

logger = logging.getLogger("sentinalai.agui.slack")

router = APIRouter(prefix="/api/v1/slack", tags=["slack"])

AGUI_BASE_URL = os.getenv("AGUI_BASE_URL", "http://localhost:8081")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _lazy_bot():
    from supervisor.slack_bot import get_bot
    return get_bot()


def _lazy_formatter():
    from supervisor.slack_bot import SlackFormatter
    return SlackFormatter


async def _verify_or_raise(request: Request) -> None:
    from supervisor.slack_bot import verify_slack_signature
    body = await request.body()
    ts = request.headers.get("X-Slack-Request-Timestamp", "")
    sig = request.headers.get("X-Slack-Signature", "")
    if not verify_slack_signature(body, ts, sig):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")


def _ephemeral(text: str, blocks: list | None = None) -> dict:
    """Slack immediate ephemeral response."""
    resp: dict[str, Any] = {"response_type": "ephemeral", "text": text}
    if blocks:
        resp["blocks"] = blocks
    return resp


def _in_channel(text: str, blocks: list | None = None) -> dict:
    resp: dict[str, Any] = {"response_type": "in_channel", "text": text}
    if blocks:
        resp["blocks"] = blocks
    return resp


# ---------------------------------------------------------------------------
# Event API — URL verification + event routing
# ---------------------------------------------------------------------------

@router.post("/events")
async def slack_events(request: Request):
    """Handle Slack Event API payloads (URL verification and event dispatch)."""
    await _verify_or_raise(request)
    payload = await request.json()

    # URL verification challenge
    if payload.get("type") == "url_verification":
        return {"challenge": payload["challenge"]}

    event = payload.get("event", {})
    event_type = event.get("type", "")
    logger.info("Slack event received: %s", event_type)

    # Ignore bot messages (prevent loops)
    if event.get("bot_id") or event.get("subtype") == "bot_message":
        return Response(status_code=200)

    if event_type == "app_mention":
        await _handle_app_mention(event)

    return Response(status_code=200)


async def _handle_app_mention(event: dict) -> None:
    """Handle @SentinalAI mentions in channels."""
    text = event.get("text", "").lower()
    channel = event.get("channel", "")
    thread_ts = event.get("ts", "")

    bot = _lazy_bot()
    if "investigate" in text:
        # Extract incident ID if present
        words = text.split()
        inc_id = next(
            (w.upper() for w in words if w.upper().startswith("INC")),
            None,
        )
        if inc_id:
            await _start_investigation_async(inc_id, channel, thread_ts)
        else:
            bot.post(
                type("SlackMessage", (), {
                    "text": "Mention an incident ID: @SentinalAI investigate INC-12345",
                    "blocks": [],
                    "channel": channel,
                    "thread_ts": thread_ts,
                })()
            )


# ---------------------------------------------------------------------------
# Slash command handler
# ---------------------------------------------------------------------------

@router.post("/slash")
async def slack_slash(
    request: Request,
    command: Annotated[str, Form()] = "/sre",
    text: Annotated[str, Form()] = "",
    user_name: Annotated[str, Form()] = "",
    user_id: Annotated[str, Form()] = "",
    channel_id: Annotated[str, Form()] = "",
    channel_name: Annotated[str, Form()] = "",
    response_url: Annotated[str, Form()] = "",
):
    """Handle /sre slash commands."""
    await _verify_or_raise(request)

    parts = text.strip().split()
    sub = parts[0].lower() if parts else "help"
    args = parts[1:]

    logger.info("/sre %s args=%s user=%s channel=%s", sub, args, user_name, channel_id)

    if sub == "investigate":
        return await _slash_investigate(args, user_name, channel_id, response_url)
    elif sub == "status":
        return await _slash_status(args, user_name, channel_id)
    elif sub == "handoff":
        return await _slash_handoff(args, user_name, channel_id)
    elif sub == "predict":
        return await _slash_predict(args, user_name, channel_id)
    elif sub == "postmortem":
        return await _slash_postmortem(args, user_name, channel_id)
    else:
        return _slash_help(user_name)


async def _slash_investigate(args: list[str], user: str, channel: str, response_url: str) -> dict:
    if not args:
        return _ephemeral("Usage: `/sre investigate <incident_id>`")

    incident_id = args[0].upper()
    Formatter = _lazy_formatter()

    # Post an immediate acknowledgment (Slack requires response within 3s)
    ack_msg = Formatter.investigation_started(
        incident_id=incident_id,
        investigation_id="pending",
        service="(detecting...)",
        severity="high",
        description=f"Investigation requested by @{user}",
        channel=channel,
    )
    bot = _lazy_bot()
    result = bot.post(ack_msg, channel=channel)
    thread_ts = result.get("ts", "")

    # Fire investigation in background (don't block Slack's 3s window)
    import asyncio
    asyncio.create_task(
        _start_investigation_async(incident_id, channel, thread_ts, user)
    )

    return Response(status_code=200)


async def _start_investigation_async(
    incident_id: str,
    channel: str,
    thread_ts: str,
    user: str = "",
) -> None:
    """Kick off investigation via the AGUI REST API and post updates to Slack."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{AGUI_BASE_URL}/api/v1/investigations",
                json={"incident_id": incident_id, "priority": "high"},
            )
            if resp.status_code != 200:
                logger.error("Failed to start investigation: %s", resp.text)
                return

            data = resp.json()
            investigation_id = data.get("investigation_id", "unknown")

            # Subscribe to events and post progress updates
            Formatter = _lazy_formatter()
            bot = _lazy_bot()

            async with client.stream(
                "GET",
                f"{AGUI_BASE_URL}/api/v1/investigations/{investigation_id}/events",
            ) as stream:
                async for line in stream.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[5:].strip())
                        event_type = event.get("event_type", "")

                        if event_type == "incident.classified":
                            payload = event.get("payload", {})
                            progress_msg = Formatter.investigation_progress(
                                incident_id=incident_id,
                                investigation_id=investigation_id,
                                service=payload.get("service", "unknown"),
                                phase="collecting_evidence",
                                tool_calls_made=0,
                                budget=20,
                                channel=channel,
                                thread_ts=thread_ts,
                            )
                            bot.post(progress_msg, channel=channel)

                        elif event_type == "investigation.completed":
                            payload = event.get("payload", {})
                            result = payload.get("result", {})
                            rca_msg = Formatter.rca_complete(
                                incident_id=incident_id,
                                investigation_id=investigation_id,
                                service=result.get("affected_service", "unknown"),
                                severity=result.get("severity", "high"),
                                root_cause=result.get("root_cause", "Unknown"),
                                confidence=result.get("confidence", 0),
                                reasoning=result.get("reasoning", ""),
                                blast_radius_level=result.get("blast_radius", {}).get("risk_tier", "unknown"),
                                blast_radius_safe=result.get("blast_radius", {}).get("safe_to_auto_apply", False),
                                proposed_fix=result.get("remediation", {}).get("permanent_fix", ""),
                                immediate_actions=result.get("remediation", {}).get("immediate_actions", []),
                                elapsed_seconds=result.get("elapsed_ms", 0) // 1000,
                                channel=channel,
                                thread_ts=thread_ts,
                            )
                            bot.post(rca_msg, channel=channel)
                            break

                    except (json.JSONDecodeError, KeyError):
                        continue
    except Exception as exc:
        logger.exception("Error during async investigation %s: %s", incident_id, exc)


async def _slash_status(args: list[str], user: str, channel: str) -> dict:
    if not args:
        return _ephemeral("Usage: `/sre status <service-name>`")

    service = args[0]
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{AGUI_BASE_URL}/api/v1/investigations",
                params={"service": service, "limit": 3},
            )
            investigations = resp.json().get("investigations", [])
    except Exception:
        investigations = []

    if not investigations:
        return _ephemeral(f"No recent investigations found for `{service}`.")

    lines = []
    for inv in investigations:
        conf = inv.get("confidence", 0)
        status = inv.get("status", "unknown")
        rc = inv.get("root_cause", "—")
        iid = inv.get("incident_id", "?")
        lines.append(f"• `{iid}` [{status.upper()}] — {rc[:60]} ({conf}% conf)")

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"Status: {service}", "emoji": False}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"Last {len(investigations)} investigations | Requested by @{user}"}],
        },
    ]
    return _in_channel(f"Status for {service}:", blocks=blocks)


async def _slash_handoff(args: list[str], user: str, channel: str) -> dict:
    """Generate a shift handoff brief and post it to the channel."""
    outgoing = user
    incoming = "incoming-sre"
    for i, arg in enumerate(args):
        if arg == "--from" and i + 1 < len(args):
            outgoing = args[i + 1]
        if arg == "--to" and i + 1 < len(args):
            incoming = args[i + 1]

    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{AGUI_BASE_URL}/api/v1/handoff",
                json={"outgoing_engineer": outgoing, "incoming_engineer": incoming},
            )
            brief = resp.json()
    except Exception as exc:
        logger.exception("Failed to generate handoff: %s", exc)
        return _ephemeral("Failed to generate handoff brief. Is the agent running?")

    Formatter = _lazy_formatter()
    msg = Formatter.shift_handoff(brief_dict=brief, channel=channel)
    bot = _lazy_bot()
    bot.post(msg, channel=channel)
    return Response(status_code=200)


async def _slash_predict(args: list[str], user: str, channel: str) -> dict:
    if not args:
        return _ephemeral("Usage: `/sre predict <service-name>`")

    service = args[0]
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{AGUI_BASE_URL}/api/v1/intelligence/predict",
                params={"service": service},
            )
            alerts = resp.json().get("alerts", [])
    except Exception as exc:
        logger.exception("Failed to run prediction for %s: %s", service, exc)
        return _ephemeral(f"Prediction failed for `{service}`. Is the sentinel loop running?")

    if not alerts:
        return _in_channel(f"`{service}` — No anomalous signals detected. All metrics within normal range.")

    Formatter = _lazy_formatter()
    bot = _lazy_bot()
    for alert in alerts[:3]:
        msg = Formatter.proactive_alert(
            service=service,
            metric_name=alert.get("metric_name", "unknown"),
            current_value=alert.get("current_value", 0),
            threshold=alert.get("threshold", 100),
            urgency=alert.get("urgency", "WARNING"),
            trend_direction=alert.get("trend_direction", "rising"),
            minutes_to_breach=alert.get("minutes_to_breach"),
            recommended_action=alert.get("recommended_action", ""),
            channel=channel,
        )
        bot.post(msg, channel=channel)
    return Response(status_code=200)


async def _slash_postmortem(args: list[str], user: str, channel: str) -> dict:
    if not args:
        return _ephemeral("Usage: `/sre postmortem <incident_id>`")

    incident_id = args[0].upper()
    try:
        import httpx
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{AGUI_BASE_URL}/api/v1/postmortem",
                json={"incident_id": incident_id},
            )
            pm = resp.json()
    except Exception as exc:
        logger.exception("Failed to generate postmortem for %s: %s", incident_id, exc)
        return _ephemeral(f"Failed to generate postmortem for `{incident_id}`.")

    Formatter = _lazy_formatter()
    msg = Formatter.postmortem_ready(
        incident_id=incident_id,
        service=pm.get("affected_service", "unknown"),
        duration_minutes=pm.get("duration_minutes", 0),
        action_item_count=len(pm.get("action_items", [])),
        report_url=f"{AGUI_BASE_URL}/postmortems/{pm.get('report_id', '')}",
        channel=channel,
    )
    bot = _lazy_bot()
    bot.post(msg, channel=channel)
    return Response(status_code=200)


def _slash_help(user: str) -> dict:
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "SentinalAI — Commands", "emoji": False}},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*/sre investigate <INC-ID>* — Start autonomous root cause analysis\n"
                    "*/sre status <service>* — Show recent investigation history for a service\n"
                    "*/sre handoff [--from X] [--to Y]* — Generate shift handoff intelligence brief\n"
                    "*/sre predict <service>* — Run predictive signal detection (pre-incident)\n"
                    "*/sre postmortem <INC-ID>* — Generate blameless postmortem draft\n"
                    "*/sre help* — Show this message"
                ),
            },
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"SentinalAI | Autonomous SRE Intelligence | Hello @{user}"}],
        },
    ]
    return _ephemeral("SentinalAI Commands", blocks=blocks)


# ---------------------------------------------------------------------------
# Interactive actions (button clicks)
# ---------------------------------------------------------------------------

@router.post("/interactive")
async def slack_interactive(
    request: Request,
    payload: Annotated[str, Form()] = "",
):
    """Handle Slack Block Kit button clicks and interactive actions."""
    await _verify_or_raise(request)

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    actions = data.get("actions", [])
    user = data.get("user", {}).get("username", "unknown")
    channel = data.get("channel", {}).get("id", "")
    thread_ts = data.get("message", {}).get("thread_ts", "")

    for action in actions:
        action_id = action.get("action_id", "")
        value = action.get("value", "")
        logger.info("Slack action: %s value=%s user=%s", action_id, value, user)

        if action_id == "approve_fix":
            await _handle_approve_fix(value, user, channel, thread_ts)
        elif action_id == "view_evidence":
            await _handle_view_evidence(value, user, channel, thread_ts)
        elif action_id == "override_rca":
            await _handle_override(value, user, channel, thread_ts)
        elif action_id == "escalate":
            await _handle_escalate(value, user, channel, thread_ts)
        elif action_id == "acknowledge_alert":
            await _handle_acknowledge(value, user, channel)
        elif action_id == "investigate_now":
            svc = value.split("|")[0] if "|" in value else value
            import asyncio
            asyncio.create_task(
                _start_investigation_async(f"ONDEMAND-{svc}", channel, thread_ts, user)
            )
        elif action_id == "review_postmortem":
            await _handle_review_postmortem(value, user, channel)

    return Response(status_code=200)


async def _handle_approve_fix(investigation_id: str, user: str, channel: str, thread_ts: str) -> None:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{AGUI_BASE_URL}/api/v1/control",
                json={
                    "investigation_id": investigation_id,
                    "action": "approve",
                    "actor": user,
                    "reason": "Approved via Slack",
                },
            )
        bot = _lazy_bot()
        bot.post(
            type("M", (), {
                "text": f"Fix approved by @{user}. Applying remediation...",
                "blocks": [{"type": "section", "text": {"type": "mrkdwn",
                    "text": f"Fix approved by *@{user}*. SentinalAI is applying the remediation and will verify."}
                }],
                "channel": channel,
                "thread_ts": thread_ts,
            })(),
            channel=channel,
        )
    except Exception as exc:
        logger.exception("Approve fix failed: %s", exc)


async def _handle_view_evidence(investigation_id: str, user: str, channel: str, thread_ts: str) -> None:
    url = f"{AGUI_BASE_URL}/investigations/{investigation_id}"
    bot = _lazy_bot()
    bot.post(
        type("M", (), {
            "text": f"Evidence trail: {url}",
            "blocks": [{"type": "section", "text": {"type": "mrkdwn",
                "text": f"<{url}|View full evidence trail and hypothesis scoring> for `{investigation_id}`"}
            }],
            "channel": channel,
            "thread_ts": thread_ts,
        })(),
        channel=channel,
    )


async def _handle_override(investigation_id: str, user: str, channel: str, thread_ts: str) -> None:
    bot = _lazy_bot()
    bot.post(
        type("M", (), {
            "text": "Override requested — open the UI to provide your root cause",
            "blocks": [{"type": "section", "text": {"type": "mrkdwn",
                "text": f"<{AGUI_BASE_URL}/investigations/{investigation_id}|Open investigation> "
                        f"to override the AI root cause determination."}
            }],
            "channel": channel,
            "thread_ts": thread_ts,
        })(),
        channel=channel,
    )


async def _handle_escalate(investigation_id: str, user: str, channel: str, thread_ts: str) -> None:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{AGUI_BASE_URL}/api/v1/control",
                json={
                    "investigation_id": investigation_id,
                    "action": "escalate",
                    "actor": user,
                    "reason": "Escalated via Slack",
                },
            )
    except Exception:
        pass
    bot = _lazy_bot()
    bot.post(
        type("M", (), {
            "text": f"Escalated by @{user}",
            "blocks": [{"type": "section", "text": {"type": "mrkdwn",
                "text": f"Investigation `{investigation_id}` escalated by *@{user}*. On-call lead notified."}
            }],
            "channel": channel,
            "thread_ts": thread_ts,
        })(),
        channel=channel,
    )


async def _handle_acknowledge(value: str, user: str, channel: str) -> None:
    parts = value.split("|")
    service = parts[0] if parts else value
    bot = _lazy_bot()
    bot.post(
        type("M", (), {
            "text": f"Alert acknowledged by @{user}",
            "blocks": [{"type": "section", "text": {"type": "mrkdwn",
                "text": f"Pre-incident alert for `{service}` acknowledged by *@{user}*. SentinalAI will continue monitoring."}
            }],
            "channel": channel,
            "thread_ts": "",
        })(),
        channel=channel,
    )


async def _handle_review_postmortem(incident_id: str, user: str, channel: str) -> None:
    url = f"{AGUI_BASE_URL}/postmortems/{incident_id}"
    bot = _lazy_bot()
    bot.post(
        type("M", (), {
            "text": f"Review postmortem: {url}",
            "blocks": [{"type": "section", "text": {"type": "mrkdwn",
                "text": f"<{url}|Review and approve postmortem> for `{incident_id}`"}
            }],
            "channel": channel,
            "thread_ts": "",
        })(),
        channel=channel,
    )
