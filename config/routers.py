"""Database router that isolates ground truth (ARCHITECTURE.md §12.2).

GroundTruthLabel (app ``datagen``, model ``groundtruthlabel``) lives only in the
``ground_truth`` database. Every other model lives only in ``default``. No relation
may cross the two databases, so the detector, which only uses ``default``, has no
path to the labels.
"""

GROUND_TRUTH_DB = "ground_truth"
DEFAULT_DB = "default"
GROUND_TRUTH_MODELS = frozenset({("datagen", "groundtruthlabel")})


def _is_ground_truth(app_label: str, model_name: str | None) -> bool:
    return (app_label, (model_name or "").lower()) in GROUND_TRUTH_MODELS


def _model_key(model) -> tuple[str, str]:
    return model._meta.app_label, model._meta.model_name


class GroundTruthRouter:
    def db_for_read(self, model, **hints):
        return GROUND_TRUTH_DB if _is_ground_truth(*_model_key(model)) else DEFAULT_DB

    def db_for_write(self, model, **hints):
        return GROUND_TRUTH_DB if _is_ground_truth(*_model_key(model)) else DEFAULT_DB

    def allow_relation(self, obj1, obj2, **hints):
        # Only objects on the same side may be related; never across databases.
        return _is_ground_truth(*_model_key(obj1)) == _is_ground_truth(*_model_key(obj2))

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if db == GROUND_TRUTH_DB:
            # Only the label table; operations without a model name (RunPython,
            # RunSQL) never run against the ground-truth database.
            return model_name is not None and _is_ground_truth(app_label, model_name)
        if db == DEFAULT_DB:
            return not _is_ground_truth(app_label, model_name)
        return False
