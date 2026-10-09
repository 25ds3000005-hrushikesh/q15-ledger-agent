
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
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_orders():
    path = Path(__file__).parent / "all-orders.json"

    with path.open("r", encoding="utf-8-sig") as file:
        records = json.load(file)

    # Keep the latest updated record for each order ID.
    latest = {}

    for order in records:
        order_id = order["id"]

        if (
            order_id not in latest
            or parse_time(order["updated_at"])
            > parse_time(latest[order_id]["updated_at"])
        ):
            latest[order_id] = order

    return list(latest.values())


ORDERS = load_orders()


class Question(BaseModel):
    question: str


def interpret(question):
    text = question.lower()

    q = {
        "metric": "revenue",
        "region": None,
        "start_date": None,
        "end_date": None,
        "currency": "USD",
    }

    # Identify the metric. Check average before total revenue.
    if re.search(r"\b(average|avg|mean)\b", text):
        q["metric"] = "average"

    elif any(word in text for word in ("refund", "refunded")):
        q["metric"] = "refunds"

    elif any(
        phrase in text
        for phrase in (
            "how many orders",
            "number of orders",
            "order count",
            "count orders",
        )
    ):
        q["metric"] = "order_count"

    elif any(
        phrase in text
        for phrase in (
            "how many units",
            "units sold",
            "total quantity",
            "quantity",
        )
    ):
        q["metric"] = "quantity"

    elif any(
        phrase in text
        for phrase in (
            "number of customers",
            "how many customers",
            "count customers",
            "unique customers",
        )
    ):
        q["metric"] = "customers"

    elif any(
        phrase in text
        for phrase in (
            "number of products",
            "how many products",
            "count products",
            "unique products",
        )
    ):
        q["metric"] = "products"

    # Identify requested currency.
    for currency in RATES:
        if re.search(rf"\b{currency.lower()}\b", text):
            q["currency"] = currency
            break

    # Identify region without accidentally capturing the currency.
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

    # Identify calendar month and its exclusive end date.
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
    original_currency = order["currency"].upper()

    return (
        Decimal(str(order["amount"]))
        * RATES[original_currency]
        / RATES[currency]
    )


def answer_question(question):
    q = interpret(question)
    metric = q["metric"]
    currency = q["currency"]

    rows = []

    for order in ORDERS:
        status = order["status"].lower()

        # Revenue and average paid order value use paid orders only.
        if metric in ("revenue", "average") and status != "paid":
            continue

        # Refund total uses currently refunded orders.
        if metric == "refunds" and status != "refunded":
            continue

        # Filter by region.
        if q["region"]:
            if order["region"].strip().lower() != q["region"]:
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

    # Average value per paid order, not the total.
    if metric == "average":
        if not rows:
            return 0

        total = sum(
            (money_in_currency(order, currency) for order in rows),
            Decimal("0"),
        )

        average = total / Decimal(len(rows))

        return float(
            average.quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP,
            )
        )

    # Revenue and refund totals.
    if metric in ("revenue", "refunds"):
        total = sum(
            (money_in_currency(order, currency) for order in rows),
            Decimal("0"),
        )

        return float(
            total.quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP,
            )
        )

    if metric == "order_count":
        return len(rows)

    if metric == "quantity":
        return sum(int(order["qty"]) for order in rows)

    if metric == "customers":
        return len({order["customer"] for order in rows})

    if metric == "products":
        return len({order["product"] for order in rows})

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
