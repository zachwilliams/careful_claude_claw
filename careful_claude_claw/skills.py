from pathlib import Path

from .models import Skill, SkillScope

GLOBAL_SKILLS_DIR = Path("skills")


def _parse_skill_description(path: Path) -> str:
    """Extract first non-empty line from a skill file as its description."""
    try:
        for line in path.read_text().splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped:
                return stripped[:200]
    except OSError:
        pass
    return ""


def discover_skills() -> list[Skill]:
    """Scan for global skill files."""
    skills: list[Skill] = []

    if GLOBAL_SKILLS_DIR.is_dir():
        for f in sorted(GLOBAL_SKILLS_DIR.glob("*.md")):
            skills.append(
                Skill(
                    name=f.stem,
                    scope=SkillScope.GLOBAL,
                    description=_parse_skill_description(f),
                    file_path=str(f),
                )
            )

    return skills


def get_skill(name: str) -> Skill | None:
    """Find a skill by name."""
    for s in discover_skills():
        if s.name == name:
            return s
    return None
