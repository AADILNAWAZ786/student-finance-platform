"""Generation config: load ``config/generation.yaml`` and resolve it for one scale.

The resolved config is plain JSON data (strings for decimals and dates, so nothing depends on
float formatting). ``config_hash`` = SHA-256 of its canonical JSON: sorted keys, no
whitespace (ARCHITECTURE §11.4). The seed is not part of the config: the same config with
another seed has the same hash and different data.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "generation.yaml"
DEFAULT_SEED = 20261022
SCALE_NAMES = ("starter", "full")


class ConfigError(ValueError):
    """The requested scale or the config file is invalid."""


def load_raw(path: Path | str | None = None) -> dict:
    """The raw YAML config (``config/generation.yaml`` unless ``path`` is given)."""
    path = Path(path) if path else CONFIG_PATH
    try:
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: the top level must be a mapping")
    return raw


def _scaled(full: int, factor: Decimal) -> int:
    return int((Decimal(full) * factor).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def resolve(
    *, scale: str | None = "starter", scale_factor=None, raw: dict | None = None, path=None
) -> dict:
    """The resolved config for ``scale`` (``starter`` / ``full``) or an explicit factor."""
    raw = copy.deepcopy(raw if raw is not None else load_raw(path))
    from datagen.config_schema import check  # noqa: PLC0415 - schema imports domain policies

    problems = check(raw)
    if problems:
        raise ConfigError(
            f"invalid generation config ({len(problems)} problem(s)):\n  " + "\n  ".join(problems)
        )
    if scale_factor is not None:
        factor = Decimal(str(scale_factor))
        name = next((n for n in SCALE_NAMES if Decimal(str(raw["scales"][n])) == factor), "custom")
    else:
        if scale not in SCALE_NAMES:
            raise ConfigError(f"scale must be one of {', '.join(SCALE_NAMES)}, got {scale!r}")
        name, factor = scale, Decimal(str(raw["scales"][scale]))
    if not Decimal("0") < factor <= Decimal("1"):
        raise ConfigError(f"scale factor must be in (0, 1], got {factor}")
    factor = factor.quantize(Decimal("0.0001"))
    v = raw["volumes"]
    counts = {
        "students": _scaled(v["students"], factor),
        "payments": _scaled(v["payments"], factor),
        "awards_per_term": _scaled(v["awards_per_term"], factor),
        "anomalies_per_type": max(
            v["anomalies_min_per_type"], _scaled(v["anomalies_per_type"], factor)
        ),
    }
    if counts["students"] < 10:
        raise ConfigError("the scale factor gives fewer than 10 students")
    raw.pop("seed", None)
    raw.pop("scales", None)
    return {**raw, "scale_name": name, "scale_factor": str(factor), "counts": counts}


def canonical_json(data) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def config_hash(resolved: dict) -> str:
    return hashlib.sha256(canonical_json(resolved).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Settings:
    """Typed access to the resolved config used by the pipeline."""

    seed: int
    resolved: dict

    @property
    def counts(self) -> dict:
        return self.resolved["counts"]

    def dec(self, *path) -> Decimal:
        return Decimal(str(self.get(*path)))

    def get(self, *path):
        node = self.resolved
        for p in path:
            node = node[p]
        return node

    def day(self, *path) -> date:
        return date.fromisoformat(str(self.get(*path)))

    @property
    def hash(self) -> str:
        return config_hash(self.resolved)
