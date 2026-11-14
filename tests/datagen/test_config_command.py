"""generate_data --config (Phase 8, P8-D6): an invalid config is refused before anything is
written; a valid alternative config is used and identified by its hash."""

import pytest
import yaml
from django.core.management import CommandError, call_command

from datagen import services
from datagen.config import config_hash, load_raw, resolve
from datagen.models import GenerationRun

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "ground_truth"])


def test_an_invalid_config_is_refused_and_nothing_is_written(tmp_path):
    raw = load_raw()
    raw["payments"]["profiles"]["FULL"] = "0.90"
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(CommandError, match="payments.profiles: shares sum to 1.40, must be 1"):
        call_command("generate_data", "--config", str(path))
    assert services.nonempty_tables() == []


def test_a_valid_alternative_config_is_used(tmp_path):
    raw = load_raw()
    raw["fees"]["LIBRARY"] = 2500
    path = tmp_path / "alt.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    call_command("generate_data", "--config", str(path), "--skip-reconciliation")
    run = GenerationRun.objects.get()
    assert run.config_hash == config_hash(resolve(scale="starter", path=path))
    assert run.config_hash != config_hash(resolve(scale="starter"))
    assert run.code_version is None or len(run.code_version) == 40
