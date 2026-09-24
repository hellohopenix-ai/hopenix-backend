from rest_framework import serializers

from .models import Sale


class SaleSerializer(serializers.ModelSerializer):
    createdBy = serializers.CharField(source="created_by.name", read_only=True, default="")

    class Meta:
        model = Sale
        fields = ["id", "client", "project", "amount", "date", "status", "createdBy", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_amount(self, value):
        if value is None or value <= 0:
            raise serializers.ValidationError("Amount must be greater than zero.")
        return value

    def validate_client(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Client is required.")
        return value

    def validate_project(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Project / service is required.")
        return value
