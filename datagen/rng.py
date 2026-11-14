"""Deterministic random streams (ARCHITECTURE §11.3, frozen).

``stage_rng(seed, stage_name)`` returns an independent ``random.Random`` per pipeline stage,
seeded from SHA-256 of ``"{seed}:{stage_name}"``. Python's ``hash()`` is never used: string
hashing is randomized per process (``PYTHONHASHSEED``).

Only ``random()``, ``randrange()`` and ``randint()`` of the returned generator are used by the
generator. The distributions below are built from ``random()`` alone, so the dataset does not
depend on how a Python version implements ``gauss``, ``betavariate`` or ``shuffle``
(approved decision D-G2: identical results on every supported Python version).
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Sequence


def stage_rng(seed: int, stage_name: str) -> random.Random:
    digest = hashlib.sha256(f"{seed}:{stage_name}".encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


# --- distributions from random() only ------------------------------------------------------------


def _inverse_normal_cdf(p: float) -> float:
    """Acklam's rational approximation of the standard normal quantile (|error| < 1.2e-9).

    Pure arithmetic on floats: the same result on every IEEE-754 platform and Python version.
    """
    if not 0.0 < p < 1.0:
        raise ValueError(f"p must be in (0, 1), got {p}")
    a = (
        -39.69683028665376,
        220.9460984245205,
        -275.9285104469687,
        138.3577518672690,
        -30.66479806614716,
        2.506628277459239,
    )
    b = (
        -54.47609879822406,
        161.5858368580409,
        -155.6989798598866,
        66.80131188771972,
        -13.28068155288572,
    )
    c = (
        -0.007784894002430293,
        -0.3223964580411365,
        -2.400758277161838,
        -2.549732539343734,
        4.374664141464968,
        2.938163982698783,
    )
    d = (0.007784695709041462, 0.3224671290700398, 2.445134137142996, 3.754408661907416)
    low = 0.02425
    if p < low:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    if p > 1 - low:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    q = p - 0.5
    r = q * q
    return (
        (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
        * q
        / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    )


def _uniform_open(rng: random.Random) -> float:
    """A float in the open interval (0, 1)."""
    while True:
        u = rng.random()
        if u > 0.0:
            return u


def truncated_normal(rng: random.Random, mean: float, sd: float, low: float, high: float) -> float:
    """Normal(mean, sd) truncated to [low, high], by rejection on the inverse CDF."""
    while True:
        x = mean + sd * _inverse_normal_cdf(_uniform_open(rng))
        if low <= x <= high:
            return x


def beta_integer(rng: random.Random, a: int, b: int) -> float:
    """Beta(a, b) for integer a, b ≥ 1: the a-th smallest of a + b − 1 uniforms."""
    draws = sorted(rng.random() for _ in range(a + b - 1))
    return draws[a - 1]


def shuffled(rng: random.Random, items: Sequence) -> list:
    """Fisher–Yates with ``randrange`` (independent of ``random.shuffle``'s implementation)."""
    out = list(items)
    for i in range(len(out) - 1, 0, -1):
        j = rng.randrange(i + 1)
        out[i], out[j] = out[j], out[i]
    return out


def choose(rng: random.Random, items: Sequence):
    if not items:
        raise ValueError("cannot choose from an empty sequence")
    return items[rng.randrange(len(items))]


def sample(rng: random.Random, items: Sequence, k: int) -> list:
    """``k`` distinct items, in a deterministic order (partial Fisher–Yates)."""
    if k > len(items):
        raise ValueError(f"cannot sample {k} of {len(items)}")
    pool = list(items)
    for i in range(k):
        j = i + rng.randrange(len(pool) - i)
        pool[i], pool[j] = pool[j], pool[i]
    return pool[:k]
