"""CLI entry point for sentinel_wiki.

Usage:
    python -m sentinel_wiki bootstrap
    python -m sentinel_wiki ingest
    python -m sentinel_wiki status
    python -m sentinel_wiki search "<query>"
"""

from __future__ import annotations

import sys


def _cmd_bootstrap(args: list[str]) -> int:
    from sentinel_wiki.bootstrap import bootstrap
    base = args[0] if args else "sentinel_wiki"
    created = bootstrap(base)
    print(f"Bootstrap complete. {len(created)} paths created/verified under {base}/")
    return 0


def _cmd_ingest(args: list[str]) -> int:
    from sentinel_wiki.ingester import ingest
    base = args[0] if args else "sentinel_wiki"
    result = ingest(base)
    print(f"Ingest complete.")
    print(f"  Ingested : {len(result.ingested)}")
    print(f"  Skipped  : {len(result.skipped)}")
    print(f"  Errors   : {len(result.errors)}")
    for ingested in result.ingested:
        print(f"    + {ingested}")
    for err in result.errors:
        print(f"    ! {err}", file=sys.stderr)
    return 1 if result.errors else 0


def _cmd_status(args: list[str]) -> int:
    from sentinel_wiki.searcher import status
    base = args[0] if args else "sentinel_wiki"
    s = status(base)
    print("sentinel_wiki status:")
    for k, v in s.items():
        print(f"  {k:<28} {v}")
    return 0


def _cmd_search(args: list[str]) -> int:
    if not args:
        print("Usage: python -m sentinel_wiki search <query>", file=sys.stderr)
        return 1
    from sentinel_wiki.searcher import search
    query = " ".join(args[:-1]) if len(args) > 1 and not args[-1].startswith("-") else args[0]
    base = "sentinel_wiki"
    hits = search(query, base_path=base)
    if not hits:
        print(f"No results for: {query!r}")
        return 0
    print(f"{len(hits)} result(s) for {query!r}:")
    for hit in hits:
        snippet = f"  → {hit.snippet}" if hit.snippet else ""
        print(f"  [{hit.match_type}] {hit.note_path} (score={hit.score}){snippet}")
    return 0


_COMMANDS = {
    "bootstrap": _cmd_bootstrap,
    "ingest": _cmd_ingest,
    "status": _cmd_status,
    "search": _cmd_search,
}

if __name__ == "__main__":
    argv = sys.argv[1:]
    if not argv or argv[0] not in _COMMANDS:
        print("Usage: python -m sentinel_wiki <bootstrap|ingest|status|search>")
        sys.exit(1)
    cmd, rest = argv[0], argv[1:]
    sys.exit(_COMMANDS[cmd](rest))
