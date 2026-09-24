"""python manage.py seed_sales           -> adds ~35 realistic sale records (skips if data already exists)
python manage.py seed_sales --reset   -> wipes existing sales first, then seeds fresh
Isse SalesPage.jsx pehli dafa load hote hi khaali table ki bajaye asal
(realistic) data dikhata hai — frontend ka hardcoded SEED_SALES ab sirf
DB seed karne ka reference hai, page khud API se live data leta hai."""

import random
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from sales.models import Sale

CLIENTS = [
    "Tech Solutions", "Digital Dynamics", "Bright Future", "Alpha Traders", "NextGen Corp",
    "Skyline Retail", "Horizon Traders", "Falcon Textiles", "Prime Logistics", "Nova Media",
    "Zenith Foods", "Crestline Auto", "Bluewave Exports", "Silverline Realty", "Orbit Fitness",
    "Cedar Hospitality", "Vertex Pharma", "Amber Interiors", "Northstar Finance", "Coastal Apparel",
    "Ironclad Security", "Maple Consulting", "Sunrise Agro", "Quantum Electronics", "Willow Studio",
]
PROJECTS = ["E-commerce Website", "CRM System", "Mobile App", "UI/UX Design", "Inventory Management", "Marketing Site"]
STATUS_WEIGHTS = [("Paid", 0.6), ("Partial", 0.2), ("Pending", 0.2)]


def weighted_status():
    r = random.random()
    upto = 0
    for status, weight in STATUS_WEIGHTS:
        upto += weight
        if r <= upto:
            return status
    return STATUS_WEIGHTS[-1][0]


class Command(BaseCommand):
    help = "Seed the sales table with realistic demo data for SalesPage.jsx."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Delete existing sales before seeding.")
        parser.add_argument("--count", type=int, default=35, help="How many sale records to create.")

    def handle(self, *args, **options):
        if options["reset"]:
            deleted, _ = Sale.objects.all().delete()
            self.stdout.write(self.style.WARNING(f"Deleted {deleted} existing sale(s)."))
        elif Sale.objects.exists():
            self.stdout.write(self.style.WARNING(
                "Sales table already has data — skipping seed (use --reset to wipe and reseed)."
            ))
            return

        today = timezone.localdate()
        rows = []
        for i in range(options["count"]):
            days_ago = random.randint(0, 89)  # spread across ~last 3 months
            rows.append(Sale(
                client=random.choice(CLIENTS),
                project=random.choice(PROJECTS),
                amount=random.randint(15, 220) * 1000,
                date=today - timedelta(days=days_ago),
                status=weighted_status(),
            ))
        Sale.objects.bulk_create(rows)
        self.stdout.write(self.style.SUCCESS(f"Seeded {len(rows)} sale(s)."))
