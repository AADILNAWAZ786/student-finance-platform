"""Mandatory validation 6 (approved decision D-G2; ARCHITECTURE P4, §11.3): the same seed and
config reproduce the same data and labels — in two runs, under any PYTHONHASHSEED, and on
every supported Python version (the golden digests below are asserted on 3.14 and 3.12)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from datagen import services
from datagen.config import config_hash, resolve

ROOT = Path(__file__).resolve().parents[2]
# seed 20261022, starter scale, config/generation.yaml as committed (change both together)
GOLDEN_DATASET = "c02689f549860eb8f886011f899804a71abecc481d8df0e7ebb02a6eead7b7e3"
GOLDEN_LABELS = "160a7733fabff5d708aa3c71abed5b032991a642411b5fe2d643670a14ab4010"
GOLDEN_CONFIG = "acbe9c9ab9503071421c568056828ab666b77ad1b5c2474ff47fc8e0fd0a45c6"

SCRIPT = (
    "import django; django.setup()\n"
    "from datagen import services\n"
    "p = services.plan(seed=20261022, scale='starter')\n"
    "print(p.checksums['dataset'], p.labels_checksum)\n"
)


@pytest.fixture(scope="module")
def first():
    return services.plan(seed=20261022, scale="starter")


def test_golden_digests_on_this_python(first):
    assert first.settings.hash == config_hash(resolve(scale="starter")) == GOLDEN_CONFIG
    assert first.checksums["dataset"] == GOLDEN_DATASET
    assert first.labels_checksum == GOLDEN_LABELS


def test_two_runs_are_identical_table_by_table(first):
    second = services.plan(seed=20261022, scale="starter")
    assert second.checksums == first.checksums
    assert second.labels_checksum == first.labels_checksum
    assert second.row_counts == first.row_counts


def test_another_seed_gives_other_data_and_labels(first):
    other = services.plan(seed=7, scale="starter")
    assert other.settings.hash == first.settings.hash  # the seed is not part of the config
    assert other.checksums["dataset"] != first.checksums["dataset"]
    assert other.labels_checksum != first.labels_checksum
    assert other.row_counts["students_student"] == 100  # same exact volumes


@pytest.mark.parametrize("hashseed", ["0", "1", "12345", "random"])
def test_independent_of_python_hash_randomization(hashseed):
    env = {
        **os.environ,
        "PYTHONHASHSEED": hashseed,
        "DJANGO_SETTINGS_MODULE": "config.settings",
        "PYTHONPATH": str(ROOT),
    }
    out = subprocess.run(
        [sys.executable, "-c", SCRIPT],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert out == [GOLDEN_DATASET, GOLDEN_LABELS]


def test_planning_touches_no_database():
    """No django_db marker: pytest-django blocks every database access in this test, so
    planning (stages 1–17) provably reads and writes nothing."""
    services.plan(seed=20261022, scale="starter")
