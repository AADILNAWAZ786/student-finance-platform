"""Execution provenance for ``run_log.json`` and ``GenerationRun.code_version`` (Phase 8).

Nothing here is part of any checksum: it describes how a run was executed, never what it
produced (ARCHITECTURE §11.4).
"""

from __future__ import annotations

import platform
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import django

ROOT = Path(__file__).resolve().parents[1]


def git_sha() -> str | None:
    """The checked-out commit (40 hex characters), or None outside a git checkout."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and len(sha) == 40 else None


def environment() -> dict:
    return {
        "host": socket.gethostname(),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "django": django.get_version(),
        "sqlite": sqlite3.sqlite_version,
        "git_sha": git_sha(),
    }
