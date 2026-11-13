"""Opt-in: create the 7 documented scholarship types SCT-001..SCT-007 (DATABASE_DESIGN §3).

Deterministic and idempotent (decision D-S10):
  * no scholarship types yet  → creates SCT-001..SCT-007 in order, exactly as documented;
    SCT-004 (STEM Award) is linked to every program with is_stem = true at that moment;
  * exactly the documented 7 already exist (same criteria) → changes nothing;
  * anything else (other or different types) → refuses and changes nothing.
It is never run automatically. Categories follow each type's documented criteria (the
design table lists criteria, not categories); SCT-007 is the single ALL_FEE_LINES type.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal as D

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from academics.models import Program
from scholarships.models import ScholarshipType
from scholarships.services import create_scholarship_type

FROM = date(2026, 1, 1)


@dataclass(frozen=True)
class Spec:
    scholarship_type_id: str
    name: str
    category: str
    exclusive_group: str | None
    min_gpa: D | None
    max_gpa: D | None
    min_need_index: D | None
    residency: str
    first_term_only: bool
    program_restricted: bool
    coverage_scope: str
    award_basis: str
    award_value: D
    max_amount_per_term: D
    priority: int


E, A = "ELIGIBLE_LINES_ONLY", "ALL_FEE_LINES"
SPECS = (
    Spec("SCT-001", "Merit Excellence", "MERIT", "MERIT", D("3.70"), None, None,
         "ALL", False, False, E, "PERCENT", D("50"), D("100000.00"), 1),
    Spec("SCT-002", "Merit Achievement", "MERIT", "MERIT", D("3.30"), D("3.70"), None,
         "ALL", False, False, E, "PERCENT", D("25"), D("50000.00"), 2),
    Spec("SCT-003", "Need Grant", "NEED", None, None, None, D("0.60"),
         "ALL", False, False, E, "FIXED", D("40000.00"), D("40000.00"), 3),
    Spec("SCT-004", "STEM Award", "PROGRAM", None, D("3.00"), None, None,
         "ALL", False, True, E, "FIXED", D("20000.00"), D("20000.00"), 4),
    Spec("SCT-005", "International Award", "INTERNATIONAL", None, D("3.20"), None, None,
         "INTERNATIONAL", False, False, E, "PERCENT", D("15"), D("75000.00"), 5),
    Spec("SCT-006", "First-Term Bursary", "BURSARY", None, None, None, D("0.40"),
         "ALL", True, False, E, "FIXED", D("15000.00"), D("15000.00"), 6),
    Spec("SCT-007", "Full-Ride", "FULL_RIDE", "FULL_RIDE", D("3.85"), None, D("0.85"),
         "ALL", False, False, A, "PERCENT", D("100"), D("600000.00"), 0),
)  # fmt: skip
COMPARED = [f for f in Spec.__dataclass_fields__ if f != "scholarship_type_id"]


def differences(spec: Spec, row: ScholarshipType) -> list[str]:
    diff = [f for f in COMPARED if getattr(row, f) != getattr(spec, f)]
    if row.effective_from != FROM or row.effective_to is not None:
        diff.append("effective window")
    return diff


class Command(BaseCommand):
    help = "Create the documented scholarship types SCT-001..SCT-007 (opt-in, idempotent)."

    @transaction.atomic
    def handle(self, *args, **options):
        existing = {t.scholarship_type_id: t for t in ScholarshipType.objects.all()}
        documented = {s.scholarship_type_id for s in SPECS}
        if existing:
            problems = [
                f"{tid}: not a documented type" for tid in sorted(set(existing) - documented)
            ]
            problems += [
                f"{s.scholarship_type_id}: missing"
                for s in SPECS
                if s.scholarship_type_id not in existing
            ]
            for spec in SPECS:
                if spec.scholarship_type_id in existing:
                    diff = differences(spec, existing[spec.scholarship_type_id])
                    if diff:
                        problems.append(f"{spec.scholarship_type_id}: differs in {', '.join(diff)}")
            if problems:
                raise CommandError(
                    "Refusing to seed: scholarship types already exist and are not exactly the "
                    "documented set. " + "; ".join(problems)
                )
            self.stdout.write(
                "scholarship types already seeded: 7 documented types, nothing changed"
            )
            return

        stem = sorted(Program.objects.filter(is_stem=True).values_list("program_id", flat=True))
        if not stem:
            raise CommandError(
                "SCT-004 (STEM Award) needs at least one STEM program; create programs first."
            )
        for spec in SPECS:
            fields = {k: v for k, v in vars(spec).items() if k != "scholarship_type_id"}
            created = create_scholarship_type(
                **fields,
                effective_from=FROM,
                effective_to=None,
                program_ids=stem if spec.program_restricted else [],
            )
            if created.scholarship_type_id != spec.scholarship_type_id:
                raise CommandError(
                    f"expected {spec.scholarship_type_id}, got {created.scholarship_type_id} "
                    "(the SCT ID sequence is not at its start); nothing was changed"
                )
        self.stdout.write(f"scholarship types created: 7 (SCT-004 linked to {', '.join(stem)})")
