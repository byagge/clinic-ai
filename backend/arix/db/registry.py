"""Imports every model module so Alembic autogenerate sees the full metadata."""
from __future__ import annotations

from arix.db.base import Base  # noqa: F401
from arix.db.models_core import *  # noqa: F401,F403
from arix.verticals.clinic.models import *  # noqa: F401,F403

metadata = Base.metadata
