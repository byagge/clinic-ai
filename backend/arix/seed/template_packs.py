"""Template loading and validation.

Templates are YAML (``arix/seed/templates/*.yaml``) and are validated against
:class:`arix.core.tenant_config.TenantSettings` at load time, so a typo in a
template fails on import rather than halfway through a patient dialogue.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from arix.core.tenant_config import TenantSettings

TEMPLATE_DIR = Path(__file__).parent / "packs"


@dataclass
class Template:
    code: str
    vertical: str
    title: str
    rubrics: list[str]
    config: dict[str, Any]
    services: list[dict] = field(default_factory=list)
    doctors: list[dict] = field(default_factory=list)
    courses: list[dict] = field(default_factory=list)
    teachers: list[dict] = field(default_factory=list)
    groups: list[dict] = field(default_factory=list)
    level_test: dict = field(default_factory=dict)
    kb: list[dict] = field(default_factory=list)
    sequences: dict = field(default_factory=dict)

    def settings(self) -> TenantSettings:
        return TenantSettings.from_data(self.config)


def _load_file(path: Path) -> Template:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    meta = raw.get("meta") or {}
    template = Template(
        code=meta.get("code") or path.stem,
        vertical=meta.get("vertical") or "clinic",
        title=meta.get("title") or path.stem,
        rubrics=[r.lower() for r in (meta.get("rubrics") or [])],
        config=raw.get("config") or {},
        services=raw.get("services") or [],
        doctors=raw.get("doctors") or [],
        courses=raw.get("courses") or [],
        teachers=raw.get("teachers") or [],
        groups=raw.get("groups") or [],
        level_test=raw.get("level_test") or {},
        kb=raw.get("kb") or [],
        sequences=raw.get("sequences") or {},
    )
    template.settings()  # raises on invalid config
    return template


@lru_cache
def load_all() -> dict[str, Template]:
    out: dict[str, Template] = {}
    for path in sorted(TEMPLATE_DIR.glob("*.yaml")):
        template = _load_file(path)
        out[template.code] = template
    return out


def get_template(code: str | None) -> Template | None:
    if not code:
        return None
    return load_all().get(code)


def pick_by_rubrics(rubrics: list[str], *, vertical: str, default: str) -> Template:
    """Choose a template from 2GIS rubrics (demo generator, ТЗ §12)."""
    needles = [r.lower() for r in rubrics if r]
    best: tuple[int, Template] | None = None
    for template in load_all().values():
        if template.vertical != vertical:
            continue
        score = sum(
            1
            for rubric in template.rubrics
            for needle in needles
            if rubric in needle or needle in rubric
        )
        if score and (best is None or score > best[0]):
            best = (score, template)
    if best is not None:
        return best[1]
    fallback = load_all().get(default)
    if fallback is None:  # pragma: no cover - packaging error
        raise RuntimeError(f"default template {default!r} is missing")
    return fallback


__all__ = ["TEMPLATE_DIR", "Template", "get_template", "load_all", "pick_by_rubrics"]
