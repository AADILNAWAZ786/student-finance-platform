"""Load the 12 documented terms (3 billable 2026 terms + 9 historical admit terms).

Idempotent: existing terms are left untouched and compared with the calendar; any
difference is reported (and makes the command fail with --strict).
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from academics.calendar import standard_calendar
from academics.models import Term
from academics.services import create_term


class Command(BaseCommand):
    help = "Create the standard term calendar (ARCHITECTURE D1, D11)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Fail if an existing term differs from the calendar.",
        )

    @transaction.atomic
    def handle(self, *args, strict=False, **options):
        created = existing = 0
        mismatches = []
        for spec in standard_calendar():
            term = Term.objects.filter(pk=spec.term_id).first()
            if term is None:
                fields = spec.as_dict()
                fields.pop("term_id")
                create_term(**fields)
                created += 1
                continue
            existing += 1
            diff = [f for f, v in spec.as_dict().items() if getattr(term, f) != v]
            if diff:
                mismatches.append(f"{spec.term_id}: {', '.join(diff)}")
        self.stdout.write(f"terms created: {created}, already present: {existing}")
        for m in mismatches:
            self.stdout.write(self.style.WARNING(f"differs from calendar: {m}"))
        if mismatches and strict:
            raise CommandError(f"{len(mismatches)} term(s) differ from the calendar")
