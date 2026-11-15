"""Project settings.

Settings live in ``sfti.config.json`` at the project root and are re-read on every guarded
operation, so a change made by the user takes effect without restarting anything.

The file can only *enable* optional behaviour on non-production orgs (and, explicitly, Apex
test runs on production). Nothing in it can enable writes on a production org: that block is
unconditional and lives in ``org_guard``.

Any problem reading the file falls back to the most restrictive settings.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

CONFIG_FILENAME = "sfti.config.json"

MODE_ADVISE = "advise"
MODE_APPLY = "apply"
MODES = (MODE_ADVISE, MODE_APPLY)

HARD_MAX_ROW_LIMIT = 2000


@dataclass(frozen=True)
class Settings:
    #: "advise": the tool explains the fix and the user implements it. "apply": the tool may
    #: deploy and iterate, each write still needing the user's approval.
    mode: str = MODE_ADVISE
    #: Developer Edition orgs report IsSandbox = false. Off unless the user opts in.
    allow_developer_edition_writes: bool = False
    #: Running Apex tests on production is blocked unless the user opts in.
    allow_apex_tests_on_production: bool = False
    default_row_limit: int = 50
    max_row_limit: int = 200
    #: Problems found while loading the file; surfaced to the user by ``get_config``.
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "allow_developer_edition_writes": self.allow_developer_edition_writes,
            "allow_apex_tests_on_production": self.allow_apex_tests_on_production,
            "default_row_limit": self.default_row_limit,
            "max_row_limit": self.max_row_limit,
            "warnings": list(self.warnings),
        }


SAFE_DEFAULTS = Settings()


def project_root() -> Path:
    """Project root: ``SFTI_PROJECT_ROOT`` if set, otherwise the parent of this package."""
    env = os.environ.get("SFTI_PROJECT_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def _strict_bool(value: Any, name: str, warnings: list[str]) -> bool:
    if value is True:
        return True
    if value is False or value is None:
        return False
    warnings.append(f"{name} must be true or false; got {value!r}. Treated as false.")
    return False


def _bounded_int(value: Any, name: str, default: int, low: int, high: int, warnings: list[str]) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        warnings.append(f"{name} must be an integer; got {value!r}. Using {default}.")
        return default
    if value < low or value > high:
        warnings.append(f"{name} must be between {low} and {high}; got {value}. Using {default}.")
        return default
    return value


def load_settings(root: Path | None = None) -> Settings:
    """Load settings, falling back to the safe defaults on any problem."""
    path = (root or project_root()) / CONFIG_FILENAME
    if not path.exists():
        return SAFE_DEFAULTS
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return replace(SAFE_DEFAULTS, warnings=(f"Could not read {CONFIG_FILENAME}: {exc}. Safe defaults in use.",))
    if not isinstance(raw, dict):
        return replace(SAFE_DEFAULTS, warnings=(f"{CONFIG_FILENAME} must contain a JSON object. Safe defaults in use.",))

    warnings: list[str] = []
    mode = raw.get("mode", MODE_ADVISE)
    if mode not in MODES:
        warnings.append(f"mode must be one of {MODES}; got {mode!r}. Using {MODE_ADVISE!r}.")
        mode = MODE_ADVISE

    max_limit = _bounded_int(raw.get("max_row_limit"), "max_row_limit", 200, 1, HARD_MAX_ROW_LIMIT, warnings)
    default_limit = _bounded_int(raw.get("default_row_limit"), "default_row_limit", 50, 1, max_limit, warnings)
    default_limit = min(default_limit, max_limit)

    known = {
        "mode",
        "allow_developer_edition_writes",
        "allow_apex_tests_on_production",
        "default_row_limit",
        "max_row_limit",
    }
    for key in raw:
        if key not in known and not key.startswith("_"):
            warnings.append(f"Unknown setting {key!r} ignored.")

    return Settings(
        mode=mode,
        allow_developer_edition_writes=_strict_bool(
            raw.get("allow_developer_edition_writes"), "allow_developer_edition_writes", warnings
        ),
        allow_apex_tests_on_production=_strict_bool(
            raw.get("allow_apex_tests_on_production"), "allow_apex_tests_on_production", warnings
        ),
        default_row_limit=default_limit,
        max_row_limit=max_limit,
        warnings=tuple(warnings),
    )
