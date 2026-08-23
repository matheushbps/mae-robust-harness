from __future__ import annotations

import re
from typing import Any


def apply_explicit_visual_requirements(
    briefing: dict[str, Any], request: str
) -> dict[str, Any]:
    normalized = re.sub(r"\s+", " ", request.lower())
    background = None
    if "white background" in normalized or "fundo branco" in normalized:
        background = "#ffffff"
    elif "black background" in normalized or "fundo preto" in normalized:
        background = "#000000"
    if background is None:
        return briefing
    result = dict(briefing)
    current = briefing.get("visual_theme")
    theme = dict(current) if isinstance(current, dict) else {}
    theme["background"] = background
    theme.setdefault("accent", "#2563eb" if background == "#ffffff" else "#38bdf8")
    result["visual_theme"] = theme
    return result
