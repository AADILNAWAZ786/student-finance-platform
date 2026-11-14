"""BUSINESS_RULES.md (Phase 9) lists every rule of ARCHITECTURE §6, and cites only real
evidence: every referenced function exists, every referenced test file exists and names the
rule (or the cited test function exists), and no rule is marked Implemented without both."""

import importlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC = (ROOT / "BUSINESS_RULES.md").read_text(encoding="utf-8")
ARCH = (ROOT / "docs/ARCHITECTURE.md").read_text(encoding="utf-8")
DOTTED = re.compile(r"`([a-z_]+(?:\.[a-z_]+)+)`")
TEST_REF = re.compile(r"`(tests/[\w/]+\.py)(?:::(\w+))?`")
ISSUE_TYPES = (
    "PAYMENT_OVER_ALLOCATED",
    "SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES",
    "DUPLICATE_INVOICE",
    "REFUND_EXCEEDS_NET_PAID",
    "BALANCE_LEDGER_MISMATCH",
)


def _rows():
    for line in DOC.splitlines():
        if line.startswith("| ") and not line.startswith("| Rule") and "---" not in line:
            yield [c.strip() for c in line.strip().strip("|").split("|")]


def _resolve(dotted: str) -> bool:
    parts = dotted.split(".")
    for i in range(len(parts) - 1, 0, -1):
        try:
            obj = importlib.import_module(".".join(parts[:i]))
        except ModuleNotFoundError:
            continue
        for attr in parts[i:]:
            if not hasattr(obj, attr):
                return False
            obj = getattr(obj, attr)
        return True
    try:
        importlib.import_module(dotted)
        return True
    except ModuleNotFoundError:
        return False


def test_every_architecture_rule_is_catalogued():
    section = ARCH.split("## 6. Business rules", 1)[1].split("## 7.", 1)[0]
    ids = set(re.findall(r"^\| (BR-[A-Z]+-\d+) \|", section, re.M))
    assert len(ids) == 42
    listed = {r[0] for r in _rows() if r[0].startswith("BR-")}
    assert ids == listed, ids ^ listed


def test_every_cited_function_exists():
    names = {n for n in DOTTED.findall(DOC) if not n.endswith(".py")} - {"core.money"}
    missing = sorted(n for n in names if not n.startswith("tests") and not _resolve(n))
    assert missing == []
    assert _resolve("core.money")


def test_every_cited_test_is_real_and_about_the_rule():
    for row in _rows():
        key = row[0]
        refs = TEST_REF.findall(" ".join(row))
        if key.startswith("BR-") or any(t in " ".join(row) for t in ISSUE_TYPES):
            assert refs, f"{key}: no test cited"
            assert row[-1] == "Implemented", key
        for path, func in refs:
            text = (ROOT / path).read_text(encoding="utf-8")
            if func:
                assert re.search(rf"^def {func}\(", text, re.M), f"{path}::{func}"
            elif key.startswith("BR-"):
                assert key in text or key.replace("-", "_") in text, f"{key} not named in {path}"
            else:
                issue = next((t for t in ISSUE_TYPES if t in " ".join(row)), None)
                if issue:
                    assert issue in text, f"{issue} not in {path}"
