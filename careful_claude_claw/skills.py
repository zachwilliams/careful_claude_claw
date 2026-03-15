from pathlib import Path

from .db import get_project, list_projects
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


def discover_skills(project_name: str | None = None) -> list[Skill]:
    """Scan for skill files. If project_name is given, include that project's skills too."""
    skills: list[Skill] = []

    # Global skills
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

    # Project-scoped skills
    projects_to_scan: list[dict] = []
    if project_name:
        proj = get_project(project_name)
        if proj:
            projects_to_scan = [proj]
    else:
        projects_to_scan = list_projects()

    for proj in projects_to_scan:
        proj_skills_dir = Path(proj["path"]) / "skills"
        if proj_skills_dir.is_dir():
            for f in sorted(proj_skills_dir.glob("*.md")):
                skills.append(
                    Skill(
                        name=f.stem,
                        scope=SkillScope.PROJECT,
                        project_name=proj["name"],
                        description=_parse_skill_description(f),
                        file_path=str(f),
                    )
                )

    return skills


def get_skill(name: str, project_name: str | None = None) -> Skill | None:
    """Find a skill by name. Project skills take priority over global."""
    skills = discover_skills(project_name)
    # Prefer project-scoped match
    for s in skills:
        if s.name == name and s.scope == SkillScope.PROJECT:
            return s
    for s in skills:
        if s.name == name:
            return s
    return None
