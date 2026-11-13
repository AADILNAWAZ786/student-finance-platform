"""Student serializers. Decimals (gpa, need_index) are strings in and out (ARCHITECTURE §9.1)."""

from decimal import Decimal

from rest_framework import serializers

from academics.models import Program, Term
from core.fields import DecimalStringField
from core.serializers import StrictInputMixin
from students.models import Student, StudentStatusEvent

GPA = {
    "max_digits": 3,
    "decimal_places": 2,
    "min_value": Decimal("0.00"),
    "max_value": Decimal("4.00"),
}
NEED = {
    "max_digits": 3,
    "decimal_places": 2,
    "min_value": Decimal("0.00"),
    "max_value": Decimal("1.00"),
}


class StudentSerializer(serializers.ModelSerializer):
    program = serializers.CharField(source="program_id", read_only=True)
    admit_term = serializers.CharField(source="admit_term_id", read_only=True)
    gpa = DecimalStringField(**GPA, read_only=True)
    need_index = DecimalStringField(**NEED, read_only=True)

    class Meta:
        model = Student
        fields = [
            "student_id",
            "first_name",
            "last_name",
            "email",
            "date_of_birth",
            "residency",
            "program",
            "admit_term",
            "gpa",
            "need_index",
            "status",
        ]
        read_only_fields = fields


class StudentCreateSerializer(StrictInputMixin, serializers.Serializer):
    first_name = serializers.CharField(max_length=60)
    last_name = serializers.CharField(max_length=60)
    email = serializers.EmailField(
        max_length=120, help_text="Must use an allowed synthetic domain (example.edu)"
    )
    date_of_birth = serializers.DateField()
    residency = serializers.ChoiceField(choices=Student.Residency.choices)
    program = serializers.PrimaryKeyRelatedField(
        queryset=Program.objects.all(), help_text="Program ID, e.g. PRG-004"
    )
    admit_term = serializers.PrimaryKeyRelatedField(
        queryset=Term.objects.all(), help_text="Term ID (billable or historical)"
    )
    gpa = DecimalStringField(**GPA, help_text='String, e.g. "3.70"')
    need_index = DecimalStringField(**NEED, help_text='String, e.g. "0.45"')


class StudentContactUpdateSerializer(StrictInputMixin, serializers.Serializer):
    rejected_field_message = "Only contact fields (first_name, last_name, email) can be changed."

    first_name = serializers.CharField(max_length=60, required=False)
    last_name = serializers.CharField(max_length=60, required=False)
    email = serializers.EmailField(max_length=120, required=False)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if not attrs:
            raise serializers.ValidationError(
                {"non_field_errors": ["Provide at least one of first_name, last_name, email."]}
            )
        return attrs


class StudentStatusChangeSerializer(StrictInputMixin, serializers.Serializer):
    to_status = serializers.ChoiceField(choices=Student.Status.choices)
    reason = serializers.CharField(max_length=40, help_text="e.g. FINANCIAL_HOLD, HOLD_RELEASED")
    event_date = serializers.DateField(required=False, help_text="Business date; defaults to today")


class StudentStatusEventSerializer(serializers.ModelSerializer):
    student = serializers.CharField(source="student_id", read_only=True)

    class Meta:
        model = StudentStatusEvent
        fields = [
            "event_id",
            "student",
            "from_status",
            "to_status",
            "event_date",
            "reason",
            "actor",
            "source_ref",
        ]
        read_only_fields = fields


class StudentStatusChangeResponseSerializer(serializers.Serializer):
    student = StudentSerializer()
    event = StudentStatusEventSerializer()
