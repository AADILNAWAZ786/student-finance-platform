"""The detector's check families (ARCHITECTURE §3 ``reconciliation/checks/``: one module per
family). Importing this package registers every check."""

from reconciliation.checks import (  # noqa: F401  (registration side effect)
    arithmetic,
    lifecycle,
    relationship,
    required,
    service_rules,
    structural,
    temporal,
)
from reconciliation.checks.base import REGISTRY, Check, Finding, ordered_checks

__all__ = ["REGISTRY", "Check", "Finding", "ordered_checks"]
