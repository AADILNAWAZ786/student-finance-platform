"""Phase 8 (P8-D6): invalid generation configs are refused with every problem named, before
anything is generated."""

import copy

import pytest

from datagen.config import ConfigError, load_raw, resolve
from datagen.config_schema import check


@pytest.fixture
def raw():
    return copy.deepcopy(load_raw())


def problems(raw):
    return check(raw)


def test_the_committed_config_is_valid(raw):
    assert problems(raw) == []


def test_missing_and_unknown_keys(raw):
    del raw["refunds"]["approve_after_days"]
    raw["payments"]["payer_mixx"] = raw["payments"].pop("payer_mix")
    assert problems(raw) == [
        "payments.payer_mix: required",
        "payments.payer_mixx: unknown key",
        "refunds.approve_after_days: required",
    ]


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda r: r["payments"]["profiles"].update(FULL="0.48"),
         "payments.profiles: shares sum to 0.98, must be 1"),
        (lambda r: r["payments"]["payer_mix"].update(SELF="0.80"),
         "payments.payer_mix: shares sum to 1.10, must be 1"),
        (lambda r: r["students"].update(international_share="1.5"),
         "students.international_share: must be a quoted decimal between 0 and 1"),
        (lambda r: r["refunds"].update(request_after_days=[5, 1]),
         "refunds.request_after_days: low must not exceed high"),
        (lambda r: r["fees"].update(effective_from="2025-12-01"),
         "fees.effective_from: must be an ISO date in 2026"),
        (lambda r: r.update(snapshots=["2026-05-09", "2027-01-01"]),
         "snapshots: must be a non-empty list of ISO dates in 2026"),
        (lambda r: r.update(as_of_date="2026-11-30"),
         "as_of_date: must be 2026-12-31 (ARCHITECTURE D7; FROZEN)"),
        (lambda r: r["programs"].pop(),
         "programs: must list exactly 20 programs (FROZEN), got 19"),
        (lambda r: r["programs"][0].update(stem=False),
         "programs: exactly 8 must be STEM (FROZEN)"),
        (lambda r: r["programs"][0].update(tuition=15000),
         "programs[0].tuition: ₹3,000–₹12,000 per credit (FROZEN)"),
        (lambda r: r["programs"][0].update(min=4),
         "programs[0]: credits must satisfy 6 ≤ min ≤ max ≤ 24 (FROZEN)"),
        (lambda r: r["payments"]["methods"].update(SELF=["UPI", "BITCOIN"]),
         "payments.methods.SELF: must be a non-empty subset of"),
        (lambda r: r["waivers"].update(reasons=["BIRTHDAY"]),
         "waivers.reasons: must be a non-empty subset of"),
        (lambda r: r["volumes"].update(awards_per_term=80000),
         "volumes.awards_per_term: cannot exceed 7 × students"),
        (lambda r: r["volumes"].update(students=0),
         "volumes.students: must be an integer ≥ 1"),
        (lambda r: r["exits"].update(boundary_days=[0, 7, 90]),
         "exits.boundary_days: must not exceed exits.max_withdrawal_day"),
        (lambda r: r["students"].update(age_at_admit=[14, 30]),
         "students.age_at_admit: must lie within 16–59"),
        (lambda r: r["students"]["gpa"].update(max=5.0),
         "students.gpa: need 0 ≤ min < max ≤ 4 and sd > 0"),
        (lambda r: r["students"].update(need_index_beta=[0, 3]),
         "students.need_index_beta: both parameters must be ≥ 1"),
    ],
)  # fmt: skip
def test_each_rule_names_the_path_and_the_problem(raw, mutate, expected):
    mutate(raw)
    found = problems(raw)
    assert any(p.startswith(expected) for p in found), found


def test_every_problem_is_reported_at_once_and_resolve_refuses(raw):
    raw["volumes"]["students"] = -1
    raw["payments"]["profiles"]["FULL"] = "0.10"
    raw["anomalies"]["balance_delta"] = [9, 1]
    with pytest.raises(ConfigError) as refused:
        resolve(scale="starter", raw=raw)
    message = str(refused.value)
    # the negative student count also breaks awards ≤ 7 × students: 4 problems, all listed
    assert message.startswith("invalid generation config (4 problem(s)):")
    for fragment in ("volumes.students", "payments.profiles", "anomalies.balance_delta",
                     "volumes.awards_per_term"):  # fmt: skip
        assert fragment in message


def test_unreadable_config_files(tmp_path):
    with pytest.raises(ConfigError, match="config file not found"):
        resolve(scale="starter", path=tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("volumes: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="is not valid YAML"):
        resolve(scale="starter", path=bad)
    scalar = tmp_path / "scalar.yaml"
    scalar.write_text("42\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="the top level must be a mapping"):
        resolve(scale="starter", path=scalar)


def test_an_alternative_config_file_changes_the_hash(tmp_path):
    import yaml

    raw = load_raw()
    raw["fees"]["LIBRARY"] = 2500
    path = tmp_path / "alt.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=True), encoding="utf-8")
    from datagen.config import config_hash

    default, alt = resolve(scale="starter"), resolve(scale="starter", path=path)
    assert config_hash(alt) != config_hash(default)
    assert alt["fees"]["LIBRARY"] == 2500
