"""Deterministic fictional dataset for the platform.

Company: "Lumen Kits" (fictional) sells starter kits online.  Three daily
snapshots flow through raw -> staging -> dim/fct -> mart -> report.  Defects
are seeded so every quality dimension has something to find, and so the same
underlying error has *different* downstream consequences depending on what a
consumer actually reads.

No network access.  ``seed`` is idempotent on a fresh database.
"""

from __future__ import annotations

import json
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .util import canonical

SNAPSHOT_DATES = ["2026-11-16", "2026-11-17", "2026-11-18"]
SNAPSHOT_DATE = SNAPSHOT_DATES[-1]
LATE_SNAPSHOT_DATE = SNAPSHOT_DATES[0]
T0 = datetime(2026, 11, 18, 6, 0, tzinfo=timezone.utc)
SEED = 20261118


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _ts(rng: random.Random, base: datetime, spread_min: int) -> str:
    return _iso(base + timedelta(minutes=rng.randint(0, spread_min)))


# ------------------------------------------------------------------ topology
SOURCES = [
    ("src_crm", "crm_export", "file", 120, "customer-ops", "Nightly CRM account export (SFTP)"),
    ("src_shop", "shop_prime", "oltp", 60, "storefront", "Primary commerce OLTP binlog"),
    ("src_pay", "payments_feed", "stream", 30, "payments", "Authorisation/capture event stream"),
    ("src_pim", "pim_upload", "file", 1440, "catalogue", "Catalogue spreadsheet upload"),
    ("src_wms", "wms_api", "api", 240, "warehouse", "Warehouse management API poll"),
]

DATASETS = [
    # id, layer, source, nominal_rows, description
    ("raw_customer", "raw", "src_crm", 261, "Raw CRM customer export, one row per export record"),
    ("stg_customer", "staging", None, 261, "Email normalised, types cast"),
    ("dim_customer", "dim", None, 261, "Customer dimension, current-flag"),
    ("raw_order", "raw", "src_shop", 320, "Raw order headers"),
    ("stg_order", "staging", None, 320, "Typed order headers"),
    ("fct_order", "fct", None, 320, "Order fact"),
    ("raw_order_line", "raw", "src_shop", 965, "Raw order lines"),
    ("fct_order_line", "fct", None, 965, "Line fact with product category"),
    ("raw_product", "raw", "src_pim", 73, "Raw catalogue"),
    ("stg_product", "staging", None, 73, "Typed catalogue"),
    ("raw_payment", "raw", "src_pay", 300, "Raw captures"),
    ("stg_payment", "staging", None, 300, "Typed captures"),
    ("fct_payment", "fct", None, 300, "Capture fact joined to order currency"),
    ("raw_shipment", "raw", "src_wms", 240, "Raw shipments"),
    ("stg_shipment", "staging", None, 240, "Typed shipments"),
    ("fct_shipment", "fct", None, 240, "Shipment fact"),
    ("mart_revenue_daily", "mart", None, 24, "Revenue by order_date x category"),
    ("mart_fulfilment", "mart", None, 18, "Fulfilment by carrier x day"),
    ("mart_customer_health", "mart", None, 261, "Per-customer value + recency"),
]

REPORTS = [
    # id, name, owner, criticality, cadence, base_dataset, description
    ("rpt_exec_daily", "Exec daily revenue", "finance", "tier1", "daily", "mart_revenue_daily", "Headline revenue for the exec channel"),
    ("rpt_finance_close", "Finance close pack", "finance", "tier1", "daily", "fct_payment", "Cash + revenue close, tied to the ledger"),
    ("rpt_ops_board", "Ops wallboard", "operations", "tier2", "hourly", "fct_shipment", "Carrier SLA and backlog wallboard"),
    ("rpt_csm_dashboard", "CSM dashboard", "customer-ops", "tier2", "daily", "mart_customer_health", "Book of business per CSM"),
    ("rpt_product_mix", "Product mix ad-hoc", "catalogue", "tier3", "weekly", "fct_order_line", "Exploratory category mix analysis"),
    ("rpt_intake_sla", "Order intake SLA", "operations", "tier2", "hourly", "stg_order", "Intake rate and status mix; never shows customer identity"),
    ("rpt_treasury", "Treasury cash forecast", "finance", "tier1", "daily", "stg_payment", "Nightly cash forecast; consumer is a spreadsheet import"),
    ("rpt_customer_hygiene", "Customer hygiene report", "customer-ops", "tier3", "weekly", "stg_customer", "Country/name completeness only; email is deliberately out of scope"),
]

# parent, child, child_type, kind, propagation, impact_weight, fields, note
RELATIONS = [
    ("raw_customer", "stg_customer", "dataset", "sql", "row", 1.0, None, "email normalisation + typing"),
    ("stg_customer", "dim_customer", "dataset", "sql", "row", 1.0, None, "current-flag"),
    ("dim_customer", "mart_customer_health", "dataset", "sql", "row", 1.0, None, "customer health grain = customer"),
    ("raw_order", "stg_order", "dataset", "view", "row", 1.0, None, "typing"),
    ("stg_order", "fct_order", "dataset", "sql", "row", 1.0, None, "order fact"),
    ("raw_order", "fct_order_line", "dataset", "sql", "row", 0.9,
     ["order_id", "status", "customer_id", "order_date", "total"], "order attributes joined onto lines"),
    ("raw_order_line", "fct_order_line", "dataset", "sql", "row", 1.0, None, "line grain preserved"),
    ("stg_product", "fct_order_line", "dataset", "sql", "row", 0.7, ["sku", "category", "unit_price"],
     "category + price attached to line"),
    ("raw_product", "stg_product", "dataset", "sql", "row", 1.0, None, "typing"),
    ("raw_payment", "stg_payment", "dataset", "stream", "row", 1.0, None, "typing"),
    ("stg_payment", "fct_payment", "dataset", "sql", "row", 1.0, None, "capture fact"),
    ("fct_order", "fct_payment", "dataset", "sql", "row", 1.0, ["order_id", "currency"],
     "capture inherits order currency"),
    ("raw_shipment", "stg_shipment", "dataset", "sql", "row", 1.0, None, "typing"),
    ("stg_shipment", "fct_shipment", "dataset", "sql", "row", 1.0, None, "carrier normalisation"),
    ("fct_order_line", "mart_revenue_daily", "dataset", "sql", "aggregate", 1.0,
     ["order_date", "category", "line_total", "quantity"], "sum(line_total) by order_date x category"),
    ("fct_payment", "mart_revenue_daily", "dataset", "sql", "aggregate", 0.6,
     ["captured_at", "amount", "currency"], "captured cash by day"),
    ("fct_shipment", "mart_fulfilment", "dataset", "sql", "aggregate", 1.0,
     ["carrier", "shipped_at", "delivered_at"], "count/sum by carrier"),
    ("fct_order_line", "mart_fulfilment", "dataset", "sql", "aggregate", 0.3,
     ["order_id", "quantity"], "late-line counter"),
    ("mart_revenue_daily", "rpt_exec_daily", "report", "export", "row", 1.0, None, "exec headline"),
    ("mart_revenue_daily", "rpt_finance_close", "report", "export", "row", 1.0, None, "finance close pack"),
    ("fct_payment", "rpt_finance_close", "report", "export", "row", 1.0, None, "cash reconciliation"),
    ("fct_order", "rpt_finance_close", "report", "export", "row", 1.0, ["order_id", "status", "total"],
     "close reports revenue from the order header, not the line rollup"),
    ("mart_fulfilment", "rpt_ops_board", "report", "export", "row", 1.0, None, "ops wallboard"),
    ("mart_customer_health", "rpt_csm_dashboard", "report", "export", "row", 1.0, None, "CSM book of business"),
    ("fct_order_line", "rpt_product_mix", "report", "export", "row", 0.6, ["sku", "category", "quantity"],
     "ad-hoc mix analysis"),
    # This report reads the same staging table as finance, but not the fields
    # that are broken -> it is touched by the lineage, not affected.
    ("stg_order", "rpt_intake_sla", "report", "export", "row", 1.0, ["order_date", "status"],
     "intake monitor ignores customer identity"),
    ("stg_payment", "rpt_treasury", "report", "export", "row", 1.0, None, "cash forecast export"),
    ("stg_customer", "rpt_customer_hygiene", "report", "export", "row", 0.5, ["country", "name"],
     "hygiene report excludes email and segment"),
]

# dataset, direction, reason, severity, note  -- explicit unknown lineage
LINEAGE_GAPS = [
    ("raw_customer", "downstream", "external_consumer", "high",
     "Marketing pulls the CRM export by FTP; nothing about that consumer is registered"),
    ("mart_customer_health", "downstream", "stale_metadata", "medium",
     "rpt_email_digest is built from this mart; registration stale since 2026-06"),
    ("fct_shipment", "downstream", "manual_upload", "low",
     "Warehouse ops pastes a daily carrier summary into a spreadsheet"),
    ("stg_product", "upstream", "stale_metadata", "low",
     "Unit price is overwritten by an unpriced promo job not declared upstream"),
    ("stg_payment", "downstream", "external_consumer", "high",
     "Treasury imports a nightly CSV straight from staging; no metadata registered"),
]

SEGMENTS = ["smb", "mid", "enterprise"]
COUNTRIES = ["DE", "FR", "NL", "ES", "IT", "PL"]
CARRIERS = ["DHL", "DPD", "UPS", "GLS"]
CATEGORIES = ["electronics", "home", "outdoor", "kitchen", "stationery"]
STATUSES = ["pending", "paid", "shipped", "cancelled"]


# ------------------------------------------------------------------ generation
def generate(seed: int = SEED) -> dict[str, Any]:
    rng = random.Random(seed)
    customers: list[dict] = []
    orders: list[dict] = []
    lines: list[dict] = []
    products: list[dict] = []
    payments: list[dict] = []
    shipments: list[dict] = []
    arrivals: list[dict] = []

    for i in range(260):
        cid = f"C{i:04d}"
        seg = SEGMENTS[i % len(SEGMENTS)]
        row = {
            "_record_id": f"crm:{i:04d}",
            "customer_id": cid,
            "name": f"Lumen Customer {i}",
            "email": f"user{i}@example-{seg}.test",
            "segment": seg,
            "country": COUNTRIES[i % len(COUNTRIES)],
            "updated_at": _ts(rng, T0 - timedelta(hours=rng.randint(1, 60)), 50),
        }
        if i in (7, 19, 44, 101, 155, 201):
            row["email"] = None
        if i in (12, 63, 144):
            row["email"] = f"broken-at-example{i}.test"
        if i in (33, 88):
            row["segment"] = "reseller"
        if i in (5, 150):
            row["updated_at"] = _iso(T0 - timedelta(hours=200))
        customers.append(row)
    # duplicate CRM record carrying the same customer_id (double upload)
    customers.append({
        "_record_id": "crm:0260",
        "customer_id": "C0061",
        "name": "Lumen Customer 61 (re-upload)",
        "email": "user61@example-smb.test",
        "segment": "smb",
        "country": "DE",
        "updated_at": _iso(T0 - timedelta(hours=3)),
    })

    for i in range(72):
        price = round(rng.uniform(4.5, 480.0), 2)
        row = {
            "_record_id": f"pim:{i:03d}",
            "sku": f"SKU-{i:03d}",
            "title": f"Lumen Kit {i}",
            "category": CATEGORIES[i % len(CATEGORIES)],
            "unit_price": price,
            "active": i % 11 != 0,
            "updated_at": _ts(rng, T0 - timedelta(hours=5), 90),
        }
        if i in (13, 57):
            row["unit_price"] = -price
        if i in (26, 27):
            row["category"] = "misc"
        products.append(row)
    products.append({
        "_record_id": "pim:072",
        "sku": "SKU-040",
        "title": "Lumen Kit 40 (dup SKU)",
        "category": "home",
        "unit_price": 12.0,
        "active": True,
        "updated_at": _iso(T0 - timedelta(hours=4)),
    })

    # line prices come from the catalogue so the downstream re-derivation matches
    price_by_sku: dict[str, float] = {}
    for p in products:
        price_by_sku.setdefault(p["sku"], p["unit_price"])

    total_by_order: dict[str, float] = {}
    for i in range(320):
        oid = f"O{10000 + i}"
        day_off = i % 3
        placed = T0 - timedelta(days=day_off, hours=rng.randint(0, 8), minutes=rng.randint(0, 59))
        row = {
            "_record_id": oid,
            "order_id": oid,
            "customer_id": f"C{(i * 7) % 260:04d}",
            "order_ts": _iso(placed),
            "order_date": placed.date().isoformat(),
            "status": STATUSES[i % len(STATUSES)],
            "currency": "EUR",
            "total": 0.0,
        }
        if i in (17, 88, 201, 268):
            row["status"] = "refunded"
        if i in (31, 132):
            row["customer_id"] = None
        if i in (59, 260):
            row["customer_id"] = "C9999"
        if i == 73:
            row["currency"] = "USD"
        orders.append(row)

        n_lines = rng.randint(2, 4)
        for ln in range(n_lines):
            sku = f"SKU-{(i * 3 + ln * 11) % 72:03d}"
            if ln == 1 and i % 53 == 0:
                sku = "SKU-GHOST"
            qty = rng.randint(1, 4)
            unit = price_by_sku.get(sku, round(rng.uniform(4.5, 200.0), 2))
            line = {
                "_record_id": f"L{oid}-{ln + 1}",
                "line_id": f"L{oid}-{ln + 1}",
                "order_id": oid,
                "sku": sku,
                "quantity": qty,
                "unit_price": unit,
                "line_total": round(unit * qty, 2),
            }
            total_by_order[oid] = round(total_by_order.get(oid, 0.0) + line["line_total"], 2)
            lines.append(line)
    for o in orders:
        o["total"] = total_by_order.get(o["order_id"], 0.0)
    # break a few header totals (consistency defect)
    breakage = {"O10017": 3.5, "O10132": -2.25, "O10268": 19.99, "O10301": -7.5}
    for o in orders:
        if o["order_id"] in breakage:
            o["total"] = round(o["total"] + breakage[o["order_id"]], 2)

    for i in range(300):
        oid = f"O{10000 + (i % 320)}"
        captured = T0 - timedelta(days=i % 3, hours=rng.randint(0, 6))
        row = {
            "_record_id": f"P{20000 + i}",
            "payment_id": f"P{20000 + i}",
            "order_id": oid,
            "amount": round(rng.uniform(10, 900), 2),
            "currency": "EUR" if i % 41 else "USD",
            "method": ["card", "paypal", "invoice"][i % 3],
            "captured_at": _iso(captured),
        }
        if i in (11, 112, 211):
            row["amount"] = -abs(row["amount"])
        payments.append(row)

    for i in range(240):
        oid = f"O{10000 + (i % 320)}"
        ship = T0 - timedelta(days=i % 3, hours=rng.randint(0, 10))
        row = {
            "_record_id": f"S{30000 + i}",
            "shipment_id": f"S{30000 + i}",
            "order_id": oid,
            "carrier": CARRIERS[i % len(CARRIERS)],
            "shipped_at": _iso(ship),
            "delivered_at": _iso(ship + timedelta(hours=rng.randint(12, 96))) if i % 9 else None,
            "weight_kg": round(rng.uniform(0.2, 12.0), 2),
        }
        if i in (6, 60):
            row["carrier"] = "DHL Express"  # not normalised -> enum miss
        if i in (14, 141):
            row["weight_kg"] = -1.0
        shipments.append(row)

    for date in SNAPSHOT_DATES:
        expected = datetime.fromisoformat(date + "T06:00:00+00:00")
        for src, sla in (("src_crm", 120), ("src_shop", 60), ("src_pay", 30), ("src_pim", 1440), ("src_wms", 240)):
            if src == "src_pay" and date == LATE_SNAPSHOT_DATE:
                arrived = expected + timedelta(hours=9, minutes=12)
            elif src == "src_shop" and date == SNAPSHOT_DATE:
                arrived = expected + timedelta(minutes=90)
            elif src == "src_wms" and date == SNAPSHOT_DATE:
                arrived = expected + timedelta(minutes=310)
            else:
                arrived = expected + timedelta(minutes=rng.randint(-5, max(1, sla // 3)))
            arrivals.append({
                "source_id": src,
                "snapshot_date": date,
                "expected_at": _iso(expected),
                "arrived_at": _iso(arrived),
                "late": int((arrived - expected).total_seconds() / 60 > sla),
            })

    return {"customers": customers, "orders": orders, "lines": lines, "products": products,
            "payments": payments, "shipments": shipments, "arrivals": arrivals}


# ------------------------------------------------------------------ seeding
def _partition_costs() -> list[tuple[str, str, int, float, float]]:
    """(dataset, snapshot_date, rows, scan_cost_usd, rows_per_second)"""
    rng = random.Random(SEED + 1)
    out = []
    for ds, _layer, _src, rows0, _desc in DATASETS:
        for date in SNAPSHOT_DATES:
            rows = max(1, rows0 + rng.randint(-3, 3))
            per_row = 0.000035 if ds.startswith("raw_") else 0.000012
            out.append((ds, date, rows, round(rows * per_row + 0.02, 5), 50000))
    return out


def seed(conn) -> dict[str, int]:
    from . import db

    if conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]:
        return {"sources": 0, "datasets": 0, "seeded": 0}

    for sid, name, kind, sla, owner, desc in SOURCES:
        conn.execute("INSERT INTO sources(id,name,kind,freshness_sla_min,owner_team,description) VALUES(?,?,?,?,?,?)",
                     (sid, name, kind, sla, owner, desc))
    for did, layer, src, rows, desc in DATASETS:
        conn.execute("INSERT INTO datasets(id,name,layer,source_id,row_count,partition_key,description) VALUES(?,?,?,?,?,?,?)",
                     (did, did, layer, src, rows, "snapshot_date", desc))
    for rid, name, owner, crit, cadence, base, desc in REPORTS:
        conn.execute("INSERT INTO reports(id,name,owner_team,criticality,cadence,base_dataset,description) VALUES(?,?,?,?,?,?,?)",
                     (rid, name, owner, crit, cadence, base, desc))
    for parent, child, ctype, kind, prop, weight, fields, note in RELATIONS:
        conn.execute(
            "INSERT INTO relations(parent_id,child_id,child_type,kind,propagation,impact_weight,fields,note) VALUES(?,?,?,?,?,?,?,?)",
            (parent, child, ctype, kind, prop, weight, json.dumps(fields) if fields else None, note),
        )
    for ds, direction, reason, sev, note in LINEAGE_GAPS:
        gid = "gap_" + uuid.uuid5(uuid.NAMESPACE_URL, f"{ds}/{direction}/{reason}").hex[:12]
        conn.execute("INSERT INTO lineage_gaps(id,dataset_id,direction,reason,severity,note) VALUES(?,?,?,?,?,?)",
                     (gid, ds, direction, reason, sev, note))
    for ds, snap, rows, cost, rps in _partition_costs():
        conn.execute(
            "INSERT INTO partition_costs(dataset_id,partition_key,partition_value,rows,bytes,scan_cost_usd,rows_per_second) VALUES(?,?,?,?,?,?,?)",
            (ds, "snapshot_date", snap, rows, rows * 940, cost, rps),
        )

    data = generate()
    ing = _iso(T0)
    records: list[tuple[str, str, str, dict]] = []
    for row in data["customers"]:
        records.append(("src_crm", row["_record_id"], "row", row))
    for row in data["products"]:
        records.append(("src_pim", row["_record_id"], "row", row))
    for row in data["orders"]:
        records.append(("src_shop", row["_record_id"], "order", row))
    for row in data["lines"]:
        records.append(("src_shop", row["_record_id"], "line", row))
    for row in data["payments"]:
        records.append(("src_pay", row["_record_id"], "row", row))
    for row in data["shipments"]:
        records.append(("src_wms", row["_record_id"], "row", row))

    for date in SNAPSHOT_DATES:
        for src, rid, kind, row in records:
            payload = dict(row)
            payload["_snapshot_date"] = date
            conn.execute(
                "INSERT OR REPLACE INTO source_records(source_id,record_id,kind,payload,snapshot_date,ingested_at) VALUES(?,?,?,?,?,?)",
                (src, rid, kind, canonical(payload), date, ing),
            )
    for arr in data["arrivals"]:
        counts = {"src_crm": len(data["customers"]), "src_shop": len(data["orders"]) + len(data["lines"]),
                  "src_pay": len(data["payments"]), "src_pim": len(data["products"]), "src_wms": len(data["shipments"])}
        conn.execute(
            "INSERT INTO ingest_snapshots(id,source_id,snapshot_date,expected_at,arrived_at,record_count,late) VALUES(?,?,?,?,?,?,?)",
            (f"arr_{arr['source_id']}_{arr['snapshot_date']}", arr["source_id"], arr["snapshot_date"],
             arr["expected_at"], arr["arrived_at"], counts[arr["source_id"]], arr["late"]),
        )
    return {"sources": len(SOURCES), "datasets": len(DATASETS), "records": len(records) * len(SNAPSHOT_DATES)}


if __name__ == "__main__":  # pragma: no cover
    d = generate()
    print({k: len(v) for k, v in d.items()})
