"""verify_export (Phase 8, P8-D5): validate an exported dataset directory from its files alone.

Validation only: nothing is imported into any Django database. The SQL files are loaded into a
throw-away SQLite file in a temporary directory, used to compare and then deleted.

Checks (each reported as PASS or FAIL with its first problems):

- ``manifest``      dataset_manifest.json is present, parseable and of a known format
- ``files``         every listed file exists with its sha256 and size; no unlisted files;
                    the combined digest matches
- ``row_counts``    CSV, JSON, SQL and the manifest agree on the rows of every table
- ``csv_json``      every JSON row equals its CSV row, value by value
- ``csv_sql``       schema.sql + seed.sql load into an empty database and every row equals
                    its CSV row; SQLite's own foreign_key_check is clean
- ``ids``           primary keys are present and unique in every table
- ``foreign_keys``  every foreign key resolves to a row of the exported dataset
- ``labels``        ground truth CSV == JSON, unique label IDs, every referenced record exists
- ``separation``    no label table, column or label ID appears under dataset/
- ``reports``       findings and evaluation report agree with the manifest
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from django.db import models

from datagen import export as ex

FORMAT_VERSION = 1
UNCHECKSUMMED = ("dataset_manifest.json", "run_log.json")
LABEL_ONLY_COLUMNS = frozenset(
    {
        "label_id",
        "injection_method",
        "expected_decision",
        "rule_violated",
        "expected_value",
        "injected_value",
    }
)
LABEL_ID = re.compile(rb"\bGT-\d{4}-\d{6}\b")
MAX_SHOWN = 10  # problems listed per check; the total is always reported
csv.field_size_limit(sys.maxsize)


class VerificationError(Exception):
    """The directory cannot be verified at all (not an export)."""


@dataclass
class Check:
    name: str
    problems: list[str] = field(default_factory=list)
    total: int = 0

    def fail(self, message: str) -> None:
        self.total += 1
        if len(self.problems) < MAX_SHOWN:
            self.problems.append(message)

    @property
    def ok(self) -> bool:
        return self.total == 0


@dataclass
class Report:
    out: Path
    checks: dict[str, Check]
    tables: int = 0
    rows: int = 0

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks.values())

    def lines(self) -> list[str]:
        result = []
        for c in self.checks.values():
            result.append(f"{'PASS' if c.ok else 'FAIL'}  {c.name}")
            result.extend(f"      - {p}" for p in c.problems)
            if c.total > len(c.problems):
                result.append(f"      … and {c.total - len(c.problems)} more")
        return result


# --- value normalisation (every format to its CSV text) ------------------------------------------


def _compact(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def from_json(f: models.Field, v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(f, models.JSONField):
        return _compact(v)
    return str(v)


def from_sql(f: models.Field, v) -> str:
    if v is None:
        return ""
    if isinstance(f, models.BooleanField):
        return "true" if v else "false"
    if isinstance(f, models.DecimalField):
        return f"{Decimal(repr(v) if isinstance(v, float) else str(v)):.{f.decimal_places}f}"
    if isinstance(f, models.JSONField):
        return _compact(json.loads(v))
    return str(v)


# --- helpers ------------------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    with open(path, encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, [])
        return header, list(reader)


def _model_by_table() -> dict[str, type[models.Model]]:
    return {m._meta.db_table: m for m in ex.DATASET_MODELS}


# --- the checks ---------------------------------------------------------------------------------


def _check_files(out: Path, manifest: dict, c: Check) -> None:
    listed = manifest.get("files", {})
    present = {
        p.relative_to(out).as_posix()
        for p in out.rglob("*")
        if p.is_file() and p.relative_to(out).as_posix() not in UNCHECKSUMMED
    }
    for rel in sorted(set(listed) - present):
        c.fail(f"missing file {rel}")
    for rel in sorted(present - set(listed)):
        c.fail(f"unlisted file {rel}")
    for rel in sorted(set(listed) & present):
        path, info = out / rel, listed[rel]
        size = path.stat().st_size
        if size != info.get("bytes"):
            c.fail(f"{rel}: {size} bytes, manifest says {info.get('bytes')}")
        digest = _sha256(path)
        if digest != info.get("sha256"):
            c.fail(f"{rel}: sha256 {digest[:16]}…, manifest says {str(info.get('sha256'))[:16]}…")
    combined = ex.combined_digest(listed)
    if combined != manifest.get("combined_sha256"):
        c.fail(f"combined_sha256 is {combined}, manifest says {manifest.get('combined_sha256')}")


def _load_sql(out: Path, db_path: Path, c: Check) -> sqlite3.Connection | None:
    db = sqlite3.connect(db_path)
    try:
        db.executescript((out / "dataset/sql/schema.sql").read_text(encoding="utf-8"))
        db.executescript((out / "dataset/sql/seed.sql").read_text(encoding="utf-8"))
    except (OSError, sqlite3.Error) as exc:
        c.fail(f"schema.sql + seed.sql do not load into an empty database: {exc}")
        db.close()
        return None
    try:
        broken = db.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.Error as exc:
        c.fail(f"PRAGMA foreign_key_check failed: {exc}")
        broken = []
    for table, rowid, parent, _ in broken:
        c.fail(f"SQLite foreign_key_check: {table} rowid {rowid} has no parent in {parent}")
    return db


def verify(out) -> Report:
    out = Path(out).resolve()
    names = [
        "manifest",
        "files",
        "row_counts",
        "csv_json",
        "csv_sql",
        "ids",
        "foreign_keys",
        "labels",
        "separation",
        "reports",
    ]
    checks = {n: Check(n) for n in names}
    report = Report(out=out, checks=checks)
    if not out.is_dir():
        raise VerificationError(f"{out} is not a directory")

    # manifest ---------------------------------------------------------------------------------
    try:
        manifest = json.loads((out / "dataset_manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise VerificationError(f"no readable dataset_manifest.json in {out}: {exc}") from exc
    mc = checks["manifest"]
    required = ("format_version", "files", "row_counts", "combined_sha256", "generation_run")
    for key in required:
        if key not in manifest:
            mc.fail(f"dataset_manifest.json has no {key!r}")
    if manifest.get("format_version") != FORMAT_VERSION:
        mc.fail(f"format_version {manifest.get('format_version')!r}, expected {FORMAT_VERSION}")
    by_table = _model_by_table()
    row_counts = manifest.get("row_counts", {})
    if set(row_counts) != set(by_table):
        mc.fail(
            "manifest tables differ from the dataset tables: "
            f"extra {sorted(set(row_counts) - set(by_table))}, "
            f"missing {sorted(set(by_table) - set(row_counts))}"
        )
    if not mc.ok:
        return report  # nothing below is meaningful without a sound manifest

    _check_files(out, manifest, checks["files"])

    # tables: CSV vs JSON vs SQL, ids -----------------------------------------------------------
    rc, cj, cs, ids = (checks[n] for n in ("row_counts", "csv_json", "csv_sql", "ids"))
    pk_sets: dict[str, set[str]] = {}
    fk_values: list[tuple[str, ...]] = []  # table, pk, column, parent table, value, to_field
    files = manifest["files"]
    with tempfile.TemporaryDirectory(prefix="sf-verify-") as tmp:
        db = _load_sql(out, Path(tmp) / "verify.sqlite3", cs)
        for table in sorted(by_table):
            model = by_table[table]
            cols = ex.columns(model)
            names_ = [f.attname for f in cols]
            csv_path = out / f"dataset/csv/{table}.csv"
            json_path = out / f"dataset/json/{table}.json"
            try:
                header, csv_rows = _read_csv(csv_path)
            except OSError as exc:
                rc.fail(f"{table}: cannot read CSV ({exc})")
                continue
            if header != names_:
                cj.fail(f"{table}: CSV header {header} differs from the columns {names_}")
                continue
            report.tables += 1
            report.rows += len(csv_rows)
            expected = row_counts[table]
            listed = files.get(f"dataset/csv/{table}.csv", {}).get("rows")
            if not len(csv_rows) == listed == expected:
                rc.fail(f"{table}: CSV has {len(csv_rows)} rows, manifest says {expected}/{listed}")

            # ids
            pks = [r[0] for r in csv_rows]
            seen: set[str] = set()
            for pk in pks:
                if pk == "":
                    ids.fail(f"{table}: a row has an empty primary key")
                elif pk in seen:
                    ids.fail(f"{table}: duplicate primary key {pk}")
                seen.add(pk)
            pk_sets[table] = seen
            by_pk = {r[0]: r for r in csv_rows}

            # JSON
            try:
                json_rows = json.loads(json_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                cj.fail(f"{table}: JSON unreadable ({exc})")
                json_rows = None
            if json_rows is not None:
                if len(json_rows) != len(csv_rows):
                    rc.fail(f"{table}: JSON has {len(json_rows)} rows, CSV has {len(csv_rows)}")
                for i, obj in enumerate(json_rows):
                    if set(obj) != set(names_):
                        cj.fail(f"{table}: JSON row {i} has columns {sorted(obj)}")
                        continue
                    text = [from_json(f, obj[f.attname]) for f in cols]
                    _compare(cj, table, "JSON", by_pk, text, names_)

            # SQL
            if db is not None:
                select = ", ".join(f'"{n}"' for n in names_)
                pk_col = model._meta.pk.attname
                sql_rows = db.execute(f'SELECT {select} FROM "{table}" ORDER BY "{pk_col}"')
                n_sql = 0
                for row in sql_rows:
                    n_sql += 1
                    text = [from_sql(f, v) for f, v in zip(cols, row, strict=True)]
                    _compare(cs, table, "SQL", by_pk, text, names_)
                if n_sql != len(csv_rows):
                    rc.fail(f"{table}: SQL has {n_sql} rows, CSV has {len(csv_rows)}")

            # foreign-key values, resolved once every table's ids are known
            for i, f in enumerate(cols):
                if isinstance(f, models.ForeignKey):
                    parent = f.related_model._meta.db_table
                    target = f.target_field.attname
                    for r in csv_rows:
                        if r[i] != "":
                            fk_values.append((table, r[0], f.attname, parent, r[i], target))
        if db is not None:
            db.close()

    fk = checks["foreign_keys"]
    for table, pk, column, parent, value, target in fk_values:
        if parent not in pk_sets:
            fk.fail(f"{table}.{column} references {parent}, which is not in the export")
        elif target != by_table[parent]._meta.pk.attname:
            fk.fail(f"{table}.{column} references {parent}.{target}, not its primary key")
        elif value not in pk_sets[parent]:
            fk.fail(f"{table} {pk}: {column}={value} has no row in {parent}")

    _check_labels(out, manifest, by_table, pk_sets, checks["labels"])
    _check_separation(out, by_table, checks["separation"])
    _check_reports(out, manifest, checks["reports"])
    return report


def _compare(c: Check, table: str, fmt: str, by_pk: dict, text: list[str], names: list[str]):
    expected = by_pk.get(text[0])
    if expected is None:
        c.fail(f"{table}: {fmt} row {text[0]} is not in the CSV")
        return
    for name, want, got in zip(names, expected, text, strict=True):
        if want != got:
            c.fail(f"{table} {text[0]}: {name} is {got!r} in {fmt}, {want!r} in CSV")
            return


def _check_labels(out: Path, manifest: dict, by_table, pk_sets, c: Check) -> None:
    from datagen.evaluation import labels_of

    label_model = labels_of("")[0]
    cols = ex.columns(label_model)
    names = [f.attname for f in cols]
    try:
        header, rows = _read_csv(out / "ground_truth/ground_truth.csv")
        data = json.loads((out / "ground_truth/ground_truth.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        c.fail(f"ground truth unreadable: {exc}")
        return
    if header != names:
        c.fail(f"ground_truth.csv header {header} differs from {names}")
        return
    if len(rows) != manifest.get("ground_truth_rows") or len(data) != len(rows):
        c.fail(
            f"ground truth has {len(rows)} CSV / {len(data)} JSON rows, "
            f"manifest says {manifest.get('ground_truth_rows')}"
        )
    by_id = {r[0]: r for r in rows}
    if len(by_id) != len(rows):
        c.fail("ground truth label_id values are not unique")
    for obj in data:
        _compare(
            c,
            "ground_truth",
            "JSON",
            by_id,
            [from_json(f, obj.get(f.attname)) for f in cols],
            names,
        )
    model_tables = {m.__name__: t for t, m in by_table.items()}
    all_ids = set().union(*pk_sets.values()) if pk_sets else set()
    gen = manifest.get("generation_run")
    for obj in data:
        label = obj.get("label_id")
        if obj.get("generation_run_id") != gen:
            c.fail(f"{label}: generation_run_id {obj.get('generation_run_id')} is not {gen}")
        table = model_tables.get(obj.get("entity_type"))
        if table is None:
            c.fail(f"{label}: entity_type {obj.get('entity_type')!r} is not an exported model")
        elif obj.get("record_id") not in pk_sets.get(table, ()):
            c.fail(f"{label}: record {obj.get('record_id')} is not in {table}")
        if obj.get("student_id") not in pk_sets.get("students_student", ()):
            c.fail(f"{label}: student {obj.get('student_id')} is not in students_student")
        for rid in obj.get("related_ids") or []:
            if rid not in all_ids:
                c.fail(f"{label}: related id {rid} is not in the dataset")


def _non_dataset_tables() -> set[str]:
    """Tables that must never be exported as dataset: labels, findings and the run records."""
    from datagen.evaluation import findings_of, labels_of
    from datagen.models import GenerationRun

    finding = findings_of("")[0]
    run = finding._meta.get_field("run").related_model
    return {
        labels_of("")[0]._meta.db_table,
        finding._meta.db_table,
        run._meta.db_table,
        GenerationRun._meta.db_table,
    }


def _check_separation(out: Path, by_table, c: Check) -> None:
    expected = {f"csv/{t}.csv" for t in by_table} | {f"json/{t}.json" for t in by_table}
    expected |= {"sql/schema.sql", "sql/seed.sql"}
    dataset = out / "dataset"
    for p in sorted(dataset.rglob("*")) if dataset.is_dir() else []:
        if p.is_file():
            rel = p.relative_to(dataset).as_posix()
            if rel not in expected:
                c.fail(f"dataset/{rel} is not a dataset table file")
            with open(p, "rb") as fh:
                for n, line in enumerate(fh, 1):
                    m = LABEL_ID.search(line)
                    if m:
                        c.fail(f"dataset/{rel} line {n} contains label id {m.group().decode()}")
                        break
    forbidden = _non_dataset_tables()
    for table, model in by_table.items():
        if table in forbidden:
            c.fail(f"{table} is a label, finding or run table, not dataset")
        leaked = LABEL_ONLY_COLUMNS & {f.attname for f in ex.columns(model)}
        if leaked:
            c.fail(f"{table} has label columns {sorted(leaked)}")
    for path in (dataset / "csv").glob("*.csv") if dataset.is_dir() else []:
        with open(path, encoding="utf-8", newline="") as fh:
            header = next(csv.reader(fh), [])
        leaked = LABEL_ONLY_COLUMNS & set(header)
        if leaked:
            c.fail(f"dataset/csv/{path.name} has label columns {sorted(leaked)}")
    schema = dataset / "sql/schema.sql"
    if schema.is_file():
        text = schema.read_text(encoding="utf-8")
        for name in sorted(forbidden | LABEL_ONLY_COLUMNS):
            if f'"{name}"' in text:
                c.fail(f"dataset/sql/schema.sql mentions {name}")


def _check_reports(out: Path, manifest: dict, c: Check) -> None:
    try:
        _, findings = _read_csv(out / "reports/findings.csv")
        evaluation = json.loads((out / "reports/evaluation_report.json").read_text("utf-8"))
    except (OSError, ValueError) as exc:
        c.fail(f"reports unreadable: {exc}")
        return
    if len(findings) != manifest.get("findings_rows"):
        c.fail(
            f"findings.csv has {len(findings)} rows, manifest says {manifest.get('findings_rows')}"
        )
    for key in ("generation_run", "reconciliation_run"):
        if evaluation.get(key) != manifest.get(key):
            c.fail(f"evaluation_report {key} {evaluation.get(key)} != manifest {manifest.get(key)}")
    runs = {r[1] for r in findings}
    if runs - {manifest.get("reconciliation_run")}:
        c.fail(f"findings.csv holds findings of other runs: {sorted(runs)}")
