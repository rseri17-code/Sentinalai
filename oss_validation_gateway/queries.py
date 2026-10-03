"""Translate worker query hints into LogQL / PromQL.

Workers pass Splunk-ish strings (``timeout payment-service``) and metric
hints (``response_time_ms``). The shim maps those onto the demo seed
metric names and a Loki line filter.

Log and query matching is case-insensitive. Each query keyword is a hint.
A hint with a synonym set matches any phrase in that set; other hints
match themselves. Every hint is kept (the translator used to emit only
the first keyword) and Loki must match all of them.
"""

from __future__ import annotations

import re

# Seed exporter metric names (see deploy/oss-validation/seed/seed.py).
METRIC_PROMQL: dict[str, str] = {
    "response_time_ms": 'demo_latency_p95_ms{{service="{service}"}}',
    "memory_usage_bytes": 'process_resident_memory_bytes{{service="{service}"}}',
    "cpu_usage_percent": 'demo_saturation_pct{{service="{service}"}}',
    "error_rate": 'demo_error_rate{{service="{service}"}}',
    "request_rate": 'demo_request_rate{{service="{service}"}}',
}

GOLDEN_SIGNAL_PROMQL: dict[str, str] = {
    "latency_p95": 'demo_latency_p95_ms{{service="{service}"}}',
    "latency_baseline_p95": 'demo_latency_baseline_p95_ms{{service="{service}"}}',
    "latency_p50": 'demo_latency_p50_ms{{service="{service}"}}',
    "latency_p99": 'demo_latency_p99_ms{{service="{service}"}}',
    "error_rate": 'demo_error_rate{{service="{service}"}}',
    "request_rate": 'demo_request_rate{{service="{service}"}}',
    "saturation": 'demo_saturation_pct{{service="{service}"}}',
}

# Synonym sets for query hints.
#
# General operational vocabulary. These phrases are not taken from the demo
# seed, and none was added so a particular incident id would match.
#
# timeout
#   "timed out" — past tense written by HTTP clients, DB drivers, and
#   proxies when a call exceeds its deadline (libcurl, JDBC, Go net/http).
#   "deadline exceeded" — gRPC status DEADLINE_EXCEEDED and Go's
#   context.DeadlineExceeded, copied into service logs as that phrase.
# pool
#   "connection pool" — the object those clients exhaust (HikariCP, JDBC,
#   psycopg, urllib3).
#   "pool exhausted" — the usual exhaustion sentence.
#   "pool.exhausted" — the same words with a dotted separator, the form
#   structured logs and metric names use.
# error
#   "exception" — Java, Python, and .NET name the same failure an
#   exception when the line does not say "error".
QUERY_HINT_SYNONYMS: dict[str, tuple[str, ...]] = {
    "timeout": ("timeout", "timed out", "deadline exceeded"),
    "pool": ("pool", "connection pool", "pool exhausted", "pool.exhausted"),
    "error": ("error", "exception"),
}

_IDENT = re.compile(r"^[A-Za-z0-9_.:-]+$")
_BOOLEAN_WORDS = frozenset({"or", "and", "not"})
# RE2 (Loki) rejects unknown escapes such as backslash-space. Escape only
# metacharacters. Leave spaces so phrases stay readable line filters.
_RE2_META = re.compile(r"([.^$*+?{}\[\]\\|()])")


def _index_synonyms() -> dict[str, tuple[str, ...]]:
    """Map a single-token hint or synonym to its full set.

    Multi-word phrases stay inside the set. They are not extra tokens.
    """
    index: dict[str, tuple[str, ...]] = {}
    for hint, terms in QUERY_HINT_SYNONYMS.items():
        canonical = tuple(terms)
        index[hint.lower()] = canonical
        for term in terms:
            token = term.lower()
            if any(ch.isspace() for ch in token):
                continue
            index.setdefault(token, canonical)
    return index


_SYNONYM_BY_TOKEN = _index_synonyms()


def _safe_service(service: str | None) -> str:
    """Return a Loki/PromQL label value, or raise if it is unusable.

    Empty, missing, and whitespace-only names are rejected. A value that
    is not a single label token (quotes, spaces, or other punctuation) is
    rejected. There is no fallback service.
    """
    if service is None:
        raise ValueError("service is empty")
    value = service.strip()
    if not value:
        raise ValueError("service is empty")
    if not _IDENT.match(value):
        raise ValueError(f"service is invalid: {value!r}")
    return value


def _quote_logql(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _terms_for_hint(keyword: str) -> tuple[str, ...]:
    terms = _SYNONYM_BY_TOKEN.get(keyword.lower())
    if terms is not None:
        return terms
    return (keyword,)


def _escape_re2(value: str) -> str:
    return _RE2_META.sub(r"\\\1", value)


def _logql_filter(terms: tuple[str, ...]) -> str:
    """Case-insensitive RE2 filter. Phrases in one hint are OR'd."""
    parts = [_escape_re2(term) for term in terms]
    body = parts[0] if len(parts) == 1 else "(?:" + "|".join(parts) + ")"
    return '|~ "(?i)%s"' % _quote_logql(body)


def splunk_query_to_logql(query: str, service: str | None = None) -> str:
    """Map a playbook ``query_hint`` to a Loki LogQL selector.

    The service token is dropped, case-insensitively, when the playbook
    already interpolated it. Punctuation other than ``_.:-`` is stripped.
    ``or``, ``and``, and ``not`` are not hints.

    Every remaining keyword is a hint. The previous translator kept only
    the first one. Each hint becomes its own ``|~ "(?i)..."`` filter, and
    Loki ANDs those filters: a line must satisfy every hint. Within a
    hint, the synonym set is OR'd. A token that is itself a listed
    synonym (for example ``exception`` or ``pool.exhausted``) uses that
    hint's full set. A token with no set matches itself only. Duplicate
    hints that share a set are emitted once.

    An empty keyword list selects the service stream and adds no filter.
    ``service`` must be a non-empty label token; see ``_safe_service``.
    """
    svc = _safe_service(service)
    raw = (query or "").strip()
    tokens = [t for t in re.split(r"\s+", raw) if t and t.lower() != svc.lower()]
    keywords: list[str] = []
    seen_tokens: set[str] = set()
    for token in tokens:
        cleaned = re.sub(r"[^A-Za-z0-9_.:-]+", "", token)
        if not cleaned or cleaned.lower() in _BOOLEAN_WORDS:
            continue
        marker = cleaned.lower()
        if marker in seen_tokens:
            continue
        seen_tokens.add(marker)
        keywords.append(cleaned)
    selector = '{service="%s"}' % _quote_logql(svc)
    if not keywords:
        return selector
    filters: list[str] = []
    seen_sets: set[tuple[str, ...]] = set()
    for keyword in keywords:
        terms = _terms_for_hint(keyword)
        if terms in seen_sets:
            continue
        seen_sets.add(terms)
        filters.append(_logql_filter(terms))
    return selector + " " + " ".join(filters)


def metric_hint_to_promql(metric: str | None, service: str | None = None) -> str:
    """Map a metric hint to PromQL. The hint lookup is case-insensitive."""
    svc = _safe_service(service)
    key = (metric or "").strip().lower() or "request_rate"
    template = METRIC_PROMQL.get(key, METRIC_PROMQL["request_rate"])
    return template.format(service=svc)


def golden_signal_promql(signal: str, service: str | None = None) -> str:
    """Map a golden-signal name to PromQL. The name lookup is case-insensitive."""
    svc = _safe_service(service)
    template = GOLDEN_SIGNAL_PROMQL.get((signal or "").strip().lower())
    if template is None:
        raise KeyError(signal)
    return template.format(service=svc)
