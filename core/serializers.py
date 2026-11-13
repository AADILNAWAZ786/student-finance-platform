"""Serializers for core endpoints and the shared error envelope (used in API docs)."""

from rest_framework import serializers


class StrictInputMixin:
    """Reject request fields the serializer does not accept (instead of silently ignoring them).

    ``rejected_field_message`` lets a serializer explain why, e.g. "contact fields only".
    """

    rejected_field_message = "Unknown field."

    def validate(self, attrs):
        writable = {name for name, f in self.fields.items() if not f.read_only}
        extra = sorted(set(getattr(self, "initial_data", {}) or {}) - writable)
        if extra:
            raise serializers.ValidationError(
                {name: [self.rejected_field_message] for name in extra}
            )
        return super().validate(attrs)


class ErrorDetailSerializer(serializers.Serializer):
    code = serializers.CharField()
    rule_id = serializers.CharField(allow_null=True)
    message = serializers.CharField()
    details = serializers.JSONField()


class ErrorEnvelopeSerializer(serializers.Serializer):
    error = ErrorDetailSerializer()


class DatabaseHealthSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=["ok", "error"])
    journal_mode = serializers.CharField(allow_null=True)
    foreign_keys = serializers.BooleanField(allow_null=True)
    transaction_mode = serializers.CharField(allow_null=True)
    error = serializers.CharField(required=False)


class HealthSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=["ok", "degraded"])
    service = serializers.CharField()
    version = serializers.CharField()
    api_version = serializers.CharField()
    django_version = serializers.CharField()
    sqlite_version = serializers.CharField()
    databases = serializers.DictField(child=DatabaseHealthSerializer())
