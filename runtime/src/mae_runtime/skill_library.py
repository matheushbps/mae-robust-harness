from __future__ import annotations

import re

from .config import RUNTIME_ROOT

SKILLS_ROOT = RUNTIME_ROOT / "skills"
VALID_SKILL_NAME = re.compile(r"^[a-z0-9-]+$")


def load_skill(name: str) -> str:
    if not VALID_SKILL_NAME.fullmatch(name):
        raise ValueError(f"Invalid skill name: {name}")
    path = SKILLS_ROOT / name / "SKILL.md"
    if not path.is_file():
        raise FileNotFoundError(f"Runtime skill not found: {path}")
    text = path.read_text(encoding="utf-8")
    if text.startswith("---\n"):
        sections = text.split("---\n", 2)
        if len(sections) != 3:
            raise ValueError(f"Malformed skill frontmatter: {path}")
        text = sections[2]
    return text.strip()


def render_agent_skills(skill_names: list[str]) -> str:
    packets = [f"## Runtime skill: {name}\n\n{load_skill(name)}" for name in skill_names]
    return "\n\n".join(packets)
