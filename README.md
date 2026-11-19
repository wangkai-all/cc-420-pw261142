# Lumen Kits — data-quality console

A fictional but end-to-end data platform: five raw sources, nineteen datasets,
eight reports, a lineage graph with declared blind spots, and a scoring engine
whose every number expands down to the source record that caused it.

Everything is Python 3.11 standard library plus SQLite; the UI is a no-build
vanilla-JS single page served by the same process. There are no external
services to start.

```
python -m scripts.bootstrap --force     # schema + fixture + rule sets v1/v2 + two runs
python -m scripts.demo                  # the whole story, printed as a transcript
python -m pipeline.cli serve --port 8787
python -m unittest discover -s tests -v
```

`bootstrap` is idempotent-by-refusal: it will not overwrite an existing
database unless `--force` is passed. `demo` always builds a throwaway
`data/demo.db` (pass `--keep` to inspect it afterwards).

---

## The services

The three pieces run as separate entry points over one shared SQLite database
(WAL). Nothing imports another service's internals; they communicate through
tables and through `pipeline/services.py`, which is the only layer that knows
about both.

| Service | Entry point | Owns |
|---|---|---|
| **Rule service** | `RuleService`, `pipeline/rules.py` | rule sets and their versions; makes new versions, never edits old ones; serves historical scores per version |
| **Dependency processor** | `ProcessorService`, `pipeline/processor.py` | ingests, transforms, evaluates rules, traces lineage, scores, materialises reports; checkpointed and resumable |
| **Reports / consumer end** | `ReportService`, `pipeline/reports.py` | what a report actually sees: measured rows, estimates, blind spots, cost, drill-down |

The UI reads through `pipeline/api.py`, a small `http.server` shim exposing
`/api/*`; the CLI (`pipeline/cli.py`) reads the same service methods.

### Rules are versioned, scores are immutable

A rule set row is never updated in place. `revise()` copies the parent's rules
into a new draft, applies weights/thresholds/rule patches, then flips the child
to `active` and the parent to `superseded` in the same transaction. Every score
row carries `(run_id, rule_set_id)`, so:

* `GET /api/history/<dataset>` returns one point per run, labelled with the
  version that produced it — v1 scores stay queryable after v3 exists;
* a targeted recompute copies findings/scores from the base run with
  `reused_from`, so an untouched dataset's score in the new run is explicitly
  attributed to an older run rather than silently recalculated.

Weights are stored exactly as entered. They are *not* renormalised: if an
operator sets validity to 0.35 and leaves the rest, the dimension carries
proportionally more weight. The effective per-rule weight
(`rule weight × dimension weight`) is exposed separately on each rule set.

### Scoring

Five dimensions: completeness, consistency, timeliness, validity, uniqueness.
Within a dimension:

```
loss      = min(1, (1 - pass_rate) × severity_factor)     # low 1.0, medium 1.5, high 2.5
pass_w   += rule_weight × (1 - loss)
dimension = Σ pass_w / Σ rule_weight
overall   = weighted mean over dimensions that have at least one rule
```

A dimension with no rule is reported as **uncovered** (`n/a`), never as 100 —
an absent check is not a pass. The UI draws it as a hatched, dimmed bar, and
`/api/scorecard` lists it under `uncovered_dimensions`.

Every score row stores its `formula`, `weight`, `severity` and the count it was
computed from, so the drawer can explain the number rather than just print it.

---

## Impact: measured vs estimated vs unknown

The processor records, for every derived row, its `origin` — the parent rows and
the root source fact. When a rule fails, `impact.py` walks that graph forward
and writes `lineage_hits`:

* **measured** — a specific root record reaches a specific node, with the path
  and depth that got it there. Scope is `row`.
* **estimated** — the evidence is partition-scoped only (arrival SLA, row-count
  drift, volume anomaly). There is no row key to follow, so the report gets an
  `impact_estimates` row with method, estimated rows and cost, and scope
  `partition`. These never appear in `lineage_hits` and never appear in the
  "material rows" column.
* **unknown** — a declared `lineage_gaps` entry (an FTP consumer nobody
  registered, stale metadata, a manual upload) means part of the consumer
  surface is invisible. The report is forced to `unknown`; it is *not* `clear`.
  The UI says "Unknown lineage is not 'no impact'" and lists the gap.
* **traced_only** — lineage reaches the report, but no field the report renders
  is broken (the edge's `fields` list and the rule's `materiality` decide this).
* **clear** — reachable and nothing material, with no gaps.

Materiality has two inputs. The rule is `critical` (a rendered value is wrong)
or `advisory` (hygiene); the edge declares which child fields it carries and an
impact weight. The same broken email is material for the CSM dashboard and only
touched for finance close — which is exactly the "same error, different
downstream" story, visible in the report table as different material-row counts
for the same run.

Cost is always an estimate and always labelled: `report_artifacts.cost_kind` is
`"estimate"`, `cost_basis` names the method (partition scan over the upstream
closure at the fixture's rows-per-second), and the UI keeps it in its own
columns. Row-level tracing is measured; cost is not, and the two are never
added together.

---

## Recovery and targeted recompute

A run is eight checkpointed steps (`prepare, ingest, transform, evaluate,
trace, score, reports, finalize`), each committed separately with its status and
detail in `run_steps`.

* `proc.simulate_interrupt(at_step=...)` kills the run *entering* that step.
  Completed steps stay `done`, the faulted step is `interrupted`, later steps
  stay `pending`. No partial scores or report rows leak.
* `proc.resume(run_id)` recomputes only the non-done steps; it marks the
  already-done ones `reused: already complete` rather than re-running them, and
  cleans any half-written evidence from the crashed step first.

`proc.fix_and_rerun(...)` applies a patch to source records and computes the
downstream closure from the lineage graph — for a CRM email fix that is
`raw_customer, stg_customer, dim_customer, mart_customer_health` and nothing
else. Datasets outside the closure carry their rows, findings and scores over
from the base run with `reused_from` set; rules on the rebuilt datasets that did
not need re-evaluation are also reused. The demo prints both lists so the claim
is checkable.

---

## The fixture

Fictional retailer "Lumen Kits": five sources (`src_crm`, `src_shop`,
`src_wms`, `src_billing`, `src_catalogue`) land in `stg_*` / `raw_*`, feed
`fct_*` and `dim_*`, and eight reports read from them. Defects are seeded
deterministically (`generate(seed=20261118)`): missing emails, four bad order
totals, a duplicated header, late partitions for two streams, an invalid
segment value, a stale CRM snapshot, and so on — 74 violations across 33 rules
on the baseline run.

Lineage is declared in `fixture.RELATIONS`, including field lists and impact
weights. Four gaps are declared in `fixture.LINEAGE_GAPS`; `rpt_treasury` exists
specifically so that the *unknown* state is reachable, and `rpt_intake_sla`
exists so that *estimated-only* is reachable (it reads a late partition but no
broken row key). Three of the eight reports exist to exercise these states; the
other five are the "real" consumers.

---

## UI

Five tabs, all reading the same API:

* **Scorecard** — per-dataset dimension bars against target, uncovered
  dimensions hatched, violations contributing to each lost point. Clicking a
  dataset opens the drill-down: scores with formula, rules with materiality, and
  violations that expand to the source payload and the downstream nodes they
  reach.
* **Report impact** — the five impact states with a legend, measured rows vs
  estimated cost in separate columns, confidence, blind spots. The gaps table
  explains why an unknown consumer is not a clear one.
* **Rules & history** — five dimension-weight sliders; changing one creates a
  new version (it never edits the active one). Per-rule severity and
  materiality are editable the same way. Version history and per-dataset score
  history sit beside the controls, old versions still present.
* **Runs & recovery** — run list, the eight steps with statuses, a
  "simulate crash" control and a resume button.
* **Lineage** — the graph by layer with each edge's fields and impact weight,
  and the declared gaps drawn as nodes.

Colour follows the dataviz reference palette: status is always colour **plus**
icon plus label, the categorical slots are used in fixed order, and light/dark
are separately chosen rather than flipped.

---

## Layout

```
pipeline/
  schema.sql     tables, constraints, indexes
  db.py          connection + transaction helpers (DATAQ_DB, DATAQ_DB_TIMEOUT)
  fixture.py     sources, datasets, reports, relations, gaps, seeded defects
  model.py       transforms; writes origin (parents + root fact) per row
  checks.py      rule evaluators (row and partition scope)
  rules.py       versioned rule sets and the scoring formula
  ruleset.py     the 33 default rules with severity/materiality/description
  impact.py      lineage graph, measured trace, partition estimates
  reports.py     report artefacts: measured/estimated/unknown, cost, drivers
  runlog.py      step state machine
  processor.py   the eight steps, interruption and resume, targeted recompute
  services.py    RuleService / ProcessorService / ReportService
  api.py         /api/* over http.server
  cli.py         bootstrap, status, scorecard, dataset, impacts, rules, revise,
                 run, fix, interrupt, resume, lineage, errors, events, serve
web/
  index.html, styles.css, app.js
scripts/
  bootstrap.py, demo.py
tests/
  test_pipeline.py
```
