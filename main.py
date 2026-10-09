
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

    # Keep only the latest version of every order ID.
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

    # Identify the requested metric.
    if any(word in text for word in ("refund", "refunded")):
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

    # Identify the currency.
    for currency in RATES:
        if re.search(rf"\b{currency.lower()}\b", text):
            q["currency"] = currency
            break

    # IMPORTANT: Check "from ... region" before "in ... region".
    # This prevents "USD from the North" being treated as a region.
    patterns = [
        r"\bfrom\s+(?:the\s+)?([a-z][a-z -]*?)\s+region\b",
        r"\bin\s+(?:the\s+)?([a-z][a-z -]*?)\s+region\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)

        if match:
            region = match.group(1).strip()

            # Reject captures that accidentally include currency or
            # connecting words from earlier in the question.
            if not re.search(
                r"\b(?:usd|eur|inr|from|into|with)\b",
                region,
            ):
                q["region"] = region
                break

    # Identify calendar-month date ranges.
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


def answer_question(question):
    q = interpret(question)
    metric = q["metric"]
    currency = q["currency"]

    rows = []

    for order in ORDERS:
        status = order["status"].lower()

        # Revenue includes paid orders only.
        if metric == "revenue" and status != "paid":
            continue

        # Refunds include orders currently marked refunded.
        if metric == "refunds" and status != "refunded":
            continue

        # Filter by region when specified.
        if q["region"]:
            if order["region"].strip().lower() != q["region"].strip().lower():
                continue

        # Business dates are based on Asia/Kolkata.
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

    # Calculate money in the requested currency.
    if metric in ("revenue", "refunds"):
        total = sum(
            (
                Decimal(str(order["amount"]))
                * RATES[order["currency"].upper()]
                / RATES[currency]
                for order in rows
            ),
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
        answer = answer_question(body.question)
        return {"answer": answer}

    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Could not answer question: {exc}",
        )
