"""``export_data``: the deterministic dataset export (ARCHITECTURE §11.4, Phase 8, P8-D2/D3).

Layout written under ``out``::

    dataset/csv/<table>.csv     sorted by PK, fixed column order, UTF-8, LF, money "1234.50"
    dataset/json/<table>.json   the same rows; sorted keys; 2-space indent; LF
    dataset/sql/schema.sql      CREATE statements from sqlite_master, sorted by name
    dataset/sql/seed.sql        INSERTs in PK order, one transaction
    ground_truth/ground_truth.csv, ground_truth.json      the labels (only here)
    reports/findings.csv, evaluation_report.json, validation_summary.md
    dataset_manifest.json       seed, scale, config hash, row counts, coverage, SHA-256 per file
    source_manifest.json        provenance: 100 % synthetic, no external source
    run_log.json                execution metadata: NOT deterministic, NOT checksummed

Every file except ``run_log.json`` depends only on seed + config + code: no timestamps, no
host, no path, no database page layout. Excluded columns (execution metadata, DATABASE_DESIGN
Conventions): ``recorded_at`` everywhere; runs are not exported as tables. ``schema.sql`` is
the database's own DDL with the ``recorded_at`` column definitions removed, so ``seed.sql``
(which carries no ``recorded_at``) loads into it (KNOWN_LIMITATIONS P8-O1).

Safety (P8-D3): the export refuses unless exactly one generation run exists, the output
directory is new or empty, the stored rows still equal the checksums recorded at generation,
and re-planning the run's seed + config in memory reproduces those checksums (so the
manifest's claims hold). Files are written into a temporary sibling directory and moved into
place only when complete.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from django.db import connections, models
from django.utils import timezone

from core.models import IdSequence
from datagen import digest, provenance
from datagen.evaluation import findings_of, labels_of
from datagen.models import GenerationRun
from datagen.pipeline.materialize import MODELS

FORMAT_VERSION = 1
AS_OF = date(2026, 12, 31)
DATASET_MODELS = tuple(m for m in MODELS if m is not IdSequence)  # IdSequence: internal
DETERMINISTIC_ROOTS = ("dataset", "ground_truth", "reports", "source_manifest.json")
EXCLUDED_COLUMNS = digest.VOLATILE


class ExportError(RuntimeError):
    """The export was refused; the message says why and what to do."""


# --- value rendering (shared with verify_export) -------------------------------------------------


def columns(model) -> list[models.Field]:
    return [f for f in model._meta.concrete_fields if f.attname not in EXCLUDED_COLUMNS]


def json_value(f: models.Field, value):
    """The JSON / manifest form of a column value (no floats; money as strings)."""
    if value is None:
        return None
    if isinstance(f, models.DecimalField):
        return f"{Decimal(value):.{f.decimal_places}f}"
    if isinstance(f, models.BooleanField):
        return bool(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(f, models.JSONField):
        return json.loads(json.dumps(value, sort_keys=True))
    if isinstance(value, int):
        return value
    return str(value)


def csv_value(f: models.Field, value) -> str:
    v = json_value(f, value)
    if v is None:
        return ""  # NULL (no exported text column holds an empty string)
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(f, models.JSONField):
        return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return str(v)


def sql_value(f: models.Field, value) -> str:
    v = json_value(f, value)
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(f, models.DecimalField) or (isinstance(v, int) and not isinstance(v, bool)):
        return str(v)
    if isinstance(f, models.JSONField):
        v = json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "'" + str(v).replace("'", "''") + "'"


def rows(model, *, using="default", queryset=None):
    """(field list, iterator of raw value tuples) in PK order, streamed."""
    fields = columns(model)
    qs = queryset if queryset is not None else model.objects.using(using).all()
    it = qs.order_by("pk").values_list(*[f.attname for f in fields]).iterator(chunk_size=5000)
    return fields, it


# --- file writers ---------------------------------------------------------------------------------


class _Hasher:
    """A text file writer that hashes what it writes (UTF-8, LF)."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(path, "w", encoding="utf-8", newline="\n")
        self.sha = hashlib.sha256()
        self.bytes = 0

    def write(self, text: str) -> None:
        self.fh.write(text)
        data = text.encode("utf-8")
        self.sha.update(data)
        self.bytes += len(data)

    def close(self) -> dict:
        self.fh.close()
        return {"sha256": self.sha.hexdigest(), "bytes": self.bytes}


def _write_csv(path: Path, fields, values) -> dict:
    out, n = _Hasher(path), 0
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow([f.attname for f in fields])
    for raw in values:
        writer.writerow([csv_value(f, v) for f, v in zip(fields, raw, strict=True)])
        n += 1
        if buf.tell() > 1 << 20:
            out.write(buf.getvalue())
            buf.seek(0)
            buf.truncate()
    out.write(buf.getvalue())
    return {**out.close(), "rows": n}


def _write_json(path: Path, fields, values) -> dict:
    """Byte-identical to ``json.dumps(list_of_rows, indent=2, sort_keys=True) + "\\n"``."""
    out, n = _Hasher(path), 0
    for raw in values:
        obj = {f.attname: json_value(f, v) for f, v in zip(fields, raw, strict=True)}
        text = json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False)
        out.write(("[\n" if n == 0 else ",\n") + "\n".join("  " + x for x in text.split("\n")))
        n += 1
    out.write("[]\n" if n == 0 else "\n]\n")
    return {**out.close(), "rows": n}


def _write_json_doc(path: Path, data) -> dict:
    out = _Hasher(path)
    out.write(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    return out.close()


def _write_text(path: Path, text: str) -> dict:
    out = _Hasher(path)
    out.write(text)
    return out.close()


_RECORDED_AT = re.compile(r',\s*"recorded_at"\s+datetime\s+NOT NULL', re.IGNORECASE)


def schema_sql(using="default") -> str:
    """CREATE TABLE statements of the dataset tables, then their CREATE INDEX statements (each
    group sorted by name, so an index never precedes its table), without the excluded
    ``recorded_at`` column (P8-O1)."""
    tables = sorted(m._meta.db_table for m in DATASET_MODELS)
    with connections[using].cursor() as cur:
        placeholders = ",".join(["%s"] * len(tables))
        cur.execute(
            "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL "
            f"AND tbl_name IN ({placeholders}) "
            "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END, name",
            tables,
        )
        statements = []
        for _name, sql in cur.fetchall():
            cleaned = _RECORDED_AT.sub("", sql)
            if "recorded_at" in cleaned:  # pragma: no cover - a DDL shape not handled
                raise ExportError(f"cannot remove recorded_at from: {sql[:80]}…")
            statements.append(cleaned.strip() + ";")
    return "\n".join(statements) + "\n"


def _write_seed(path: Path, using="default") -> dict:
    out, n = _Hasher(path), 0
    out.write("BEGIN TRANSACTION;\n")
    for model in DATASET_MODELS:
        fields, values = rows(model, using=using)
        head = f'INSERT INTO "{model._meta.db_table}" (' + ", ".join(
            f'"{f.column}"' for f in fields
        )
        buf = []
        for raw in values:
            vals = ", ".join(sql_value(f, v) for f, v in zip(fields, raw, strict=True))
            buf.append(f"{head}) VALUES ({vals});\n")
            n += 1
            if len(buf) >= 5000:
                out.write("".join(buf))
                buf.clear()
        out.write("".join(buf))
    out.write("COMMIT;\n")
    return {**out.close(), "rows": n}


# --- the export -----------------------------------------------------------------------------------


@dataclass
class ExportResult:
    out: Path
    manifest: dict
    run_log: dict = field(default_factory=dict)


def _choose_reconciliation_run(run_id: str | None):
    from reconciliation.models import ReconciliationRun

    if run_id:
        run = ReconciliationRun.objects.filter(pk=run_id).first()
        if run is None:
            raise ExportError(f"unknown reconciliation run {run_id}")
    else:
        candidates = ReconciliationRun.objects.filter(
            status="COMPLETED", scope="ALL", as_of_date=AS_OF
        ).order_by("run_id")
        run = candidates.filter(triggered_by="DATAGEN").first() or candidates.last()
        if run is None:
            raise ExportError(
                "no COMPLETED ALL-scope reconciliation run as of 2026-12-31: run "
                "`manage.py run_reconciliation --as-of 2026-12-31` first (or generate without "
                "--skip-reconciliation)"
            )
    if (run.status, run.scope, run.as_of_date) != ("COMPLETED", "ALL", AS_OF):
        raise ExportError(f"{run.pk} must be a COMPLETED ALL-scope run as of {AS_OF}")
    return run


def preflight(*, config_path=None):
    """The checks of P8-D3. Returns (generation run, plan)."""
    from datagen import services

    runs = list(GenerationRun.objects.order_by("run_id"))
    if not runs:
        raise ExportError("no generation run in the database: run `manage.py generate_data` first")
    if len(runs) > 1:  # unreachable with D-G6, kept as a guard
        raise ExportError(f"{len(runs)} generation runs found; an export needs exactly one")
    run = runs[0]
    stored = services.database_checksums()
    changed = sorted(
        t for t, h in run.checksums.get("tables", {}).items() if stored["tables"].get(t) != h
    )
    if changed:
        raise ExportError(
            f"the data changed after {run.run_id} was generated, so its manifest would be false; "
            f"changed tables: {', '.join(changed)}. Export only an unmodified generated dataset "
            "(regenerate into fresh databases)."
        )
    inject = labels_of(run.pk)[2].exists()
    plan = services.plan(
        seed=run.seed,
        scale_factor=run.scale_factor,
        inject=inject,
        run_id=run.run_id,
        config_path=config_path,
    )
    if plan.settings.hash != run.config_hash:
        raise ExportError(
            f"the generation config differs from the one {run.run_id} used (config_hash "
            f"{run.config_hash[:16]}…, now {plan.settings.hash[:16]}…): pass the original file "
            "with --config"
        )
    if plan.checksums != run.checksums:
        diff = sorted(
            t for t, h in plan.checksums["tables"].items() if run.checksums["tables"].get(t) != h
        )
        raise ExportError(
            f"re-planning seed {run.seed} with this code does not reproduce {run.run_id} "
            f"(tables: {', '.join(diff)}): the generator code changed since generation; "
            "regenerate with the current code"
        )
    if services.stored_label_checksum(run.pk) != plan.labels_checksum:
        raise ExportError(f"the ground-truth labels of {run.run_id} changed after generation")
    return run, plan


def export(out, *, config_path=None, reconciliation_run: str | None = None) -> ExportResult:
    from datagen.evaluation import evaluate

    out = Path(out).resolve()
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ExportError(f"{out} already exists and is not an empty directory; choose a new --out")
    started, clock = timezone.now(), time.monotonic()
    steps: dict[str, float] = {}

    def step(name, t0):
        steps[name] = round(time.monotonic() - t0, 3)

    t0 = time.monotonic()
    run, plan = preflight(config_path=config_path)
    recon = _choose_reconciliation_run(reconciliation_run)
    step("preflight", t0)

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{out.name}.partial-", dir=out.parent))
    try:
        files: dict[str, dict] = {}

        def put(rel, info):
            files[rel] = info

        t0 = time.monotonic()
        row_counts = {}
        for model in DATASET_MODELS:
            table = model._meta.db_table
            fields, values = rows(model)
            put(
                f"dataset/csv/{table}.csv",
                _write_csv(tmp / f"dataset/csv/{table}.csv", fields, values),
            )
            fields, values = rows(model)
            put(
                f"dataset/json/{table}.json",
                _write_json(tmp / f"dataset/json/{table}.json", fields, values),
            )
            row_counts[table] = files[f"dataset/csv/{table}.csv"]["rows"]
        step("dataset_csv_json", t0)
        t0 = time.monotonic()
        put("dataset/sql/schema.sql", _write_text(tmp / "dataset/sql/schema.sql", schema_sql()))
        put("dataset/sql/seed.sql", _write_seed(tmp / "dataset/sql/seed.sql"))
        step("dataset_sql", t0)

        t0 = time.monotonic()
        label_model, gt_db, labels = labels_of(run.pk)
        for fmt, writer in (("csv", _write_csv), ("json", _write_json)):
            fields, values = rows(label_model, using=gt_db, queryset=labels)
            put(
                f"ground_truth/ground_truth.{fmt}",
                writer(tmp / f"ground_truth/ground_truth.{fmt}", fields, values),
            )
        finding_model, db, findings = findings_of(recon.pk)
        fields, values = rows(finding_model, using=db, queryset=findings)
        put("reports/findings.csv", _write_csv(tmp / "reports/findings.csv", fields, values))
        evaluation = evaluate(generation_run_id=run.pk, reconciliation_run_id=recon.pk)
        put(
            "reports/evaluation_report.json",
            _write_json_doc(tmp / "reports/evaluation_report.json", evaluation),
        )
        put(
            "reports/validation_summary.md",
            _write_text(
                tmp / "reports/validation_summary.md",
                validation_summary(run, plan, recon, evaluation),
            ),
        )
        put(
            "source_manifest.json",
            _write_json_doc(tmp / "source_manifest.json", source_manifest(plan)),
        )
        step("ground_truth_reports", t0)

        manifest = dataset_manifest(run, plan, recon, row_counts, files)
        _write_json_doc(tmp / "dataset_manifest.json", manifest)
        finished = timezone.now()
        run_log = {
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "duration_seconds": round(time.monotonic() - clock, 3),
            "step_seconds": steps,
            "output": str(out),
            "generation_run": {
                "run_id": run.run_id,
                "started_at": run.started_at.isoformat(),
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                "code_version": run.code_version,
            },
            "reconciliation_run": {
                "run_id": recon.run_id,
                "started_at": recon.started_at.isoformat(),
                "finished_at": recon.finished_at.isoformat() if recon.finished_at else None,
            },
            "environment": provenance.environment(),
        }
        _write_json_doc(tmp / "run_log.json", run_log)
        if out.exists():
            out.rmdir()
        os.replace(tmp, out)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return ExportResult(out, manifest, run_log)


def combined_digest(files: dict[str, dict]) -> str:
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(f"{rel}:{files[rel]['sha256']}\n".encode())
    return h.hexdigest()


def dataset_manifest(run, plan, recon, row_counts, files) -> dict:
    v = plan.validation
    return {
        "format_version": FORMAT_VERSION,
        "generator": "datagen (ARCHITECTURE §11)",
        "seed": run.seed,
        "scale_name": run.scale_name,
        "scale_factor": f"{run.scale_factor:.4f}",
        "config_hash": run.config_hash,
        "generation_run": run.run_id,
        "reconciliation_run": recon.run_id,
        "as_of_date": AS_OF.isoformat(),
        "excluded_columns": sorted(EXCLUDED_COLUMNS),
        "row_counts": row_counts,
        "ground_truth_rows": files["ground_truth/ground_truth.csv"]["rows"],
        "findings_rows": files["reports/findings.csv"]["rows"],
        "coverage": {
            "exact_counts": v["exact_counts"],
            "bands": v["coverage"],
            "count_targets": v["count_targets"],
            "boundary_cases": v["boundary_cases"],
        },
        "database_checksums": run.checksums,
        "files": files,
        "combined_sha256": combined_digest(files),
    }


def source_manifest(plan) -> dict:
    na = "N/A (no external data)"
    return {
        "format_version": FORMAT_VERSION,
        "provenance": "100% synthetic: generated by datagen from a seed and config/generation.yaml",
        "external_sources": [],
        "external_source_fields": {
            "url": na,
            "version": na,
            "retrieved_on": na,
            "license": na,
            "attribution": na,
            "transformations": na,
            "discarded_fields": na,
        },
        "personal_data": "none: names are drawn from synthetic lists; every email uses the "
        "reserved domain example.edu (ARCHITECTURE R-53)",
        "config_hash": plan.settings.hash,
        "decisions": "ARCHITECTURE D10: no external data",
    }


def _table(rows_, head) -> str:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows_]
    return "\n".join(out)


def validation_summary(run, plan, recon, evaluation) -> str:
    v = plan.validation
    lines = [
        "# Validation summary",
        "",
        f"Generation run {run.run_id}: seed {run.seed}, scale {run.scale_name} "
        f"({run.scale_factor:.4f}), config_hash `{run.config_hash}`. Reconciliation run "
        f"{recon.run_id} (as of {AS_OF}). Deterministic: this file depends only on seed, config "
        "and code.",
        "",
        f"Result: **{'PASSED' if v['passed'] else 'FAILED'}**"
        + ("" if v["passed"] else " — " + "; ".join(v["problems"])),
        "",
        "## Exact baseline counts (DATABASE_DESIGN §7)",
        "",
        _table(
            [(k, x["actual"], x["expected"]) for k, x in v["exact_counts"].items()],
            ["table", "actual", "expected"],
        ),
        "",
        "## Coverage bands (DATABASE_DESIGN §7)",
        "",
        _table(
            [
                (k, x["actual_pct"], x["low_pct"], x["high_pct"] or "–")
                for k, x in v["coverage"].items()
            ],
            ["measure", "actual %", "low %", "high %"],
        ),
        "",
        _table(
            [
                (k, x["actual"], x["low"], x["high"] if x["high"] is not None else "–")
                for k, x in v["count_targets"].items()
            ],
            ["count target", "actual", "low", "high"],
        ),
        "",
        "## Boundary cases (ARCHITECTURE §11.2; at least "
        f"{v['boundary_cases']['minimum_each']} each)",
        "",
        _table(sorted(v["boundary_cases"]["counts"].items()), ["case", "count"]),
        "",
        "## Distributions (ARCHITECTURE §11.2)",
        "",
        _table(
            [
                (k, x["mean"], x["p10"], x["p50"], x["p90"])
                for k, x in v["distributions"].items()
                if isinstance(x, dict)
            ],
            ["variable", "mean", "p10", "p50", "p90"],
        ),
        "",
        f"International students: {v['distributions']['international_pct']} %. "
        f"Payment profiles: {', '.join(f'{k} {n}' for k, n in v['payment_profiles'].items())}. "
        f"Eligible but not awarded: {v['eligible_not_awarded']}. "
        f"Refunds rejected at approval or processing: {v['rejected_refunds']}.",
        "",
        "## Detection (ARCHITECTURE §12.3)",
        "",
        _table(
            [
                (t, m["labels"], m["tp"], m["fp"], m["fn"], m["precision"], m["recall"], m["f1"])
                for t, m in evaluation["by_type"].items()
            ],
            ["issue type", "labels", "TP", "FP", "FN", "precision", "recall", "F1"],
        ),
        "",
        f"Consequential findings: {evaluation['consequential']}. Unexpected findings: "
        f"{evaluation['unexpected']}. Non-anomalous students reconciled: "
        f"{evaluation['students']['pct_non_anomalous_reconciled']} %. Targets met: "
        f"{'yes' if evaluation['meets_targets'] else 'no'}.",
        "",
    ]
    return "\n".join(lines)
