"""Bidirectional incident↔commit index for SentinalAI.

Every time a git commit is identified as the root cause of an incident
(via git_worker.git_find_breaking_change) or as the fix for an incident
(via fix_engine creating a PR), a link is recorded here.

This index answers two critical queries:

  Forward:  "INC-0042 was caused/fixed by which commits?"
            → get_commits_for_incident("INC-0042")

  Reverse:  "Commit abc123 was involved in which incidents?"
            → get_incidents_for_commit("abc123")

This closes the hot-integration loop: any code change can be backtracked
to the incident/alert that triggered or was caused by it.

Link types:
    caused_by   — commit introduced the regression that caused this incident
    fixed_by    — commit (PR merge) resolved the incident
    related     — commit is correlated but causality not confirmed

Persistence:
    File-backed JSON at INCIDENT_GIT_INDEX_PATH.
    Atomic writes via tmp + os.replace().
    Thread-safe reads/writes via module-level RLock.

Configuration:
    INCIDENT_GIT_INDEX_PATH  — JSON file (default: eval/incident_git_index.json)
    INCIDENT_GIT_LINKER_ENABLED — on/off (default: true)
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Any

logger = logging.getLogger("sentinalai.incident_git_linker")

LINKER_ENABLED = os.environ.get(
    "INCIDENT_GIT_LINKER_ENABLED", "true"
).lower() in ("1", "true", "yes")

INDEX_PATH = os.environ.get(
    "INCIDENT_GIT_INDEX_PATH",
    os.path.join(os.path.dirname(__file__), "..", "eval", "incident_git_index.json"),
)

_lock = threading.RLock()
_index: "_GitIncidentIndex | None" = None


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class GitLink:
    """A link between an incident and a commit."""

    incident_id: str
    commit_sha: str
    repo: str
    relationship: str              # caused_by | fixed_by | related
    confidence: float = 1.0        # 0.0–1.0
    author: str = ""
    commit_message: str = ""
    pr_number: int | None = None
    linked_at: float = field(default_factory=time.time)
    linked_by: str = "agent"       # agent | human | webhook

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "GitLink":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    @property
    def short_sha(self) -> str:
        return self.commit_sha[:8] if self.commit_sha else ""


# ---------------------------------------------------------------------------
# Index class
# ---------------------------------------------------------------------------

class _GitIncidentIndex:
    """In-memory bidirectional index with file persistence."""

    def __init__(self) -> None:
        # incident_id → [GitLink]
        self._by_incident: dict[str, list[GitLink]] = {}
        # commit_sha → [GitLink]
        self._by_commit: dict[str, list[GitLink]] = {}

    # ------------------------------------------------------------------ #
    # Mutation
    # ------------------------------------------------------------------ #

    def link(self, link: GitLink) -> None:
        """Record a bidirectional incident↔commit link."""
        # Dedup: same incident+sha+relationship = idempotent
        existing = self._by_incident.get(link.incident_id, [])
        for existing_link in existing:
            if (existing_link.commit_sha == link.commit_sha and
                    existing_link.relationship == link.relationship):
                # Update confidence if higher
                existing_link.confidence = max(existing_link.confidence, link.confidence)
                return

        self._by_incident.setdefault(link.incident_id, []).append(link)
        self._by_commit.setdefault(link.commit_sha, []).append(link)
        logger.info(
            "Linked incident %s ← %s → commit %s (%s, confidence=%.2f)",
            link.incident_id, link.relationship, link.short_sha, link.repo, link.confidence,
        )

    # ------------------------------------------------------------------ #
    # Query
    # ------------------------------------------------------------------ #

    def get_commits_for_incident(
        self, incident_id: str, relationship: str | None = None
    ) -> list[GitLink]:
        """Return all commits linked to an incident."""
        links = self._by_incident.get(incident_id, [])
        if relationship:
            links = [l for l in links if l.relationship == relationship]
        return sorted(links, key=lambda l: -l.confidence)

    def get_incidents_for_commit(
        self, commit_sha: str, relationship: str | None = None
    ) -> list[GitLink]:
        """Return all incidents linked to a commit SHA."""
        # Support prefix matching (first 8 chars)
        results: list[GitLink] = []
        for sha, links in self._by_commit.items():
            if sha == commit_sha or sha.startswith(commit_sha) or commit_sha.startswith(sha[:8]):
                results.extend(links)
        if relationship:
            results = [l for l in results if l.relationship == relationship]
        return sorted(results, key=lambda l: -l.confidence)

    def get_incident_ids_for_repo(self, repo: str) -> list[str]:
        """Return all incident IDs that have commits in a given repo."""
        seen: set[str] = set()
        for links in self._by_incident.values():
            for link in links:
                if link.repo == repo:
                    seen.add(link.incident_id)
        return sorted(seen)

    def has_link(self, incident_id: str, commit_sha: str) -> bool:
        """Check if any link exists between incident and commit."""
        for link in self._by_incident.get(incident_id, []):
            if link.commit_sha.startswith(commit_sha[:8]):
                return True
        return False

    def stats(self) -> dict[str, int]:
        return {
            "incidents_linked": len(self._by_incident),
            "commits_linked": len(self._by_commit),
            "total_links": sum(len(v) for v in self._by_incident.values()),
        }

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    def to_dict(self) -> dict[str, Any]:
        return {
            "by_incident": {
                iid: [l.to_dict() for l in links]
                for iid, links in self._by_incident.items()
            },
            "by_commit": {
                sha: [l.to_dict() for l in links]
                for sha, links in self._by_commit.items()
            },
            "stats": self.stats(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "_GitIncidentIndex":
        idx = cls()
        for iid, links in data.get("by_incident", {}).items():
            for ld in links:
                link = GitLink.from_dict(ld)
                idx._by_incident.setdefault(iid, []).append(link)
                idx._by_commit.setdefault(link.commit_sha, []).append(link)
        return idx

    def save(self, path: str = INDEX_PATH) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
        os.replace(tmp, path)
        logger.debug("Incident-git index saved: %s", self.stats())


# ---------------------------------------------------------------------------
# Module-level API
# ---------------------------------------------------------------------------

def _get_index() -> _GitIncidentIndex:
    global _index
    with _lock:
        if _index is not None:
            return _index
        idx = _GitIncidentIndex()
        if os.path.exists(INDEX_PATH):
            try:
                with open(INDEX_PATH) as f:
                    data = json.load(f)
                idx = _GitIncidentIndex.from_dict(data)
                logger.info(
                    "Loaded incident-git index: %d incidents, %d commits",
                    len(idx._by_incident), len(idx._by_commit),
                )
            except Exception as exc:
                logger.warning("Could not load incident-git index: %s — starting fresh", exc)
        _index = idx
        return _index


def link_incident_to_commit(
    incident_id: str,
    commit_sha: str,
    repo: str,
    relationship: str = "caused_by",
    confidence: float = 1.0,
    author: str = "",
    commit_message: str = "",
    pr_number: int | None = None,
    save: bool = True,
) -> GitLink | None:
    """Record a link between an incident and a commit.

    Args:
        incident_id:     Alert/incident identifier (e.g. "INC-0042")
        commit_sha:      Full or partial commit SHA
        repo:            org/repo string (e.g. "acme/payment-service")
        relationship:    "caused_by" | "fixed_by" | "related"
        confidence:      0.0–1.0 confidence that this link is correct
        author:          Commit author (for display)
        commit_message:  First 120 chars of commit message
        pr_number:       PR number if commit was a PR merge
        save:            Persist index to disk after linking

    Returns:
        GitLink if successful, None if linker is disabled
    """
    if not LINKER_ENABLED:
        return None
    link = GitLink(
        incident_id=incident_id,
        commit_sha=commit_sha,
        repo=repo,
        relationship=relationship,
        confidence=confidence,
        author=author,
        commit_message=commit_message[:120] if commit_message else "",
        pr_number=pr_number,
    )
    with _lock:
        idx = _get_index()
        idx.link(link)
        if save:
            try:
                idx.save()
            except Exception as exc:
                logger.warning("Failed to save incident-git index: %s", exc)
    return link


def get_commits_for_incident(
    incident_id: str,
    relationship: str | None = None,
) -> list[dict[str, Any]]:
    """Return all commits linked to an incident.

    Returns list of dicts with keys: commit_sha, repo, relationship,
    confidence, author, commit_message, pr_number, linked_at.
    """
    if not LINKER_ENABLED:
        return []
    idx = _get_index()
    return [l.to_dict() for l in idx.get_commits_for_incident(incident_id, relationship)]


def get_incidents_for_commit(
    commit_sha: str,
    relationship: str | None = None,
) -> list[dict[str, Any]]:
    """Return all incidents linked to a commit SHA.

    Supports short SHA prefix matching (first 8 chars).
    """
    if not LINKER_ENABLED:
        return []
    idx = _get_index()
    return [l.to_dict() for l in idx.get_incidents_for_commit(commit_sha, relationship)]


def get_incident_audit_trail(incident_id: str) -> dict[str, Any]:
    """Return full audit trail for an incident: causal commits + fix commits."""
    if not LINKER_ENABLED:
        return {"incident_id": incident_id, "enabled": False}
    idx = _get_index()
    caused_by = idx.get_commits_for_incident(incident_id, "caused_by")
    fixed_by  = idx.get_commits_for_incident(incident_id, "fixed_by")
    related   = idx.get_commits_for_incident(incident_id, "related")
    return {
        "incident_id": incident_id,
        "caused_by_commits": [l.to_dict() for l in caused_by],
        "fixed_by_commits":  [l.to_dict() for l in fixed_by],
        "related_commits":   [l.to_dict() for l in related],
        "total_links": len(caused_by) + len(fixed_by) + len(related),
    }


def index_stats() -> dict[str, int]:
    """Return index statistics."""
    if not LINKER_ENABLED:
        return {}
    return _get_index().stats()
