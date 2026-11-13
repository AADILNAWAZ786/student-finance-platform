"""ARCHITECTURE.md §11.3: SHA-256-seeded stage RNG is stable across processes.

Contract under test: datagen.rng.stage_rng(seed, stage_name) -> random.Random
Golden values computed from the documented algorithm with seed 20261022.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from datagen.rng import stage_rng

SEED = 20261022
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_golden_values():
    r = stage_rng(SEED, "payments")
    assert r.random() == 0.7898218038574595
    assert r.randrange(1_000_000) == 996729
    assert stage_rng(SEED, "students").random() == 0.8254671368282721


def test_same_inputs_same_stream():
    a, b = stage_rng(SEED, "invoices"), stage_rng(SEED, "invoices")
    assert [a.random() for _ in range(5)] == [b.random() for _ in range(5)]


def test_stages_are_independent_streams():
    assert stage_rng(SEED, "invoices").random() != stage_rng(SEED, "payments").random()


def test_different_seed_different_stream():
    assert stage_rng(SEED, "payments").random() != stage_rng(SEED + 1, "payments").random()


@pytest.mark.parametrize("hashseed", ["0", "1", "12345", "random"])
def test_independent_of_python_hash_randomization(hashseed):
    code = f"from datagen.rng import stage_rng; print(repr(stage_rng({SEED}, 'payments').random()))"
    env = {**os.environ, "PYTHONHASHSEED": hashseed}
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert out == "0.7898218038574595"
