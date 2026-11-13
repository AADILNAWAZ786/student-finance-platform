"""System checks for the SQLite setup (ARCHITECTURE.md §8). Run on every manage.py command.

The required aliases and router come from settings (the ``config`` layer). ``core`` is
one of the packages that must never name the ground-truth database (ARCHITECTURE §12.2).
"""

from __future__ import annotations

import sqlite3

from django.conf import settings
from django.core.checks import Error, register


def check_sqlite_setup(
    databases: dict | None = None,
    routers: list[str] | None = None,
    sqlite_version: tuple[int, ...] | None = None,
    min_version: tuple[int, ...] | None = None,
    required_aliases: tuple[str, ...] | None = None,
    required_routers: tuple[str, ...] | None = None,
) -> list[Error]:
    databases = settings.DATABASES if databases is None else databases
    routers = settings.DATABASE_ROUTERS if routers is None else routers
    if sqlite_version is None:
        sqlite_version = sqlite3.sqlite_version_info
    if min_version is None:
        min_version = settings.SQLITE_MIN_VERSION
    if required_aliases is None:
        required_aliases = tuple(settings.REQUIRED_DATABASE_ALIASES)
    if required_routers is None:
        required_routers = tuple(settings.REQUIRED_DATABASE_ROUTERS)

    errors: list[Error] = []
    if tuple(sqlite_version) < tuple(min_version):
        errors.append(
            Error(
                f"SQLite {'.'.join(map(str, sqlite_version))} is too old; "
                f"{'.'.join(map(str, min_version))}+ is required for JSONField.",
                id="core.E001",
            )
        )
    for alias in required_aliases:
        if alias not in databases:
            errors.append(Error(f"DATABASES[{alias!r}] is not configured.", id="core.E002"))
            continue
        cfg = databases[alias]
        if cfg.get("ENGINE") != "django.db.backends.sqlite3":
            errors.append(Error(f"DATABASES[{alias!r}] must use SQLite.", id="core.E003"))
        if cfg.get("OPTIONS", {}).get("transaction_mode") != "IMMEDIATE":
            errors.append(
                Error(
                    f"DATABASES[{alias!r}] must set OPTIONS transaction_mode='IMMEDIATE'.",
                    hint="Serializes write transactions on SQLite (ARCHITECTURE §8.2).",
                    id="core.E004",
                )
            )
    for router in required_routers:
        if router not in routers:
            errors.append(Error(f"{router} must be in DATABASE_ROUTERS.", id="core.E005"))
    try:
        with sqlite3.connect(":memory:") as conn:
            conn.execute("SELECT json_valid('{}')").fetchone()
    except sqlite3.OperationalError:
        errors.append(Error("SQLite JSON functions are unavailable.", id="core.E006"))
    return errors


@register()
def sqlite_setup_check(app_configs, **kwargs):
    return check_sqlite_setup()
