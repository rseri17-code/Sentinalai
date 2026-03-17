"""
Skill definition validation tests.

Ensures every skill file in skills/ is well-formed:
- Has required YAML frontmatter fields
- References a valid playbook type
- Has the correct max_calls budget
- Matches the incident_types it claims to handle
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SKILLS_DIR = Path(__file__).parent.parent / "skills"
VALID_PLAYBOOK_TYPES = frozenset([
    "timeout", "oomkill", "error_spike", "latency", "saturation",
    "network", "cascading", "missing_data", "flapping", "silent_failure",
])

REQUIRED_FRONTMATTER_FIELDS = {"skill", "description", "playbook", "incident_types", "max_calls"}
MAX_ALLOWED_CALLS = 8  # matches PHASE_CALL_LIMITS evidence_gathering ceiling


def _parse_frontmatter(content: str) -> dict:
    """Extract YAML-like frontmatter fields from a markdown file."""
    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return {}
    fields: dict = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            # Parse list fields: [a, b, c]
            if value.startswith("[") and value.endswith("]"):
                items = [v.strip() for v in value[1:-1].split(",")]
                fields[key] = items
            elif value:
                fields[key] = value
    return fields


def skill_files():
    return list(SKILLS_DIR.glob("*.md"))


@pytest.mark.parametrize("skill_file", skill_files(), ids=lambda f: f.name)
class TestSkillFiles:

    def test_file_exists_and_non_empty(self, skill_file: Path):
        assert skill_file.exists()
        content = skill_file.read_text(encoding="utf-8")
        assert len(content) > 100, f"{skill_file.name} is suspiciously short"

    def test_has_frontmatter(self, skill_file: Path):
        content = skill_file.read_text(encoding="utf-8")
        assert content.startswith("---"), f"{skill_file.name} missing frontmatter opening"
        fm = _parse_frontmatter(content)
        assert fm, f"{skill_file.name} frontmatter could not be parsed"

    def test_required_fields_present(self, skill_file: Path):
        content = skill_file.read_text(encoding="utf-8")
        fm = _parse_frontmatter(content)
        missing = REQUIRED_FRONTMATTER_FIELDS - set(fm.keys())
        assert not missing, f"{skill_file.name} missing frontmatter fields: {missing}"

    def test_playbook_is_valid_type(self, skill_file: Path):
        content = skill_file.read_text(encoding="utf-8")
        fm = _parse_frontmatter(content)
        playbook = fm.get("playbook", "")
        assert playbook in VALID_PLAYBOOK_TYPES, (
            f"{skill_file.name} has unknown playbook '{playbook}'. "
            f"Valid: {sorted(VALID_PLAYBOOK_TYPES)}"
        )

    def test_incident_types_are_valid(self, skill_file: Path):
        content = skill_file.read_text(encoding="utf-8")
        fm = _parse_frontmatter(content)
        incident_types = fm.get("incident_types", [])
        if isinstance(incident_types, str):
            incident_types = [incident_types]
        for itype in incident_types:
            assert itype in VALID_PLAYBOOK_TYPES, (
                f"{skill_file.name} references unknown incident_type '{itype}'"
            )

    def test_max_calls_within_budget(self, skill_file: Path):
        content = skill_file.read_text(encoding="utf-8")
        fm = _parse_frontmatter(content)
        raw = fm.get("max_calls", "0")
        try:
            max_calls = int(raw)
        except ValueError:
            pytest.fail(f"{skill_file.name} has non-integer max_calls: '{raw}'")
        assert 1 <= max_calls <= MAX_ALLOWED_CALLS, (
            f"{skill_file.name} max_calls={max_calls} out of range [1, {MAX_ALLOWED_CALLS}]"
        )

    def test_has_investigation_steps_section(self, skill_file: Path):
        content = skill_file.read_text(encoding="utf-8")
        assert "## Investigation Steps" in content, (
            f"{skill_file.name} missing '## Investigation Steps' section"
        )

    def test_has_hypotheses_section(self, skill_file: Path):
        content = skill_file.read_text(encoding="utf-8")
        assert "## Hypotheses" in content, (
            f"{skill_file.name} missing '## Hypotheses' section"
        )

    def test_has_success_criteria_section(self, skill_file: Path):
        content = skill_file.read_text(encoding="utf-8")
        assert "## Success Criteria" in content, (
            f"{skill_file.name} missing '## Success Criteria' section"
        )

    def test_skill_name_matches_filename(self, skill_file: Path):
        content = skill_file.read_text(encoding="utf-8")
        fm = _parse_frontmatter(content)
        skill_name = fm.get("skill", "")
        # skill name should appear in filename (e.g., "timeout" in "timeout-investigation.md")
        assert skill_name.split("-")[0] in skill_file.stem, (
            f"{skill_file.name}: skill name '{skill_name}' doesn't match filename"
        )


class TestSkillCoverage:
    """All 10 playbook types must have a corresponding skill file."""

    def test_all_playbook_types_have_skill_file(self):
        present_playbooks = set()
        for skill_file in SKILLS_DIR.glob("*.md"):
            content = skill_file.read_text(encoding="utf-8")
            fm = _parse_frontmatter(content)
            pb = fm.get("playbook", "")
            if pb:
                present_playbooks.add(pb)
        missing = VALID_PLAYBOOK_TYPES - present_playbooks
        assert not missing, (
            f"No skill file for playbook types: {sorted(missing)}"
        )


class TestAgentFiles:
    """Smoke-test agent definition files in agents/."""

    AGENTS_DIR = Path(__file__).parent.parent / "agents"
    REQUIRED_AGENTS = {
        "incident-classifier",
        "hypothesis-scorer",
        "rca-writer",
        "loop-operator",
        "investigation-coordinator",
    }

    def test_agents_dir_exists(self):
        assert self.AGENTS_DIR.exists()

    def test_required_agents_present(self):
        present = {f.stem for f in self.AGENTS_DIR.glob("*.md")}
        missing = self.REQUIRED_AGENTS - present
        assert not missing, f"Missing agent files: {missing}"

    @pytest.mark.parametrize("agent_name", list(REQUIRED_AGENTS), ids=lambda n: n)
    def test_agent_has_name_in_frontmatter(self, agent_name: str):
        AGENTS_DIR = Path(__file__).parent.parent / "agents"
        agent_file = AGENTS_DIR / f"{agent_name}.md"
        assert agent_file.exists(), f"Agent file not found: {agent_file}"
        content = agent_file.read_text(encoding="utf-8")
        assert "name:" in content, f"{agent_file.name} missing 'name:' in frontmatter"

    # Make required_agents accessible to parametrize
    REQUIRED_AGENTS = {
        "incident-classifier",
        "hypothesis-scorer",
        "rca-writer",
        "loop-operator",
        "investigation-coordinator",
    }


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
