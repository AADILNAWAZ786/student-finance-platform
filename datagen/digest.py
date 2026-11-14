"""Canonical content checksums (approved decision D-G2; ARCHITECTURE §11.4).

A table digest is the SHA-256 of its rows, one canonical JSON line per row, sorted by primary
key. Every concrete column is included except the documented volatile execution metadata:
``recorded_at`` (every table), ``started_at`` / ``finished_at`` (runs). Values are rendered
without floats: decimals with their field's decimal places, dates in ISO format, JSON with
sorted keys. The same function digests unsaved instances (before insert) and rows read back
from the database, so "what was generated" and "what is stored" can be compared.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal

from django.db import models

VOLATILE = frozenset({"recorded_at", "started_at", "finished_at"})


def _canonical(field: models.Field, value):
    if value is None:
        return None
    if isinstance(field, models.DecimalField):
        return f"{Decimal(value):.{field.decimal_places}f}"
    if isinstance(value, bool):
        return value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(field, models.JSONField):
        return json.loads(json.dumps(value, sort_keys=True, default=str))
    if isinstance(value, int):
        return value
    return str(value)


def fields_of(model) -> list[models.Field]:
    return [f for f in model._meta.concrete_fields if f.attname not in VOLATILE]


def row(instance, fields=None) -> dict:
    fields = fields or fields_of(type(instance))
    return {f.attname: _canonical(f, getattr(instance, f.attname)) for f in fields}


def table_digest(model, instances) -> str:
    fields = fields_of(model)
    lines = sorted(
        json.dumps(row(i, fields), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        for i in instances
    )
    h = hashlib.sha256()
    for line in lines:
        h.update(line.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def combined(digests: dict[str, str]) -> str:
    h = hashlib.sha256()
    for name in sorted(digests):
        h.update(f"{name}:{digests[name]}\n".encode())
    return h.hexdigest()


def instance_digests(rows_by_model: dict) -> dict[str, str]:
    return {m._meta.db_table: table_digest(m, rows) for m, rows in rows_by_model.items()}


def database_digests(models_, *, using: str = "default") -> dict[str, str]:
    return {
        m._meta.db_table: table_digest(m, m.objects.using(using).order_by("pk")) for m in models_
    }
