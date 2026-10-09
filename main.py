
import json
import re
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Acme Ledger Agent")

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
MODEL = "gemma3:1b-it-qat"

RATES = {
    "USD": Decimal("1"),
    "EUR": Decimal("1.14"),
    "INR": Decimal("0.01103"),
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


class Question(BaseModel):
    question: str


def interpret(question):
    prompt = f"""
Convert the user's ledger question into JSON only.
Allowed fields:
metric: revenue, refunds, order_count, quantity, customers, products
region: string or null
product: string or null
customer: string or null
status: string or null
start_date: YYYY-MM-DD or null
end_date: YYYY-MM-DD or null (exclusive)
currency: USD, EUR, INR or null
group_by: region, product, customer, month or null

Rules:
- "revenue" means the sum of amounts for paid orders only.
- "refunds" means the sum of amounts for orders whose current status is refunded.
- Use created_at for date filters.
- Interpret month names as calendar months.
- Do not invent filters. Use null when absent.
- Return valid JSON, without Markdown fences or explanations.

Question: {question}
"""
    response = requests.post(
        OLLAMA_URL,
        json={"model": MODEL, "prompt": prompt, "stream": False,
              "format": "json"},
        timeout=12,
    )
    response.raise_for_status()
    text = response.json()["response"]
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("Model did not return JSON")
    return json.loads(match.group())


def answer_question(question):
    q = interpret(question)
    metric = q.get("metric") or "revenue"
    currency = (q.get("currency") or "USD").upper()

    if currency not in RATES:
        currency = "USD"

    rows = []
    for order in ORDERS:
        if metric == "revenue" and order["status"] != "paid":
            continue
        if metric == "refunds" and order["status"] != "refunded":
            continue

        if q.get("region") and order["region"].lower() != q["region"].lower():
            continue
        if q.get("product") and order["product"].lower() != q["product"].lower():
            continue
        if q.get("customer") and order["customer"].lower() != q["customer"].lower():
            continue
        if q.get("status") and order["status"].lower() != q["status"].lower():
            continue

        # Business dates are interpreted in Asia/Kolkata.
        created = parse_time(order["created_at"]).astimezone().date().isoformat()
        if q.get("start_date") and created < q["start_date"]:
            continue
        if q.get("end_date") and created >= q["end_date"]:
            continue

        rows.append(order)

    if metric in ("revenue", "refunds"):
        total = sum(
            (
                Decimal(str(row["amount"]))
                * RATES.get(row["currency"], Decimal("1"))
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

    raise ValueError(f"Unsupported metric: {metric}")


@app.get("/")
def health():
    return {"status": "running", "unique_orders": len(ORDERS)}


@app.post("/")
def ask(body: Question):
    try:
        answer = answer_question(body.question)
        return {"answer": answer}
    except requests.RequestException as exc:
        raise HTTPException(status_code=503, detail=f"Ollama error: {exc}")
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail=f"Could not answer question: {exc}")
