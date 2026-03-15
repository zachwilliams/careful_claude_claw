import pytest

import careful_claude_claw.skills as skills_module
from careful_claude_claw.models import SkillScope


@pytest.fixture()
def global_skills_dir(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    monkeypatch.setattr(skills_module, "GLOBAL_SKILLS_DIR", skills_dir)
    return skills_dir


def test_discover_global_skills(global_skills_dir):
    (global_skills_dir / "daily-briefing.md").write_text(
        "# Daily Briefing\nCheck emails and slack."
    )
    (global_skills_dir / "code-review.md").write_text("# Code Review\nReview open PRs.")

    skills = skills_module.discover_skills()
    assert len(skills) == 2
    names = {s.name for s in skills}
    assert names == {"daily-briefing", "code-review"}
    assert all(s.scope == SkillScope.GLOBAL for s in skills)


def test_get_skill_by_name(global_skills_dir):
    (global_skills_dir / "briefing.md").write_text("# Briefing\nMorning update.")

    skill = skills_module.get_skill("briefing")
    assert skill is not None
    assert skill.name == "briefing"
    assert skill.description == "Briefing"


def test_get_skill_not_found(global_skills_dir):
    assert skills_module.get_skill("nonexistent") is None


def test_skill_description_parsing(global_skills_dir):
    (global_skills_dir / "test.md").write_text("# My Skill Title\nSome details here.")

    skills = skills_module.discover_skills()
    assert skills[0].description == "My Skill Title"


def test_empty_skills_dir(global_skills_dir):
    assert skills_module.discover_skills() == []
