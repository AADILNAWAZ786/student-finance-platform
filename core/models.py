"""Core infrastructure models (DATABASE_DESIGN.md §3, core).

No business models live here. ``IdSequence`` backs ``core.ids.next_id``;
``StatusEventBase`` is the abstract shape shared by every ``*Event`` table
(the concrete tables arrive with their apps from Phase 1).
"""

from django.db import models

from core.ids import ID_MAX_LENGTHS, id_validator


class IdSequence(models.Model):
    """Counter per ID scope, e.g. ``PAY:2026`` or ``INV:STU-000001:2026``.

    Internal; not exported.
    """

    key = models.CharField(max_length=80, primary_key=True)
    last_value = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "core_idsequence"
        verbose_name = "ID sequence"

    def __str__(self) -> str:
        return f"{self.key} = {self.last_value}"


class StatusEventBase(models.Model):
    """Append-only status change record. Subclasses add the FK to their entity."""

    event_id = models.CharField(
        max_length=ID_MAX_LENGTHS["status_event"],
        primary_key=True,
        validators=[id_validator("status_event")],
    )
    # NULL (not "") deliberately means "no previous status" / "no source": DATABASE_DESIGN §3
    from_status = models.CharField(max_length=20, null=True, blank=True)  # noqa: DJ001
    to_status = models.CharField(max_length=20)
    event_date = models.DateField()
    reason = models.CharField(max_length=40)
    actor = models.CharField(max_length=40)
    source_ref = models.CharField(max_length=64, null=True, blank=True)  # noqa: DJ001
    recorded_at = models.DateTimeField(auto_now_add=True)  # execution metadata; not exported

    class Meta:
        abstract = True
        ordering = ("event_date", "event_id")

    def __str__(self) -> str:
        return f"{self.event_id}: {self.from_status} -> {self.to_status}"
