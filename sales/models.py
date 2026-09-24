from django.conf import settings
from django.db import models


class Sale(models.Model):
    """Ek row = SalesPage.jsx ke sales table ki ek entry (client, project,
    amount, date, status). Client/project yahan free-text hain (FK nahi) —
    kyunke frontend ka Add/Edit Sale modal inhe plain <input list="..."> se
    leta hai, kisi existing Client/Project record ko select karke nahi."""

    class StatusChoices(models.TextChoices):
        PAID = "Paid", "Paid"
        PARTIAL = "Partial", "Partial"
        PENDING = "Pending", "Pending"

    client = models.CharField(max_length=255)
    project = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    date = models.DateField()
    status = models.CharField(max_length=10, choices=StatusChoices.choices, default=StatusChoices.PENDING)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="sales"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date", "-created_at"]
        indexes = [
            models.Index(fields=["client"]),
            models.Index(fields=["project"]),
            models.Index(fields=["status"]),
            models.Index(fields=["date"]),
        ]

    def __str__(self):
        return f"{self.client} — {self.project} — {self.amount}"
