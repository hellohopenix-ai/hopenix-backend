from rest_framework import serializers

from .models import Expense


class ExpenseSerializer(serializers.ModelSerializer):
    """camelCase output so this is a drop-in JSON shape for
    ExpensesPage.jsx — same pattern as tasks.TaskSerializer elsewhere in
    this project. The page's `receipt`/`receiptData`/`receiptName`
    fields become `receiptType`/`receiptUrl`/`receiptName` here:
    receiptData used to be a base64 string kept in localStorage,
    receiptUrl is now a real link to the uploaded file on disk."""

    receiptType = serializers.CharField(source="receipt_type", read_only=True)
    receiptUrl = serializers.SerializerMethodField()
    receiptName = serializers.CharField(source="receipt_name", read_only=True)
    createdBy = serializers.CharField(source="created_by.name", read_only=True, default="")
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    updatedAt = serializers.DateTimeField(source="updated_at", read_only=True)

    class Meta:
        model = Expense
        fields = [
            "id", "title", "category", "project", "amount", "date",
            "payment", "status",
            "receiptType", "receiptUrl", "receiptName",
            "createdBy", "createdAt", "updatedAt",
        ]

    def get_receiptUrl(self, obj):
        if not obj.receipt_file:
            return None
        request = self.context.get("request")
        url = obj.receipt_file.url
        return request.build_absolute_uri(url) if request else url

    def validate_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError("Amount must be greater than zero.")
        return value

    def validate_status(self, value):
        # Only admin/accountant can approve or reject an expense —
        # mirrors the role check other ViewSets in this backend already
        # do (e.g. projects.ProjectViewSet.destroy). Everyone else's
        # expenses go in (and stay) Pending until reviewed.
        request = self.context.get("request")
        if request and value != "Pending" and request.user.role not in ("admin", "accountant"):
            raise serializers.ValidationError(
                "Only an admin or accountant can approve or reject an expense."
            )
        return value
