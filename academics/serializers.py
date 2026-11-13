"""Academics serializers. Read serializers expose business IDs; write serializers accept them."""

from rest_framework import serializers

from academics.models import Enrollment, EnrollmentEvent, Program, Term
from core.serializers import StrictInputMixin
from students.models import Student


class ProgramSerializer(serializers.ModelSerializer):
    class Meta:
        model = Program
        fields = [
            "program_id",
            "code",
            "name",
            "level",
            "department",
            "is_stem",
            "has_lab",
            "min_credits",
            "max_credits",
            "is_active",
        ]
        read_only_fields = ["program_id"]


class ProgramCreateSerializer(StrictInputMixin, serializers.ModelSerializer):
    class Meta:
        model = Program
        fields = [
            "code",
            "name",
            "level",
            "department",
            "is_stem",
            "has_lab",
            "min_credits",
            "max_credits",
            "is_active",
        ]
        extra_kwargs = {"code": {"validators": []}}  # uniqueness: service → 400 with message


class TermSerializer(serializers.ModelSerializer):
    class Meta:
        model = Term
        fields = [
            "term_id",
            "year",
            "season",
            "is_billable",
            "invoice_date",
            "due_date",
            "start_date",
            "census_date",
            "end_date",
            "late_fee_grace_days",
        ]
        read_only_fields = ["term_id"]


class TermCreateSerializer(StrictInputMixin, serializers.ModelSerializer):
    year = serializers.IntegerField(min_value=2000, max_value=2999)

    class Meta:
        model = Term
        fields = [
            "year",
            "season",
            "is_billable",
            "invoice_date",
            "due_date",
            "start_date",
            "census_date",
            "end_date",
            "late_fee_grace_days",
        ]
        validators: list = []  # (year, season) uniqueness: service → 400 with message


class EnrollmentSerializer(serializers.ModelSerializer):
    student = serializers.CharField(source="student_id", read_only=True)
    program = serializers.CharField(source="program_id", read_only=True)
    term = serializers.CharField(source="term_id", read_only=True)

    class Meta:
        model = Enrollment
        fields = [
            "enrollment_id",
            "student",
            "program",
            "term",
            "credits",
            "status",
            "enrolled_on",
            "withdrawal_date",
            "cancellation_date",
        ]
        read_only_fields = fields


class EnrollmentCreateSerializer(StrictInputMixin, serializers.Serializer):
    student = serializers.PrimaryKeyRelatedField(
        queryset=Student.objects.all(), help_text="Student ID, e.g. STU-000001"
    )
    term = serializers.PrimaryKeyRelatedField(
        queryset=Term.objects.all(), help_text="Billable term ID, e.g. TERM-2026-FALL"
    )
    credits = serializers.IntegerField(min_value=1, max_value=24)
    enrolled_on = serializers.DateField(
        required=False, help_text="Business date; defaults to today. Must be ≤ term start + 7."
    )


class EnrollmentEventSerializer(serializers.ModelSerializer):
    enrollment = serializers.CharField(source="enrollment_id", read_only=True)

    class Meta:
        model = EnrollmentEvent
        fields = [
            "event_id",
            "enrollment",
            "from_status",
            "to_status",
            "event_date",
            "reason",
            "actor",
            "source_ref",
        ]
        read_only_fields = fields
