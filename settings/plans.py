"""The subscription plans the Billing tab offers. Kept on the server so a
plan's price and storage allowance can't be edited from the browser — the
frontend just renders whatever GET /api/settings/billing/ returns."""
import calendar
from datetime import date

PLAN_CATALOG = [
    {
        "name": "Starter",
        "price": 9999,
        "storage_limit_gb": 10,
        "features": ["10 Projects", "20 Users", "Basic AI Assistant", "Email Support", "10 GB Storage"],
    },
    {
        "name": "Business",
        "price": 24999,
        "storage_limit_gb": 50,
        "features": ["Unlimited Projects", "Unlimited Users", "AI Assistant (Advanced)", "Priority Support", "50 GB Storage"],
    },
    {
        "name": "Enterprise",
        "price": None,  # "Custom" — negotiated, not self-serve priced
        "storage_limit_gb": 200,
        "features": ["Everything in Business", "Dedicated Manager", "Custom Integrations", "SLA & Onboarding", "200 GB Storage"],
    },
]
_BY_NAME = {p["name"].lower(): p for p in PLAN_CATALOG}

CARD_BRANDS = ["Visa", "Mastercard", "American Express", "Discover", "UnionPay", "JCB", "Diners Club", "RuPay", "Card"]


def get_plan(name):
    return _BY_NAME.get((name or "").strip().lower())


def add_month(d: date) -> date:
    """Same day next month (clamped: Jan 31 -> Feb 28/29)."""
    year = d.year + (d.month // 12)
    month = d.month % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))
