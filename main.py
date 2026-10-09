
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


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_orders():
    path = Path(__file__).parent / "all-orders.json"
    with path.open(encoding="utf-8-sig") as f:
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


class Question(BaseModel):
    question: str


MONTHS = {
    "january": 1, "february": 2, "march": 3,
    "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9,
    "october": 10, "november": 11, "december": 12,
}


def interpret(question):
    text = question.lower()
    q = {
        "metric": "revenue",
        "region": None,
        "product": None,
        "customer": None,
        "start_date": None,
        "end_date": None,
        "currency": "USD",
    }

    if any(x in text for x in ("refund", "refunded")):
        q["metric"] = "refunds"
    elif "customer" in text and any(
        x in text for x in ("how many", "number of", "count")
    ):
        q["metric"] = "customers"
    elif "product" in text and any(
        x in text for x in ("how many", "number of", "count")
    ):
        q["metric"] = "products"
    elif any(x in text for x in ("how many orders", "order count", "number of orders")):
        q["metric"] = "order_count"
    elif any(x in text for x in ("quantity", "units sold", "how many units")):
        q["metric"] = "quantity"

    for currency in RATES:
        if re.search(rf"\b{currency.lower()}\b", text):
            q["currency"] = currency
            break

    match = re.search(r"\b(?:in|from)\s+([a-z][a-z0-9 -]*?)\s+region\b", text)
    if match:
        q["region"] = match.group(1).strip()

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

        if metric == "revenue" and status != "paid":
            continue
        if metric == "refunds" and status != "refunded":
            continue

        if q["region"] and order["region"].lower() != q["region"]:
            continue

        created = parse_time(order["created_at"]).astimezone(TZ).date().isoformat()
        if q["start_date"] and created < q["start_date"]:
            continue
        if q["end_date"] and created >= q["end_date"]:
            continue

        rows.append(order)

    if metric in ("revenue", "refunds"):
        total = sum(
            (
                Decimal(str(row["amount"]))
                * RATES[row["currency"]]
                / RATES[currency]
                for row in rows
            ),
            Decimal("0"),
        )
        return float(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    if metric == "order_count":
        return len(rows)
    if metric == "quantity":
        return sum(int(row["qty"]) for row in rows)
    if metric == "customers":
        return len({row["customer"] for row in rows})
    if metric == "products":
        return len({row["product"] for row in rows})

    raise ValueError("Unsupported metric")


@app.get("/")
def health():
    return {"status": "running", "unique_orders": len(ORDERS)}


@app.post("/")
def ask(body: Question):
    try:
        return {"answer": answer_question(body.question)}
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
