"""Dependency domain failure detection for SentinalAI.

Detects which failure domain is active based on evidence and error patterns,
then returns targeted evidence query recommendations for self_critique gap-filling.

Five dependency cognition domains:
  CERTIFICATE  — TLS/SSL cert expiry, handshake failures, renewal failures
  IDENTITY     — LDAP/SSO/OAuth/PingFederate auth failures, token expiry
  CREDENTIAL   — CyberArk/vault CPM rotation, stale passwords, DB auth failures
  DNS_AUTH     — DNS resolution failures, NXDOMAIN, AppViewX DNS
  DB_AUTH      — Database authentication errors, connection pool exhaustion from auth

Example cascade:
  CPM rotation failed → stale password → DB auth failure → connection exhaustion → timeout

Output is a list of DomainHypothesis objects, each with:
  - domain: detected failure domain
  - confidence: how strongly evidence points to this domain
  - indicators: which patterns triggered detection
  - gap_queries: targeted evidence queries to plug into self_critique
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger("sentinalai.dependency_domain_detector")


class FailureDomain(str, Enum):
    CERTIFICATE = "CERTIFICATE"
    IDENTITY    = "IDENTITY"
    CREDENTIAL  = "CREDENTIAL"
    DNS_AUTH    = "DNS_AUTH"
    DB_AUTH     = "DB_AUTH"
    UNKNOWN     = "UNKNOWN"


@dataclass
class DomainHypothesis:
    domain: FailureDomain
    confidence: float          # 0.0–1.0
    indicators: list[str]      # which patterns triggered
    gap_queries: list[dict]    # self_critique-compatible query dicts
    cascade_chain: list[str] = field(default_factory=list)  # inferred cascade


# ---------------------------------------------------------------------------
# Pattern definitions per domain
# ---------------------------------------------------------------------------

_CERT_PATTERNS = [
    (r'\bcertificate.expir|\bssl.cert|\bx509\b|\btls.handshake.fail|\bssl.error\b', 0.90),
    (r'\bcert.renew|\bcert.rotation|\blet.?s.?encrypt|\bcertmgr\b', 0.80),
    (r'\bhandshake.timeout|\bpeer.certificate\b|\bca.bundle\b|\bcrl.check\b', 0.75),
    (r'\bpkix.path\b|\buntrusted.cert\b|\bself.signed\b|\bcert.chain\b', 0.85),
    (r'\bAppViewX\b|\bvenafi\b|\bcertificate.authority\b', 0.70),
    (r'\bcertificate.*expired\b|\bssl.*expired\b', 0.95),
    (r'\bNotBefore\b|\bNotAfter\b|\bvalidity.period\b', 0.80),
]

_IDENTITY_PATTERNS = [
    (r'\bPingFederate\b|\bping.fed\b|\bOAuth.fail|\bOIDC.fail\b', 0.90),
    (r'\bLDAP.fail|\bAD.auth|\bActive.Directory.fail|\bkerberos.fail\b', 0.85),
    (r'\bSSO.fail|\bsaml.fail|\btoken.expir|\baccess.token.invalid\b', 0.85),
    (r'\bidentity.provider.unavail|\bIdP.fail|\bfederation.fail\b', 0.90),
    (r'\bjwt.expir|\bjwt.invalid|\bbearer.token.fail\b', 0.80),
    (r'\bauth.service.unavail|\bunauthorized.*401\b|\b403.forbidden\b', 0.65),
    (r'\bservice.account.expir|\bservice.principal.fail\b', 0.80),
]

_CREDENTIAL_PATTERNS = [
    (r'\bCyberArk\b|\bCPM.rotation|\bpassword.rotation.fail\b', 0.95),
    (r'\bvault.fail|\bHashicorp.Vault|\bsecret.rotation\b', 0.85),
    (r'\bstale.password|\bpassword.expired\b|\bold.password\b', 0.90),
    (r'\bcredential.fail|\bauthentication.fail.*password\b', 0.80),
    (r'\bpassword.mismatch|\binvalid.credentials\b|\baccess.denied\b', 0.70),
    (r'\brotation.fail|\bCPM.error\b|\bcredential.store.unavail\b', 0.90),
    (r'\bCyberArk.*fail|\bPAM.fail\b|\bprivileged.account\b', 0.90),
]

_DNS_PATTERNS = [
    (r'\bDNS.resolution.fail|\bDNS.lookup.fail|\bNXDOMAIN\b', 0.95),
    (r'\bname.resolution.fail|\bgetaddrinfo.fail\b|\bhost.not.found\b', 0.90),
    (r'\bDNS.timeout|\bDNS.unreachable|\bname.server.fail\b', 0.85),
    (r'\bAppViewX.*DNS|\bDNS.*AppViewX\b|\bdns.update.fail\b', 0.80),
    (r'\bResolvConf\b|\b/etc/resolv.conf\b|\bsearch.domain.fail\b', 0.75),
    (r'\bCNAME.loop\b|\bDNS.propagation\b|\bttl.expir\b', 0.70),
    (r'\bservice.discovery.fail|\bconsul.fail|\bconsul.*dns\b', 0.75),
]

_DB_AUTH_PATTERNS = [
    (r'\bconnection.pool.exhaust|\bmax.connections.reached\b|\bToo.many.connections\b', 0.90),
    (r'\bDB.auth.fail|\bdatabase.auth|\bOracle.auth.fail\b', 0.90),
    (r'\bpsycopg2.*authentication\b|\bmysql.*access.denied\b|\bORA-01017\b', 0.95),
    (r'\bconnection.refused.*db\b|\bdb.connection.fail\b|\bSQL.Server.*auth\b', 0.85),
    (r'\bJDBC.*auth\b|\bHikariCP.*fail\b|\bc3p0.*fail\b', 0.85),
    (r'\binvalid.username.*db\b|\bdb.password.fail\b|\bpg.hba.conf\b', 0.90),
    (r'\bconnection.pool.*timeout\b|\bwait.timeout.*db\b', 0.75),
]

_ALL_DOMAINS = [
    (FailureDomain.CERTIFICATE, _CERT_PATTERNS),
    (FailureDomain.IDENTITY,    _IDENTITY_PATTERNS),
    (FailureDomain.CREDENTIAL,  _CREDENTIAL_PATTERNS),
    (FailureDomain.DNS_AUTH,    _DNS_PATTERNS),
    (FailureDomain.DB_AUTH,     _DB_AUTH_PATTERNS),
]

# Pre-compile all patterns
_COMPILED_DOMAINS: list[tuple[FailureDomain, list[tuple[re.Pattern, float]]]] = [
    (domain, [(re.compile(p, re.IGNORECASE), w) for p, w in patterns])
    for domain, patterns in _ALL_DOMAINS
]


# ---------------------------------------------------------------------------
# Gap query templates per domain
# ---------------------------------------------------------------------------

_GAP_QUERIES: dict[FailureDomain, list[dict]] = {
    FailureDomain.CERTIFICATE: [
        {"worker": "log_worker",    "action": "search_logs",
         "params": {"query": "ssl OR tls OR certificate OR x509 OR handshake", "source": "cert_logs"}},
        {"worker": "change_worker", "action": "get_config_changes",
         "params": {"filter": "certificate OR cert-rotation OR ssl"}},
        {"worker": "event_worker",  "action": "get_events",
         "params": {"filter": "CertificateExpired OR TLSError OR HandshakeFailed"}},
    ],
    FailureDomain.IDENTITY: [
        {"worker": "log_worker",    "action": "search_logs",
         "params": {"query": "PingFederate OR LDAP OR SSO OR OAuth OR SAML OR token", "source": "auth_logs"}},
        {"worker": "log_worker",    "action": "search_logs",
         "params": {"query": "401 OR 403 OR unauthorized OR forbidden", "source": "application_logs"}},
        {"worker": "event_worker",  "action": "get_events",
         "params": {"filter": "AuthenticationFailed OR IdentityProviderError"}},
    ],
    FailureDomain.CREDENTIAL: [
        {"worker": "log_worker",    "action": "search_logs",
         "params": {"query": "CyberArk OR CPM OR password-rotation OR credential", "source": "cyberark_logs"}},
        {"worker": "log_worker",    "action": "search_logs",
         "params": {"query": "password expired OR stale credential OR invalid credentials", "source": "application_logs"}},
        {"worker": "change_worker", "action": "get_config_changes",
         "params": {"filter": "password-rotation OR credential-rotation OR vault"}},
    ],
    FailureDomain.DNS_AUTH: [
        {"worker": "log_worker",    "action": "search_logs",
         "params": {"query": "NXDOMAIN OR DNS OR name resolution OR getaddrinfo", "source": "dns_logs"}},
        {"worker": "log_worker",    "action": "search_logs",
         "params": {"query": "AppViewX OR dns-update OR service-discovery", "source": "infra_logs"}},
        {"worker": "event_worker",  "action": "get_events",
         "params": {"filter": "DNSResolutionFailed OR NameResolutionError"}},
    ],
    FailureDomain.DB_AUTH: [
        {"worker": "log_worker",    "action": "search_logs",
         "params": {"query": "connection pool OR max connections OR DB auth OR ORA-", "source": "db_logs"}},
        {"worker": "metric_worker", "action": "query_metrics",
         "params": {"metric": "db.connections.active OR db.pool.size OR db.pool.wait"}},
        {"worker": "log_worker",    "action": "search_logs",
         "params": {"query": "authentication failed OR access denied OR invalid password", "source": "db_logs"}},
    ],
}


# ---------------------------------------------------------------------------
# Cascade chain inferences
# ---------------------------------------------------------------------------

_CASCADE_CHAINS: dict[FailureDomain, list[str]] = {
    FailureDomain.CERTIFICATE: [
        "TLS cert expired/invalid",
        "Handshake failure",
        "Connection refused",
        "Service unavailable",
    ],
    FailureDomain.IDENTITY: [
        "Identity provider failure",
        "Authentication token invalid/expired",
        "Authorization denied (401/403)",
        "Service requests rejected",
    ],
    FailureDomain.CREDENTIAL: [
        "CPM rotation failed / vault unavailable",
        "Stale/expired password in use",
        "Database authentication failure",
        "Connection pool exhaustion",
        "Application timeout / cascade",
    ],
    FailureDomain.DNS_AUTH: [
        "DNS lookup failure",
        "Service endpoint unresolvable",
        "TCP connection cannot be established",
        "Cascade to dependent services",
    ],
    FailureDomain.DB_AUTH: [
        "DB authentication failure",
        "Connection pool exhaustion",
        "Query timeouts",
        "Application error cascade",
    ],
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect(
    evidence: dict,
    root_cause: str = "",
    incident_type: str = "unknown",
    min_confidence: float = 0.40,
) -> list[DomainHypothesis]:
    """Detect active failure domains from evidence and root cause text.

    Returns a ranked list of DomainHypothesis objects (highest confidence first).
    Only domains with confidence >= min_confidence are returned.

    Parameters
    ----------
    evidence:       Raw evidence dict from investigation
    root_cause:     RCA root cause string
    incident_type:  Incident type classification
    min_confidence: Minimum confidence threshold to include a domain (default 0.40)
    """
    combined_text = _flatten(evidence, root_cause, incident_type)

    results: list[DomainHypothesis] = []

    for domain, compiled_patterns in _COMPILED_DOMAINS:
        best_score = 0.0
        triggered: list[str] = []

        for pattern, weight in compiled_patterns:
            m = pattern.search(combined_text)
            if m:
                triggered.append(m.group(0))
                if weight > best_score:
                    best_score = weight

        if best_score < min_confidence:
            continue

        results.append(DomainHypothesis(
            domain=domain,
            confidence=round(best_score, 3),
            indicators=triggered[:5],   # cap to 5 most relevant
            gap_queries=_GAP_QUERIES.get(domain, []),
            cascade_chain=_CASCADE_CHAINS.get(domain, []),
        ))

    # Sort by confidence descending
    results.sort(key=lambda h: h.confidence, reverse=True)

    if results:
        logger.info(
            "Dependency domain detection: found %d domain(s): %s",
            len(results),
            [f"{h.domain.value}={h.confidence:.2f}" for h in results],
        )
    else:
        logger.debug("Dependency domain detection: no domains above %.2f threshold", min_confidence)

    return results


def get_gap_queries(
    evidence: dict,
    root_cause: str = "",
    incident_type: str = "unknown",
    min_confidence: float = 0.40,
    max_queries: int = 6,
) -> list[dict]:
    """Return self_critique-compatible gap queries for detected domains.

    Merges queries from all detected domains, deduplicates by action+source,
    and caps at max_queries.

    Each returned dict has: {worker, action, params}
    """
    hypotheses = detect(evidence, root_cause, incident_type, min_confidence)

    seen: set[str] = set()
    queries: list[dict] = []

    for hypothesis in hypotheses:
        for q in hypothesis.gap_queries:
            key = f"{q.get('worker')}:{q.get('action')}:{q.get('params', {}).get('source', '')}"
            if key not in seen:
                seen.add(key)
                queries.append(q)
                if len(queries) >= max_queries:
                    return queries

    return queries


def infer_cascade(
    evidence: dict,
    root_cause: str = "",
    incident_type: str = "unknown",
) -> Optional[list[str]]:
    """Return the most likely cascade chain for the primary detected domain.

    Returns None if no domain is detected with sufficient confidence.
    """
    hypotheses = detect(evidence, root_cause, incident_type, min_confidence=0.60)
    if not hypotheses:
        return None
    return hypotheses[0].cascade_chain


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _flatten(evidence: dict, root_cause: str, incident_type: str) -> str:
    """Flatten all inputs to a single string for pattern matching."""
    parts = [root_cause, incident_type]
    for k, v in evidence.items():
        if k.startswith("_"):
            continue
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, (dict, list)):
            parts.append(str(v)[:2000])  # cap large values
    return " ".join(parts)
