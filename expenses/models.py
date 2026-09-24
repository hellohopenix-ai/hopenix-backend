import uuid
from django.conf import settings
from django.db import models


class PaymentMethod(models.TextChoices):
    CASH = "Cash", "Cash"
    CARD = "Card", "Card"
    BANK_TRANSFER = "Bank Transfer", "Bank Transfer"
    CASH_IN_HAND = "Cash in Hand", "Cash in Hand"


class ExpenseStatus(models.TextChoices):
    APPROVED = "Approved", "Approved"
    PENDING = "Pending", "Pending"
    REJECTED = "Rejected", "Rejected"


def expense_receipt_path(instance, filename):
    # media/expenses/<expense_id>/receipts/<uuid>_<filename> — same
    # per-record folder pattern as projects.project_brief_path. The
    # actual bytes live on disk under MEDIA_ROOT; Postgres only ever
    # stores this relative path (no more base64 data: URLs in
    # localStorage like the old frontend-only version did).
    return f"expenses/{instance.id or 'new'}/receipts/{uuid.uuid4()}_{filename}"


class Expense(models.Model):
    """Backs ExpensesPage.jsx. Field names/shape are chosen so the
    page's existing expense object (title, category, project, amount,
    date, payment, status, receipt/receiptData/receiptName) maps onto
    this with only the localStorage read/write swapped for real API
    calls — same drop-in approach tasks.Task and projects.Project
    already use elsewhere in this backend.

    category is a plain CharField, not choices/FK — ExpensesPage.jsx
    lets someone type a brand-new category on the fly (customCategories
    in its old localStorage version) and this keeps that working;
    distinct values already in the table are exposed by the ViewSet's
    `filters` action instead of a fixed list.
    """

    title = models.CharField(max_length=255)
    category = models.CharField(max_length=100)
    project = models.CharField(max_length=255, blank=True, default="")

    amount = models.DecimalField(max_digits=12, decimal_places=2)
    date = models.DateField()

    payment = models.CharField(max_length=20, choices=PaymentMethod.choices, default=PaymentMethod.CASH)
    status = models.CharField(max_length=20, choices=ExpenseStatus.choices, default=ExpenseStatus.PENDING)

    # Real uploaded file (image or pdf) — mirrors projects.Project's
    # brief/completed_zip FileField pattern.
    receipt_file = models.FileField(upload_to=expense_receipt_path, null=True, blank=True)
    receipt_name = models.CharField(max_length=255, blank=True, default="")
    receipt_content_type = models.CharField(max_length=100, blank=True, default="")

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="expenses"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date", "-id"]
        indexes = [
            models.Index(fields=["category"]),
            models.Index(fields=["project"]),
            models.Index(fields=["status"]),
            models.Index(fields=["date"]),
        ]

    def __str__(self):
        return f"{self.title} - PKR {self.amount}"

    @property
    def receipt_type(self):
        """image | pdf | upload — matches ReceiptCell's three icon
        states on the frontend. "upload" also covers "no receipt
        attached yet" (the dashed upload-prompt icon)."""
        if not self.receipt_file:
            return "upload"
        if self.receipt_content_type == "application/pdf" or self.receipt_file.name.lower().endswith(".pdf"):
            return "pdf"
        return "image"