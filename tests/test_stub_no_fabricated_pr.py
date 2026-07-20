"""Trust-breaker guard — stub mode must never fabricate a remediation PR.

The audit found the GitHub stub returned a plausible, mergeable PR at a real-
looking URL. In default stub mode an operator could mistake it for a genuine
auto-remediation. Stub mode must fail loudly instead.
"""
from workers.mcp_client import _stub_github


def test_create_pr_stub_does_not_fabricate_a_pr():
    r = _stub_github("create_pull_request", {"repo": "org/x", "title": "fix"})
    assert r["pr"] is None
    assert r["created"] is False
    assert r["stub"] is True
    # no fake number / url / mergeable flag anywhere
    blob = str(r)
    assert "9001" not in blob
    assert "mergeable" not in blob
    assert "github.com" not in blob


def test_generic_pr_stub_is_not_actionable():
    r = _stub_github("get_pull_request", {})
    assert r["pr"] is None
    assert r["stub"] is True
