"""Deterministic transformation layer (raw -> staging -> dim/fct -> mart).

Every produced row records:
  ``origin``     direct parent rows as [{dataset, record_id}] — the impact
                 tracer walks these, so it never has to guess key names.
  ``root_fact``  the original source record, so a score can be expanded to the
                 exact fact that caused it.
  ``snapshot_date`` the partition the row belongs to.

A dataset is built from its declared inputs only; nothing is hidden.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Iterable

Row = dict[str, Any]

# dataset -> direct parents used by the transform
INPUTS: dict[str, list[str]] = {
    "raw_customer": [],
    "stg_customer": ["raw_customer"],
    "dim_customer": ["stg_customer"],
    "mart_customer_health": ["dim_customer"],
    "raw_order": [],
    "stg_order": ["raw_order"],
    "fct_order": ["stg_order"],
    "raw_order_line": [],
    "fct_order_line": ["raw_order", "raw_order_line", "stg_product"],
    "raw_product": [],
    "stg_product": ["raw_product"],
    "raw_payment": [],
    "stg_payment": ["raw_payment"],
    "fct_payment": ["stg_payment", "fct_order"],
    "raw_shipment": [],
    "stg_shipment": ["raw_shipment"],
    "fct_shipment": ["stg_shipment"],
    "mart_revenue_daily": ["fct_order_line"],
    "mart_fulfilment": ["fct_order_line", "fct_shipment"],
}

# datasets whose transform also reads a side input that must be loaded
SIDE_INPUTS: dict[str, list[str]] = {
    "mart_customer_health": ["fct_order"],
}


# ------------------------------------------------------------------ helpers
def row_key_of(dataset: str, source_record_id: str) -> str:
    return source_record_id


def _make(dataset: str, key: str, payload: dict, parents: Iterable[Row | tuple[str, str]],
          root: dict | None, date: str) -> Row:
    origin: list[dict[str, str]] = []
    for p in parents:
        if isinstance(p, tuple):
            origin.append({"dataset": p[0], "record_id": p[1]})
        else:
            origin.append({"dataset": p["_dataset"], "record_id": p["row_key"]})
    if root is None:
        root = {"source_id": "derived", "record_id": f"{dataset}:{key}"}
    return {
        "_dataset": dataset,
        "row_key": key,
        "payload": payload,
        "origin": origin,
        "root_fact": root,
        "snapshot_date": date,
    }


def _tag(dataset: str, rows: list[Row]) -> list[Row]:
    for r in rows:
        r["_dataset"] = dataset
    return rows


def _source_rows(conn, source_id: str, date: str, kinds: tuple[str, ...]) -> list[Row]:
    q = "SELECT record_id, payload FROM source_records WHERE source_id=? AND snapshot_date=?"
    args: list[Any] = [source_id, date]
    if kinds:
        q += f" AND kind IN ({','.join('?' * len(kinds))})"
        args.extend(kinds)
    out = []
    for r in conn.execute(q + " ORDER BY record_id", args):
        out.append(_make(
            "", r["record_id"], json.loads(r["payload"]), (),
            {"source_id": source_id, "record_id": r["record_id"]}, date,
        ))
    return out


def _parent_roots(rows: list[Row]) -> dict | None:
    return rows[0]["root_fact"] if rows else None


def _payloads(rows: list[Row]) -> list[dict]:
    return [r["payload"] for r in rows]


# ------------------------------------------------------------------ builders
def build_raw_customer(conn, date, parents):
    return _tag("raw_customer", _source_rows(conn, "src_crm", date, ("row",)))


def build_stg_customer(conn, date, parents):
    out = []
    for r in parents["raw_customer"]:
        p = r["payload"]
        email = p.get("email")
        row = {
            "customer_id": p.get("customer_id"),
            "name": (p.get("name") or "").strip(),
            "email": email.strip().lower() if isinstance(email, str) and email.strip() else None,
            "segment": p.get("segment"),
            "country": p.get("country"),
            "updated_at": p.get("updated_at"),
        }
        out.append(_make("stg_customer", r["row_key"], row, [r], r["root_fact"], date))
    return out


def build_dim_customer(conn, date, parents):
    latest: dict[str, Row] = {}
    for r in parents["stg_customer"]:
        cid = r["payload"].get("customer_id")
        if not cid:
            continue
        prev = latest.get(cid)
        if prev is None or (r["payload"].get("updated_at") or "") >= (prev["payload"].get("updated_at") or ""):
            latest[cid] = r
    out = []
    for cid, r in sorted(latest.items()):
        payload = {**r["payload"], "is_current": True, "source_record": r["root_fact"]["record_id"]}
        out.append(_make("dim_customer", cid, payload, [r], r["root_fact"], date))
    return out


def build_mart_customer_health(conn, date, parents):
    orders_by_customer: dict[str, list[dict]] = defaultdict(list)
    for r in parents.get("_fct_order", []):
        p = r["payload"]
        if p.get("customer_id"):
            orders_by_customer[p["customer_id"]].append(p)
    out = []
    for r in parents["dim_customer"]:
        p = r["payload"]
        cid = p.get("customer_id")
        orders = orders_by_customer.get(cid, [])
        row = {
            "customer_id": cid,
            "segment": p.get("segment"),
            "country": p.get("country"),
            "email_present": p.get("email") is not None,
            "lifetime_orders": len(orders),
            "lifetime_revenue": round(sum(o.get("total") or 0.0 for o in orders), 2),
            "last_order_date": max([o.get("order_date") or "" for o in orders], default=None),
        }
        out.append(_make("mart_customer_health", cid, row, [r], r["root_fact"], date))
    return out


def build_raw_order(conn, date, parents):
    return _tag("raw_order", _source_rows(conn, "src_shop", date, ("order",)))


def build_stg_order(conn, date, parents):
    out = []
    for r in parents["raw_order"]:
        p = r["payload"]
        row = {k: p.get(k) for k in ("order_id", "customer_id", "order_ts", "order_date", "status", "currency")}
        row["total"] = float(p.get("total") or 0.0)
        row["customer_id"] = row["customer_id"] or None
        out.append(_make("stg_order", r["row_key"], row, [r], r["root_fact"], date))
    return out


def build_fct_order(conn, date, parents):
    out = []
    for r in parents["stg_order"]:
        out.append(_make("fct_order", r["row_key"], dict(r["payload"]), [r], r["root_fact"], date))
    return out


def build_raw_order_line(conn, date, parents):
    return _tag("raw_order_line", _source_rows(conn, "src_shop", date, ("line",)))


def build_raw_product(conn, date, parents):
    return _tag("raw_product", _source_rows(conn, "src_pim", date, ("row",)))


def build_stg_product(conn, date, parents):
    out = []
    for r in parents["raw_product"]:
        p = r["payload"]
        cat = p.get("category")
        row = {
            "sku": p.get("sku"),
            "title": p.get("title"),
            "category": cat.strip().lower() if isinstance(cat, str) else cat,
            "unit_price": float(p.get("unit_price") or 0.0),
            "active": bool(p.get("active")),
            "updated_at": p.get("updated_at"),
        }
        out.append(_make("stg_product", r["row_key"], row, [r], r["root_fact"], date))
    return out


def build_fct_order_line(conn, date, parents):
    by_sku: dict[str, list[Row]] = defaultdict(list)
    for r in parents.get("stg_product", []):
        by_sku[r["payload"].get("sku")].append(r)
    orders = {r["row_key"]: r for r in parents["raw_order"]}
    out = []
    for r in parents["raw_order_line"]:
        p = r["payload"]
        header = orders.get(p.get("order_id"))
        products = by_sku.get(p.get("sku"), [])
        prod = products[0] if products else None
        qty = int(p.get("quantity") or 0)
        unit = float(prod["payload"]["unit_price"]) if prod else float(p.get("unit_price") or 0.0)
        row = {
            "line_id": p.get("line_id"),
            "order_id": p.get("order_id"),
            "sku": p.get("sku"),
            "quantity": qty,
            "unit_price": unit,
            "line_total": round(unit * qty, 2),
            "category": prod["payload"].get("category") if prod else None,
            "order_date": header["payload"].get("order_date") if header else None,
            "order_status": header["payload"].get("status") if header else None,
        }
        ps: list[Row] = [r]
        if header is not None:
            ps.append(header)
        if prod is not None:
            ps.append(prod)
        out.append(_make("fct_order_line", r["row_key"], row, ps, r["root_fact"], date))
    return out


def build_raw_payment(conn, date, parents):
    return _tag("raw_payment", _source_rows(conn, "src_pay", date, ("row",)))


def build_stg_payment(conn, date, parents):
    out = []
    for r in parents["raw_payment"]:
        p = r["payload"]
        row = {k: p.get(k) for k in ("payment_id", "order_id", "currency", "method", "captured_at")}
        row["amount"] = float(p.get("amount") or 0.0)
        out.append(_make("stg_payment", r["row_key"], row, [r], r["root_fact"], date))
    return out


def build_fct_payment(conn, date, parents):
    orders = {r["payload"].get("order_id"): r for r in parents.get("fct_order", [])}
    out = []
    for r in parents["stg_payment"]:
        p = r["payload"]
        header = orders.get(p.get("order_id"))
        row = {
            **p,
            "order_currency": header["payload"].get("currency") if header else None,
            "currency_matches_order": header is not None and header["payload"].get("currency") == p.get("currency"),
        }
        ps: list[Row] = [r]
        if header is not None:
            ps.append(header)
        out.append(_make("fct_payment", r["row_key"], row, ps, r["root_fact"], date))
    return out


def build_raw_shipment(conn, date, parents):
    return _tag("raw_shipment", _source_rows(conn, "src_wms", date, ("row",)))


def build_stg_shipment(conn, date, parents):
    out = []
    for r in parents["raw_shipment"]:
        p = r["payload"]
        carrier = p.get("carrier")
        row = {**{k: p.get(k) for k in ("shipment_id", "order_id", "shipped_at", "delivered_at")},
               "carrier": carrier.strip() if isinstance(carrier, str) else carrier,
               "weight_kg": float(p.get("weight_kg") or 0.0)}
        out.append(_make("stg_shipment", r["row_key"], row, [r], r["root_fact"], date))
    return out


def build_fct_shipment(conn, date, parents):
    out = []
    for r in parents["stg_shipment"]:
        p = r["payload"]
        row = {k: p.get(k) for k in ("shipment_id", "order_id", "carrier", "shipped_at", "delivered_at", "weight_kg")}
        row["shipment_date"] = (row.get("shipped_at") or "")[:10]
        out.append(_make("fct_shipment", r["row_key"], row, [r], r["root_fact"], date))
    return out


def build_mart_revenue_daily(conn, date, parents):
    groups: dict[tuple[str, str], list[Row]] = defaultdict(list)
    for r in parents["fct_order_line"]:
        p = r["payload"]
        groups[(p.get("order_date") or "unknown", p.get("category") or "unknown")].append(r)
    out = []
    for (day, cat), rows in sorted(groups.items()):
        row = {
            "order_date": day,
            "category": cat,
            "lines": len(rows),
            "units": sum(int(r["payload"].get("quantity") or 0) for r in rows),
            "revenue": round(sum(r["payload"].get("line_total") or 0.0 for r in rows), 2),
        }
        out.append(_make("mart_revenue_daily", f"{day}:{cat}", row, rows, _parent_roots(rows), date))
    return out


def build_mart_fulfilment(conn, date, parents):
    groups: dict[tuple[str, str], list[Row]] = defaultdict(list)
    for r in parents["fct_shipment"]:
        p = r["payload"]
        groups[(p.get("shipment_date") or "unknown", p.get("carrier") or "unknown")].append(r)
    out = []
    for (day, carrier), rows in sorted(groups.items()):
        row = {
            "shipment_date": day,
            "carrier": carrier,
            "shipments": len(rows),
            "delivered": sum(1 for r in rows if r["payload"].get("delivered_at")),
            "weight_kg": round(sum(r["payload"].get("weight_kg") or 0.0 for r in rows), 2),
        }
        out.append(_make("mart_fulfilment", f"{day}:{carrier}", row, rows, _parent_roots(rows), date))
    return out


BUILDERS = {
    "raw_customer": build_raw_customer,
    "stg_customer": build_stg_customer,
    "dim_customer": build_dim_customer,
    "mart_customer_health": build_mart_customer_health,
    "raw_order": build_raw_order,
    "stg_order": build_stg_order,
    "fct_order": build_fct_order,
    "raw_order_line": build_raw_order_line,
    "fct_order_line": build_fct_order_line,
    "raw_product": build_raw_product,
    "stg_product": build_stg_product,
    "raw_payment": build_raw_payment,
    "stg_payment": build_stg_payment,
    "fct_payment": build_fct_payment,
    "raw_shipment": build_raw_shipment,
    "stg_shipment": build_stg_shipment,
    "fct_shipment": build_fct_shipment,
    "mart_revenue_daily": build_mart_revenue_daily,
    "mart_fulfilment": build_mart_fulfilment,
}

ORDER = [
    "raw_customer", "stg_customer", "dim_customer",
    "raw_order", "stg_order", "fct_order",
    "raw_product", "stg_product", "raw_order_line", "fct_order_line",
    "raw_payment", "stg_payment", "fct_payment",
    "raw_shipment", "stg_shipment", "fct_shipment",
    "mart_revenue_daily", "mart_fulfilment", "mart_customer_health",
]


def build_all(conn, date: str, only: set[str] | None = None,
              base: dict[str, list[Row]] | None = None) -> dict[str, list[Row]]:
    """Build datasets for a snapshot, parents first.

    ``only`` restricts rebuilds; inputs outside it are taken from ``base``
    (the previous run's materialisation).  This is what makes a targeted
    backfill recompute only the affected slice instead of the whole pipeline.
    """
    base = base or {}
    built: dict[str, list[Row]] = {}
    for ds in ORDER:
        if only is not None and ds not in only:
            if ds in base:
                built[ds] = base[ds]
            continue
        parents = {p: built.get(p, []) for p in INPUTS[ds]}
        for side in SIDE_INPUTS.get(ds, []):
            parents[f"_{side}"] = built.get(side, [])
        built[ds] = BUILDERS[ds](conn, date, parents)
    return built


def persist(conn, run_id: str, date: str, rows_by_ds: dict[str, list[Row]]) -> int:
    n = 0
    for ds, rows in rows_by_ds.items():
        conn.executemany(
            "INSERT OR REPLACE INTO dataset_rows(run_id,dataset_id,row_key,payload,snapshot_date,origin) VALUES(?,?,?,?,?,?)",
            [(run_id, ds, r["row_key"], json.dumps(r["payload"], sort_keys=True), date,
              json.dumps({"parents": r["origin"], "root_fact": r["root_fact"]}, sort_keys=True)) for r in rows],
        )
        n += len(rows)
    return n


def load_rows(conn, run_id: str, only: set[str] | None = None) -> dict[str, list[Row]]:
    built: dict[str, list[Row]] = defaultdict(list)
    rows = conn.execute("SELECT dataset_id,row_key,payload,origin,snapshot_date FROM dataset_rows WHERE run_id=?", (run_id,))
    for r in rows:
        ds = r["dataset_id"]
        if only is not None and ds not in only:
            continue
        meta = json.loads(r["origin"])
        built[ds].append({
            "_dataset": ds,
            "row_key": r["row_key"],
            "payload": json.loads(r["payload"]),
            "origin": meta["parents"],
            "root_fact": meta["root_fact"],
            "snapshot_date": r["snapshot_date"],
        })
    return built


def row_edges(rows_by_ds: dict[str, list[Row]]) -> dict[tuple[str, str], list[dict[str, str]]]:
    """parent (dataset, record) -> [{dataset, record_id, propagation}] child rows.

    ``propagation`` comes from the relations table at trace time; here we only
    record which child rows consumed which parent rows.
    """
    idx: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for ds, rows in rows_by_ds.items():
        for r in rows:
            for o in r["origin"]:
                idx[(o["dataset"], o["record_id"])].append({
                    "dataset": ds, "record_id": r["row_key"], "row_key": r["row_key"],
                })
    return idx
