"""Translate worker query hints into LogQL / PromQL.

Workers pass Splunk-ish strings (``timeout payment-service``) and metric
hints (``response_time_ms``). The shim maps those onto the demo seed
metric names and a Loki line filter.

Log and query matching is case-insensitive. Each query keyword is a hint.
A hint with a synonym set matches any phrase in that set; other hints
match themselves. A hint of two or three words matches those words in any
order, in one filter. An explicit ``OR`` gives each alternative that same
treatment. Regex metacharacters in a hint are escaped and match literally.
"""

from __future__ import annotations

import itertools
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
_DROPPED_WORDS = frozenset({"and", "not"})
_OR_SPLIT = re.compile(r"(?i)\s+OR\s+")
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
    rejected. A dot is allowed: ``svc.v2`` is one identifier. There is no
    fallback service. The caller interpolates the name into a label
    equality matcher, or escapes it if the name is used as a line filter.
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
    """Escape RE2 metacharacters so the text matches literally.

    ``+ | ( ) [ ] { } ? * ^ $ \\ .`` are escaped. They are not dropped
    and they are not left as operators. Spaces stay spaces so a synonym
    phrase such as ``timed out`` stays that phrase.
    """
    return _RE2_META.sub(r"\\\1", value)


def _hint_pattern(keyword: str) -> str:
    """RE2 body for one hint word. Synonyms inside the word are alternatives.

    Each synonym is escaped before it is interpolated. A multi-word
    synonym stays in the written order; it is one alternative, not a
    further permutation. The permutations below apply to the hint's own
    words.
    """
    parts: list[str] = []
    seen: set[str] = set()
    for term in _terms_for_hint(keyword):
        escaped = _escape_re2(term)
        if escaped in seen:
            continue
        seen.add(escaped)
        parts.append(escaped)
    if len(parts) == 1:
        return parts[0]
    return "(?:" + "|".join(parts) + ")"


def _line_filter(body: str) -> str:
    """One case-insensitive line filter. ``body`` is already escaped."""
    return '|~ "(?i)%s"' % _quote_logql(body)


def _keywords_from_clause(clause: str, service: str) -> list[str]:
    """Words of one hint, in written order.

    The service token, ``and``, and ``not`` are not words. Hint text is
    kept as written: regex metacharacters are not stripped. Duplicate
    words that share a synonym set are kept once, at the first position.
    More than three words raises. The hint is not shortened to three.
    """
    tokens = [t for t in re.split(r"\s+", clause.strip()) if t and t.lower() != service.lower()]
    keywords: list[str] = []
    seen: set[tuple[str, ...]] = set()
    for token in tokens:
        if token.lower() in _DROPPED_WORDS:
            continue
        terms = _terms_for_hint(token)
        if terms in seen:
            continue
        seen.add(terms)
        keywords.append(token)
    if len(keywords) > 3:
        raise ValueError(
            f"hint has {len(keywords)} words; at most 3 are supported: {clause.strip()!r}"
        )
    return keywords


def _permute_pattern(keywords: list[str]) -> str:
    """Every word order of one hint, as alternatives in one group.

    RE2 has no lookahead. Each order joins the words with ``.*``.
    ``connection refused`` is ``(?:connection.*refused|refused.*connection)``.
    Three words produce at most six alternatives. One word is that word's
    pattern, with no extra group. Permutations follow
    ``itertools.permutations`` on the written word order, so the same hint
    always yields the same string.
    """
    if len(keywords) > 3:
        raise ValueError(
            f"hint has {len(keywords)} words; at most 3 are supported"
        )
    word_patterns = [_hint_pattern(keyword) for keyword in keywords]
    if len(word_patterns) == 1:
        return word_patterns[0]
    alternatives: list[str] = []
    seen: set[str] = set()
    for perm in itertools.permutations(word_patterns):
        alternative = ".*".join(perm)
        if alternative in seen:
            continue
        seen.add(alternative)
        alternatives.append(alternative)
    return "(?:" + "|".join(alternatives) + ")"


def splunk_query_to_logql(query: str, service: str | None = None) -> str:
    """Map a playbook ``query_hint`` to a Loki LogQL selector.

    The service token is dropped, case-insensitively, when the playbook
    already interpolated it. ``and`` and ``not`` are not hints. Hint words
    are regex-escaped and are not stripped down to alphanumerics.

    A space-separated hint of one, two, or three words is one filter.
    Two or three words become every permutation of those words inside one
    non-capturing group. Every permutation still contains every word, so
    the hint stays a conjunction; only the order is free. Matching either
    word alone would return lines the playbook did not ask for. A hint
    with more than three words raises ``ValueError``. Within a word, the
    synonym set is OR'd. A token that is itself a listed synonym uses that
    hint's full set. A token with no set matches itself only.

    An explicit ``OR`` (any case) splits the query into alternatives. Each
    alternative gets the same permutation treatment, and the alternatives
    are OR'd in one filter. ``latency OR slow`` is
    ``|~ "(?i)(?:latency|slow)"``. ``connection refused OR dns`` permutes
    the two-word side, then ORs ``dns``.

    An empty keyword list selects the service stream and adds no filter.
    ``service`` must be a non-empty label token; see ``_safe_service``.
    The selector is a label equality match, ``{service="..."}``, not a
    regex matcher. A dot in a service name such as ``svc.v2`` is literal.
    A service name that instead arrives as a hint word is escaped with
    the other hint text, so the dot cannot match an arbitrary character.
    """
    svc = _safe_service(service)
    raw = (query or "").strip()
    patterns: list[str] = []
    seen_patterns: set[str] = set()
    for part in _OR_SPLIT.split(raw):
        keywords = _keywords_from_clause(part, svc)
        if not keywords:
            continue
        pattern = _permute_pattern(keywords)
        if pattern in seen_patterns:
            continue
        seen_patterns.add(pattern)
        patterns.append(pattern)
    selector = '{service="%s"}' % _quote_logql(svc)
    if not patterns:
        return selector
    body = patterns[0] if len(patterns) == 1 else "(?:" + "|".join(patterns) + ")"
    return selector + " " + _line_filter(body)


def metric_hint_to_promql(metric: str | None, service: str | None = None) -> str:
    """Map a metric hint to PromQL. The hint lookup is case-insensitive."""
    svc = _safe_service(service)
    key = (metric or "").strip().lower() or "request_rate"
    template = METRIC_PROMQL.get(key, METRIC_PROMQL["request_rate"])
    # Label equality. Quote quotes and backslashes. Do not regex-escape
    # the value: a dot must stay a dot in the label text.
    return template.format(service=_quote_logql(svc))


def golden_signal_promql(signal: str, service: str | None = None) -> str:
    """Map a golden-signal name to PromQL. The name lookup is case-insensitive."""
    svc = _safe_service(service)
    template = GOLDEN_SIGNAL_PROMQL.get((signal or "").strip().lower())
    if template is None:
        raise KeyError(signal)
    return template.format(service=_quote_logql(svc))
