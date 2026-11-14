"""Generation config resolution and config_hash (ARCHITECTURE §11.1, §11.4), and the
version-independent distributions built on stage_rng (approved decision D-G2). Pure."""

from decimal import Decimal

import pytest

from datagen import rng as drng
from datagen.config import ConfigError, canonical_json, config_hash, load_raw, resolve


def test_scales_resolve_to_the_documented_volumes():
    starter, full = resolve(scale="starter"), resolve(scale="full")
    assert starter["counts"] == {
        "students": 100,
        "payments": 500,
        "awards_per_term": 50,
        "anomalies_per_type": 3,
    }
    assert full["counts"] == {
        "students": 10000,
        "payments": 50000,
        "awards_per_term": 5000,
        "anomalies_per_type": 150,
    }
    assert (starter["scale_name"], starter["scale_factor"]) == ("starter", "0.0100")
    assert len(starter["programs"]) == 20  # master data stays full size (§11.4)


def test_a_custom_factor_scales_volumes_with_a_minimum_of_three_anomalies():
    r = resolve(scale_factor="0.05")
    assert r["scale_name"] == "custom" and r["scale_factor"] == "0.0500"
    assert r["counts"]["students"] == 500 and r["counts"]["anomalies_per_type"] == 8
    assert resolve(scale_factor="0.01")["scale_name"] == "starter"


@pytest.mark.parametrize(
    "bad",
    [{"scale": "huge"}, {"scale_factor": "0"}, {"scale_factor": "2"}, {"scale_factor": "0.0001"}],
)
def test_invalid_scales_are_refused(bad):
    with pytest.raises(ConfigError):
        resolve(**{"scale": None, **bad})


def test_config_hash_is_canonical_and_excludes_the_seed():
    raw = load_raw()
    a = resolve(scale="starter", raw=raw)
    assert config_hash(a) == config_hash(resolve(scale="starter"))
    assert "seed" not in a and "scales" not in a
    assert canonical_json({"b": 1, "a": [1, 2]}) == '{"a":[1,2],"b":1}'
    raw["fees"]["LAB"] = 12001
    assert config_hash(resolve(scale="starter", raw=raw)) != config_hash(a)


def test_distributions_have_pinned_values():
    """Built only from random(): identical on every Python version (asserted on 3.14 and 3.12)."""
    r = drng.stage_rng(20261022, "students")
    assert f"{drng.truncated_normal(r, 3.0, 0.5, 0.0, 4.0):.12f}" == GOLDEN["normal"]
    assert f"{drng.beta_integer(r, 2, 3):.12f}" == GOLDEN["beta"]
    assert drng.shuffled(r, range(8)) == GOLDEN["shuffled"]
    assert drng.sample(r, list("abcdefgh"), 3) == GOLDEN["sample"]


def test_distribution_shapes():
    r = drng.stage_rng(1, "shape")
    xs = [drng.truncated_normal(r, 3.0, 0.5, 0.0, 4.0) for _ in range(20000)]
    assert all(0 <= x <= 4 for x in xs)
    # truncating N(3, 0.5) at 4 (μ + 2σ) lowers the mean by σ·φ(2)/Φ(2) ≈ 0.0276
    assert abs(sum(xs) / len(xs) - 2.9724) < 0.01
    bs = [drng.beta_integer(r, 2, 3) for _ in range(20000)]
    assert abs(sum(bs) / len(bs) - 0.4) < 0.01  # Beta(2, 3) mean = 2 / 5
    assert abs(drng._inverse_normal_cdf(0.975) - 1.959964) < 1e-6
    with pytest.raises(ValueError):
        drng.choose(r, [])
    assert Decimal(f"{xs[0]:.2f}")  # rendered without float noise


GOLDEN = {  # identical on Python 3.14.6 and 3.12.3
    "normal": "3.468201507435",
    "beta": "0.286731559275",
    "shuffled": [3, 5, 7, 1, 2, 4, 6, 0],
    "sample": ["d", "a", "h"],
}
