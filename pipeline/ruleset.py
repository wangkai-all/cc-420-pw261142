"""The default rule-set definition (fiction: Lumen Kits).

``materiality`` decides what a violation means to a downstream consumer:

  critical   the value a report renders is wrong -> counts as affected
  advisory   data hygiene; lineage still carries it, but reports show it as
             "attention" not "affected"

The same broken email is critical for the CSM dashboard (email_present column)
and advisory for the finance pack (email is only joined, never displayed).
"""

from __future__ import annotations

from typing import Any

EMAIL_RE = r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$"

RULESET_NAME = "lumen-core"


def rules() -> list[dict[str, Any]]:
    return [
        # ---------------- completeness
        dict(rule_id="cmp_customer_email", name="Customer email present", dimension="completeness",
             dataset_id="stg_customer", field="email", severity="high", materiality="critical",
             expression={"op": "not_null", "field": "email"}, weight=1.0,
             description="CSM dashboard and any email join require a non-null email."),
        dict(rule_id="cmp_customer_name", name="Customer name present", dimension="completeness",
             dataset_id="stg_customer", field="name", severity="medium", materiality="advisory",
             expression={"op": "not_null", "field": "name"}, weight=0.5,
             description="Hygiene only: no report renders the name."),
        dict(rule_id="cmp_order_customer", name="Order has customer key", dimension="completeness",
             dataset_id="stg_order", field="customer_id", severity="high", materiality="critical",
             expression={"op": "not_null", "field": "customer_id"}, weight=1.0,
             description="Order fact cannot be attributed to a customer -> revenue and health marts."),
        dict(rule_id="cmp_order_line_sku", name="Order line has SKU", dimension="completeness",
             dataset_id="fct_order_line", field="sku", severity="high", materiality="critical",
             expression={"op": "not_null", "field": "sku"}, weight=1.0,
             description="Product mix report groups by SKU."),
        dict(rule_id="cmp_product_price", name="Product price present", dimension="completeness",
             dataset_id="stg_product", field="unit_price", severity="high", materiality="critical",
             expression={"op": "not_null", "field": "unit_price"}, weight=1.0, description="Price is required for every line total; a null price silently zeroes revenue."),
        dict(rule_id="cmp_shipment_carrier", name="Shipment carrier present", dimension="completeness",
             dataset_id="stg_shipment", field="carrier", severity="high", materiality="critical",
             expression={"op": "not_null", "field": "carrier"}, weight=1.0,
             description="Ops wallboard splits by carrier."),
        dict(rule_id="cmp_header_total", name="Order header total present", dimension="completeness",
             dataset_id="fct_order", field="total", severity="high", materiality="critical",
             expression={"op": "not_null", "field": "total"}, weight=1.0, description="Finance close renders the header total; a null total fails the export."),
        dict(rule_id="cmp_order_status", name="Order status present", dimension="completeness",
             dataset_id="fct_order", field="status", severity="medium", materiality="critical",
             expression={"op": "not_null", "field": "status"}, weight=0.6, description="Status drives the close funnel and the ops wallboard."),
        dict(rule_id="uniq_order_header", name="Order id unique", dimension="uniqueness",
             dataset_id="fct_order", field="order_id", severity="high", materiality="critical",
             expression={"op": "unique", "field": "order_id"}, weight=1.0,
             description="A duplicated header double-counts revenue at close."),
        dict(rule_id="uniq_shipment_id", name="Shipment id unique", dimension="uniqueness",
             dataset_id="fct_shipment", field="shipment_id", severity="medium", materiality="advisory",
             expression={"op": "unique", "field": "shipment_id"}, weight=0.6, description="Duplicate shipment ids inflate on-time counts on the ops board."),
        dict(rule_id="cmp_payment_order", name="Payment has order id", dimension="completeness",
             dataset_id="stg_payment", field="order_id", severity="high", materiality="critical",
             expression={"op": "not_null", "field": "order_id"}, weight=1.0, description="An unattributed payment cannot be reconciled to an order at close."),

        # ---------------- consistency
        dict(rule_id="con_customer_fk", name="Order customer exists", dimension="consistency",
             dataset_id="fct_order", field="customer_id", severity="high", materiality="critical",
             expression={"op": "foreign_key", "field": "customer_id", "ref_dataset": "dim_customer"}, weight=1.0,
             description="Orders joining to no customer drop out of customer health silently."),
        dict(rule_id="con_header_total", name="Order header equals line sum", dimension="consistency",
             dataset_id="fct_order", field="total", severity="high", materiality="critical",
             expression={"op": "sum_match", "right": "fct_order_line", "right_field": "line_total",
                         "left_field": "total", "group_by": "order_id", "tolerance": 0.01}, weight=1.2,
             description="Revenue is summed from lines but reported from the header at close."),
        dict(rule_id="con_payment_currency", name="Capture currency matches order", dimension="consistency",
             dataset_id="fct_payment", field="currency", severity="high", materiality="critical",
             expression={"op": "equals_field", "field": "currency", "other": "order_currency"}, weight=1.0,
             description="Finance pack converts by currency; a mismatch mis-states cash."),
        dict(rule_id="con_line_product_fk", name="Order line SKU exists in catalogue", dimension="consistency",
             dataset_id="fct_order_line", field="sku", severity="medium", materiality="critical",
             expression={"op": "foreign_key", "field": "sku", "ref_dataset": "stg_product", "ref_field": "sku"}, weight=1.0,
             description="Unknown SKU -> 'unknown' category bucket in product mix."),
        dict(rule_id="con_customer_unique", name="Customer id unique", dimension="consistency",
             dataset_id="dim_customer", field="customer_id", severity="high", materiality="critical",
             expression={"op": "unique", "field": "customer_id"}, weight=1.0, description="One customer per id; duplicates split the health score."),

        # ---------------- timeliness
        dict(rule_id="tim_payment_arrival", name="Payments feed arrives within SLA", dimension="timeliness",
             dataset_id="stg_payment", field=None, severity="high", materiality="critical",
             expression={"op": "arrival_sla", "hours": 0}, weight=1.0,
             description="Partition-scoped: cost and impact are estimates, not row counts."),
        dict(rule_id="tim_shop_arrival", name="Shop feed arrives within SLA", dimension="timeliness",
             dataset_id="stg_order", field=None, severity="high", materiality="critical",
             expression={"op": "arrival_sla", "hours": 0}, weight=1.0, description="Shop stream must land by 04:00; a late partition delays the daily dashboard."),
        dict(rule_id="tim_wms_arrival", name="WMS feed arrives within SLA", dimension="timeliness",
             dataset_id="stg_shipment", field=None, severity="medium", materiality="advisory",
             expression={"op": "arrival_sla", "hours": 0}, weight=0.6, description="WMS extract must land by 05:00 before the ops board refresh."),
        dict(rule_id="tim_customer_recency", name="Customer record refreshed recently", dimension="timeliness",
             dataset_id="stg_customer", field="updated_at", severity="medium", materiality="advisory",
             expression={"op": "not_stale", "field": "updated_at", "hours": 72}, weight=0.8, description="CRM snapshot older than 3 days makes health scores stale, not just wrong."),

        # ---------------- validity
        dict(rule_id="val_customer_email_format", name="Customer email format", dimension="validity",
             dataset_id="stg_customer", field="email", severity="medium", materiality="critical",
             expression={"op": "regex", "field": "email", "pattern": EMAIL_RE}, weight=0.8,
             description="Malformed email cannot be used by the CSM digest."),
        dict(rule_id="val_customer_segment", name="Customer segment in enum", dimension="validity",
             dataset_id="stg_customer", field="segment", severity="medium", materiality="critical",
             expression={"op": "enum", "field": "segment", "values": ["smb", "mid", "enterprise"]}, weight=1.0, description="Segment feeds every mart split; an unknown value lands in no bucket."),
        dict(rule_id="val_order_status", name="Order status in lifecycle enum", dimension="validity",
             dataset_id="fct_order", field="status", severity="high", materiality="critical",
             expression={"op": "enum", "field": "status", "values": ["pending", "paid", "shipped", "cancelled"]}, weight=1.0, description="Unrecognised statuses vanish from the close funnel."),
        dict(rule_id="val_order_total_nonneg", name="Order total non-negative", dimension="validity",
             dataset_id="fct_order", field="total", severity="medium", materiality="critical",
             expression={"op": "range", "field": "total", "min": 0}, weight=0.8, description="Negative order totals are returns misfiled as sales."),
        dict(rule_id="val_product_price_positive", name="Product price positive", dimension="validity",
             dataset_id="stg_product", field="unit_price", severity="high", materiality="critical",
             expression={"op": "range", "field": "unit_price", "min": 0.01, "max": 100000}, weight=1.0,
             description="Negative price poisons revenue in the exec headline."),
        dict(rule_id="val_product_category", name="Product category in enum", dimension="validity",
             dataset_id="stg_product", field="category", severity="low", materiality="critical",
             expression={"op": "enum", "field": "category", "values": ["electronics", "home", "outdoor", "kitchen", "stationery"]}, weight=0.6, description="Category drives the product mix report; an unmapped value renders blank."),
        dict(rule_id="val_payment_amount_positive", name="Capture amount positive", dimension="validity",
             dataset_id="fct_payment", field="amount", severity="high", materiality="critical",
             expression={"op": "range", "field": "amount", "min": 0.01}, weight=1.0, description="Zero or negative payments break the cash forecast."),
        dict(rule_id="val_ship_carrier_enum", name="Carrier normalised", dimension="validity",
             dataset_id="stg_shipment", field="carrier", severity="medium", materiality="critical",
             expression={"op": "enum", "field": "carrier", "values": ["DHL", "DPD", "UPS", "GLS"]}, weight=0.8, description="Carrier must map to the ops board's lane list."),
        dict(rule_id="val_ship_weight", name="Shipment weight positive", dimension="validity",
             dataset_id="stg_shipment", field="weight_kg", severity="low", materiality="advisory",
             expression={"op": "range", "field": "weight_kg", "min": 0.0, "max": 1000}, weight=0.4, description="Weight must be plausible for the carrier cost model."),

        # ---------------- uniqueness
        dict(rule_id="uniq_product_sku", name="Catalogue SKU unique", dimension="uniqueness",
             dataset_id="stg_product", field="sku", severity="high", materiality="critical",
             expression={"op": "unique", "field": "sku"}, weight=1.0,
             description="Duplicate SKU fans a line out to two categories."),
        dict(rule_id="uniq_order_line", name="Order line ids unique", dimension="uniqueness",
             dataset_id="fct_order_line", field="line_id", severity="high", materiality="critical",
             expression={"op": "unique", "field": "line_id"}, weight=1.0, description="Duplicate lines double-count units in the product mix report."),
        dict(rule_id="uniq_payment_id", name="Payment ids unique", dimension="uniqueness",
             dataset_id="fct_payment", field="payment_id", severity="high", materiality="critical",
             expression={"op": "unique", "field": "payment_id"}, weight=1.0, description="Duplicate payments overstate collected cash in treasury."),

        # ---------------- partition volume (estimate-only impact)
        dict(rule_id="vol_orders_partition", name="Order partition volume sane", dimension="completeness",
             dataset_id="fct_order", field=None, severity="high", materiality="critical",
             expression={"op": "volume_anomaly", "min_rows": 250}, weight=0.5,
             description="Guards against a partial load; impact can only be estimated."),
    ]


WEIGHTS = {"completeness": 0.30, "consistency": 0.25, "timeliness": 0.15, "validity": 0.20, "uniqueness": 0.10}
# Operating targets: below threshold is amber, below 90% of it is red.
THRESHOLDS = {"completeness": 97.0, "consistency": 95.0, "timeliness": 95.0, "validity": 98.0, "uniqueness": 99.0}
