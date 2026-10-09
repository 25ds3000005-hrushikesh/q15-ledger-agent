
import json
import re
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Acme Ledger Agent")

TZ = ZoneInfo("Asia/Kolkata")

RATES = {
    "USD": Decimal("1"),
    "EUR": Decimal("1.14"),
    "INR": Decimal("0.01103"),
}

MONTHS = {
    "january": 1, "february": 2, "march": 3,
    "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9,
    "october": 10, "november": 11, "december": 12,
}


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_orders():
    path = Path(__file__).parent / "all-orders.json"

    with path.open("r", encoding="utf-8-sig") as f:
        records = json.load(f)

    latest = {}
    for order in records:
        oid = order["id"]
        if (
            oid not in latest
            or parse_time(order["updated_at"])
            > parse_time(latest[oid]["updated_at"])
        ):
            latest[oid] = order

    return list(latest.values())


ORDERS = load_orders()
PRODUCTS = sorted(
    {str(order["product"]) for order in ORDERS},
    key=len,
    reverse=True,
)


class Question(BaseModel):
    question: str


def interpret(question):
    text = question.lower()

    q = {
        "metric": "revenue",
        "region": None,
        "product": None,
        "start_date": None,
        "end_date": None,
        "currency": "USD",
    }

    # Identify the metric.
    if (
        "distinct customer" in text
        or "unique customer" in text
        or "different customers" in text
        or "how many customers" in text
        or "number of customers" in text
    ):
        q["metric"] = "customers"

    elif re.search(r"\b(average|avg|mean)\b", text):
        q["metric"] = "average"

    elif "refund" in text:
        q["metric"] = "refunds"

    elif any(
        phrase in text
        for phrase in (
            "how many orders", "number of orders",
            "order count", "count orders",
        )
    ):
        q["metric"] = "order_count"

    elif any(
        phrase in text
        for phrase in (
            "how many units", "units sold",
            "total quantity", "quantity",
        )
    ):
        q["metric"] = "quantity"

    elif any(
        phrase in text
        for phrase in (
            "how many products", "number of products",
            "unique products", "count products",
        )
    ):
        q["metric"] = "products"

    # Currency.
    for currency in RATES:
        if re.search(rf"\b{currency.lower()}\b", text):
            q["currency"] = currency
            break

    # Product names are taken from the actual dataset.
    for product in PRODUCTS:
        if product.lower() in text:
            q["product"] = product
            break

    # Region: examples "from the North region", "in East region".
    patterns = [
        r"\bfrom\s+(?:the\s+)?([a-z][a-z -]*?)\s+region\b",
        r"\bin\s+(?:the\s+)?([a-z][a-z -]*?)\s+region\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            region = match.group(1).strip()

            if not re.search(
                r"\b(?:usd|eur|inr|from|into|with)\b",
                region,
            ):
                q["region"] = region
                break

    # Calendar month boundaries.
    for month, number in MONTHS.items():
        match = re.search(rf"\b{month}\s+(20\d{{2}})\b", text)

        if match:
            year = int(match.group(1))
            q["start_date"] = f"{year:04d}-{number:02d}-01"

            if number == 12:
                q["end_date"] = f"{year + 1:04d}-01-01"
            else:
                q["end_date"] = f"{year:04d}-{number + 1:02d}-01"

            break

    return q


def money_in_currency(order, currency):
    original = order["currency"].upper()
    return (
        Decimal(str(order["amount"]))
        * RATES[original]
        / RATES[currency]
    )


def answer_question(question):
    q = interpret(question)
    metric = q["metric"]
    currency = q["currency"]
    rows = []

    for order in ORDERS:
        status = order["status"].lower()

        # These metrics use paid orders only.
        if metric in ("revenue", "average") and status != "paid":
            continue

        # Refunds use orders currently marked refunded.
        if metric == "refunds" and status != "refunded":
            continue

        # Customer-count questions explicitly saying paid orders only.
        text = question.lower()
        if (
            metric == "customers"
            and "paid orders only" in text
            and status != "paid"
        ):
            continue

        if (
            q["region"]
            and order["region"].strip().lower() != q["region"].lower()
        ):
            continue

        if (
            q["product"]
            and order["product"].strip().lower() != q["product"].lower()
        ):
            continue

        # Business dates use Asia/Kolkata.
        created = (
            parse_time(order["created_at"])
            .astimezone(TZ)
            .date()
            .isoformat()
        )

        if q["start_date"] and created < q["start_date"]:
            continue

        if q["end_date"] and created >= q["end_date"]:
            continue

        rows.append(order)

    # Distinct customers.
    if metric == "customers":
        return len({order["customer"] for order in rows})

    # Distinct products.
    if metric == "products":
        return len({order["product"] for order in rows})

    # Order count.
    if metric == "order_count":
        return len(rows)

    # Total quantity.
    if metric == "quantity":
        return sum(int(order["qty"]) for order in rows)

    # Average paid order value.
    if metric == "average":
        if not rows:
            return 0

        total = sum(
            (money_in_currency(order, currency) for order in rows),
            Decimal("0"),
        )
        result = total / Decimal(len(rows))

        return float(
            result.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        )

    # Revenue or refund total.
    if metric in ("revenue", "refunds"):
        total = sum(
            (money_in_currency(order, currency) for order in rows),
            Decimal("0"),
        )

        return float(
            total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        )

    raise ValueError("Unsupported metric")


@app.get("/")
def health():
    return {
        "service": "Acme Appliances order ledger",
        "status": "running",
        "unique_orders": len(ORDERS),
    }


@app.post("/")
def ask(body: Question):
    try:
        return {"answer": answer_question(body.question)}
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Could not answer question: {exc}",
        )
