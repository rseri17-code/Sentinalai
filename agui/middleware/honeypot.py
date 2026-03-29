"""Honeypot middleware for SentinalAI.

Unauthorized visitors (no valid invite token/session) see a convincing but
entirely fabricated SentinalAI dashboard.  The React UI loads normally — all
data is fake, subtly incoherent, and harmless.

Valid visitors (invite link or existing session cookie) pass straight through
to real handlers.

Flow:
  1. Check `sentinalai_session` cookie → HMAC-valid → real app
  2. Check `?invite=<TOKEN>` query param → valid → set cookie, redirect clean
  3. Neither → honeypot: API routes return fake JSON, HTML routes serve the
     React shell (which fetches the fake API data)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import random
import time
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

logger = logging.getLogger(__name__)

# ── Secrets ────────────────────────────────────────────────────────────────────
_SESSION_SECRET = os.getenv("AGUI_SESSION_SECRET", "sentinalai-session-secret-change-me")
_INVITE_SECRET  = os.getenv("AGUI_INVITE_SECRET",  "sentinalai-invite-secret-change-me")
INVITE_TOKENS   = set(os.getenv("AGUI_INVITE_TOKENS", "").split(",")) - {""}

HONEYPOT_ENABLED = os.getenv("AGUI_HONEYPOT", "true").lower() == "true"
SESSION_COOKIE   = "sentinalai_session"
SESSION_TTL      = int(os.getenv("AGUI_SESSION_TTL", str(7 * 24 * 3600)))  # 7 days


# ── HMAC helpers ───────────────────────────────────────────────────────────────

def _sign(value: str, secret: str) -> str:
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()


def _verify(value: str, sig: str, secret: str) -> bool:
    expected = _sign(value, secret)
    return hmac.compare_digest(expected, sig)


def make_session_cookie(actor_id: str) -> str:
    """Return a signed session cookie value: '{actor_id}:{ts}:{sig}'."""
    ts = str(int(time.time()))
    payload = f"{actor_id}:{ts}"
    sig = _sign(payload, _SESSION_SECRET)
    return f"{payload}:{sig}"


def validate_session_cookie(value: str) -> str | None:
    """Return actor_id if cookie is valid and not expired, else None."""
    try:
        parts = value.split(":")
        if len(parts) != 3:
            return None
        actor_id, ts, sig = parts
        payload = f"{actor_id}:{ts}"
        if not _verify(payload, sig, _SESSION_SECRET):
            return None
        age = int(time.time()) - int(ts)
        if age > SESSION_TTL:
            return None
        return actor_id
    except Exception:
        return None


def validate_invite_token(token: str) -> bool:
    """Return True if token is a valid invite."""
    # Static tokens from env
    if token in INVITE_TOKENS:
        return True
    # HMAC-signed dynamic tokens: 'invite:{ts}:{sig}'
    try:
        parts = token.split(":")
        if len(parts) == 3 and parts[0] == "invite":
            _, ts, sig = parts
            payload = f"invite:{ts}"
            if not _verify(payload, sig, _INVITE_SECRET):
                return False
            age = int(time.time()) - int(ts)
            return age < SESSION_TTL
    except Exception:
        pass
    return False


def generate_invite_token() -> str:
    """Generate a new time-limited HMAC invite token."""
    ts = str(int(time.time()))
    payload = f"invite:{ts}"
    sig = _sign(payload, _INVITE_SECRET)
    return f"invite:{ts}:{sig}"


# ── Fake data factory ──────────────────────────────────────────────────────────

_FAKE_SERVICES = ["payments-api", "auth-service", "order-processor", "user-service", "api-gateway"]
_FAKE_TYPES    = ["error_spike", "latency", "oomkill", "timeout", "saturation"]
_FAKE_SEVERITIES = ["critical", "major", "warning"]
_FAKE_RC = [
    "Downstream database connection pool exhausted after config push",
    "Memory leak in v2.4.1 introduced by last deployment",
    "TLS certificate expired on internal service mesh",
    "Cache invalidation storm from misconfigured TTL",
    "Thread pool exhaustion under increased load",
]


def _fake_investigation(seed: int | None = None) -> dict:
    rng = random.Random(seed or time.time())
    inc_id = f"INC{rng.randint(10000, 99999)}"
    svc = rng.choice(_FAKE_SERVICES)
    return {
        "investigation_id": f"inv-{inc_id.lower()}-honeypot",
        "incident_id": inc_id,
        "affected_service": svc,
        "incident_type": rng.choice(_FAKE_TYPES),
        "severity": rng.choice(_FAKE_SEVERITIES),
        "status": "completed",
        "confidence": rng.randint(65, 72) / 100,
        "root_cause": rng.choice(_FAKE_RC),
        "started_at": "2024-01-15T10:00:00Z",
        "completed_at": "2024-01-15T10:04:37Z",
        "hypotheses": [
            {"name": "config_drift", "confidence": 0.68, "rank": 1},
            {"name": "traffic_surge", "confidence": 0.31, "rank": 2},
        ],
        "memory_matches": [],
        "receipts": [],
    }


def _fake_incidents() -> list[dict]:
    return [_fake_investigation(seed=i) for i in range(1, 6)]


# ── Honeypot response router ───────────────────────────────────────────────────

_HONEYPOT_ROUTES: dict[str, Callable[[], Response]] = {}


def _honeypot_response(path: str) -> Response | None:
    """Return a fake response for the given API path, or None if not intercepted."""
    if path.startswith("/api/v1/investigations"):
        parts = path.split("/")
        if len(parts) == 4:
            # List investigations
            items = _fake_incidents()
            return JSONResponse({
                "investigations": items,
                "total": len(items),
                "page": 1,
                "limit": 50,
            })
        if len(parts) >= 5:
            inv_id = parts[4]
            seed = abs(hash(inv_id)) % 10000
            return JSONResponse(_fake_investigation(seed))

    if path.startswith("/api/v1/incidents"):
        return JSONResponse({
            "incidents": [{"incident_id": f"INC{i}", "summary": "Synthetic event", "severity": "warning"} for i in range(10000, 10003)],
            "total": 3,
        })

    if path.startswith("/api/v1/learning"):
        return JSONResponse({
            "experience_store": {"total": 47, "avg_quality_score": 0.69, "by_incident_type": {"error_spike": 18, "latency": 14, "oomkill": 15}},
            "strategy_evolution": {"total_updates": 312, "evolved_incident_types": 3, "top_evolved_steps": []},
            "self_learning_active": True,
        })

    if path.startswith("/api/v1/status"):
        return JSONResponse({
            "service": "agui-bff",
            "version": "1.0.0",
            "timestamp": time.time(),
            "ws_connections": 1,
            "bus_investigations": 2,
            "bus_history_size": 47,
            "actor": {"id": "viewer", "role": "viewer"},
        })

    if path.startswith("/api/"):
        return JSONResponse({"detail": "Not found"}, status_code=404)

    return None  # Let through (static assets, etc.)


# ── Middleware ─────────────────────────────────────────────────────────────────

class HoneypotMiddleware(BaseHTTPMiddleware):
    """
    Gate all requests behind invite-token / session validation.

    Authorized path:
      1. Cookie `sentinalai_session` present and HMAC-valid → pass through
      2. Query param `?invite=TOKEN` valid → set cookie, redirect to clean URL

    Unauthorized path (honeypot):
      - API routes → realistic fake JSON (200 OK, no auth errors)
      - All other routes → pass through to static file handler
        (React SPA loads normally but fetches the fake API data above)
    """

    async def dispatch(self, request: Request, call_next):
        if not HONEYPOT_ENABLED:
            return await call_next(request)

        path = request.url.path

        # Always allow: health, invite redemption, and WebSocket (handle auth separately)
        if path in ("/ping", "/favicon.ico") or path.startswith("/ws/"):
            return await call_next(request)

        # ── 1. Check session cookie ────────────────────────────────────────
        cookie_val = request.cookies.get(SESSION_COOKIE, "")
        actor = validate_session_cookie(cookie_val) if cookie_val else None
        if actor:
            return await call_next(request)

        # ── 2. Check invite token in query param ───────────────────────────
        invite = request.query_params.get("invite", "")
        if invite and validate_invite_token(invite):
            actor_id = f"guest-{abs(hash(invite)) % 100000:05d}"
            session = make_session_cookie(actor_id)
            # Redirect to clean URL (strip ?invite=...)
            clean_url = str(request.url).split("?")[0]
            resp = RedirectResponse(url=clean_url, status_code=302)
            resp.set_cookie(
                SESSION_COOKIE,
                session,
                max_age=SESSION_TTL,
                httponly=True,
                samesite="lax",
                secure=request.url.scheme == "https",
            )
            logger.info("Invite redeemed, session created for %s", actor_id)
            return resp

        # ── 3. No valid auth → honeypot ────────────────────────────────────
        logger.info("Honeypot: unauthenticated request to %s from %s",
                    path, request.client.host if request.client else "?")

        fake = _honeypot_response(path)
        if fake is not None:
            return fake

        # Non-API routes (static files, React SPA) → serve normally.
        # The React app will load, fetch the fake API data above, and look real.
        return await call_next(request)
