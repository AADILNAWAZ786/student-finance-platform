"""``python manage.py run_reconciliation``: the detector (ARCHITECTURE §11.1), the same engine as
``POST /api/v1/reconciliation/runs/`` (``reconciliation.services.run_reconciliation``), with
``triggered_by = COMMAND`` (decision D-R12). Prints the run's summary; exits non-zero when
the run FAILED. Default database only."""

import json
from datetime import date

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from reconciliation import policy, services
from students.models import Student


class Command(BaseCommand):
    help = "Run reconciliation (every check) for every student or one, as of a date."

    def add_arguments(self, parser):
        parser.add_argument("--scope", choices=policy.SCOPES, default=policy.ALL)
        parser.add_argument("--student", help="Student ID (required with --scope STUDENT)")
        parser.add_argument(
            "--as-of", required=True, type=date.fromisoformat, help="YYYY-MM-DD (2026)"
        )

    def handle(self, *args, scope, student, as_of, **options):
        target = None
        if student:
            target = Student.objects.filter(pk=student).first()
            if target is None:
                raise CommandError(f"Unknown student {student}")
        try:
            run = services.run_reconciliation(
                scope=scope, student=target, as_of_date=as_of, triggered_by=policy.COMMAND
            )
        except ValidationError as exc:
            raise CommandError(json.dumps(exc.message_dict, sort_keys=True)) from exc
        s = run.summary
        if run.status == policy.FAILED:
            raise CommandError(f"{run.run_id} FAILED: {json.dumps(s.get('error'), sort_keys=True)}")
        self.stdout.write(
            f"{run.run_id} {run.status} scope={run.scope} as_of={run.as_of_date} "
            f"findings={s['findings_total']} "
            f"students_reconciled={s['students']['reconciled']}/{s['students']['total']} "
            f"({s['students']['pct_reconciled']}%)"
        )
        for issue_type, count in s["findings_by_issue_type"].items():
            if count:
                self.stdout.write(f"  {issue_type}: {count}")
