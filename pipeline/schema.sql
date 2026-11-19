-- Fictional data-quality / blast-radius platform.
-- Append-only where audit matters; mutable state is limited to control tables.

PRAGMA user_version = 1;

-- ---------------------------------------------------------------- topology
CREATE TABLE IF NOT EXISTS sources (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    kind              TEXT NOT NULL,              -- oltp | file | api | stream
    freshness_sla_min INTEGER NOT NULL,
    owner_team        TEXT NOT NULL,
    description       TEXT
);

CREATE TABLE IF NOT EXISTS datasets (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    layer         TEXT NOT NULL,                  -- raw | staging | dim | fct | mart
    source_id     TEXT REFERENCES sources(id),
    row_count     INTEGER NOT NULL DEFAULT 0,
    partition_key TEXT,                           -- snapshot_date
    description   TEXT
);

-- Graph nodes: parent/child may be a dataset or a report id.
CREATE TABLE IF NOT EXISTS relations (
    parent_id     TEXT NOT NULL,
    child_id      TEXT NOT NULL,
    child_type    TEXT NOT NULL DEFAULT 'dataset', -- dataset | report
    kind          TEXT NOT NULL,                   -- view | sql | stream | export
    propagation   TEXT NOT NULL DEFAULT 'row',     -- row | aggregate
    impact_weight REAL NOT NULL DEFAULT 1.0,
    fields        TEXT,                            -- json list; null = all fields consumed
    note          TEXT,
    PRIMARY KEY (parent_id, child_id)
);
CREATE INDEX IF NOT EXISTS idx_relations_child ON relations(child_id);

CREATE TABLE IF NOT EXISTS reports (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    owner_team     TEXT NOT NULL,
    criticality    TEXT NOT NULL,                  -- tier1 | tier2 | tier3
    cadence        TEXT NOT NULL,
    base_dataset   TEXT NOT NULL,
    description    TEXT
);


-- Missing / partially observed lineage evidence, stored explicitly so that
-- "unknown" can never be rendered as "no impact".
CREATE TABLE IF NOT EXISTS lineage_gaps (
    id            TEXT PRIMARY KEY,
    dataset_id    TEXT NOT NULL,
    direction     TEXT NOT NULL,                   -- upstream | downstream
    reason        TEXT NOT NULL,                   -- external_consumer | stale_metadata | manual_upload
    severity      TEXT NOT NULL DEFAULT 'medium',
    note          TEXT
);
CREATE INDEX IF NOT EXISTS idx_lineage_gaps_ds ON lineage_gaps(dataset_id);

-- ---------------------------------------------------------------- records
CREATE TABLE IF NOT EXISTS source_records (
    source_id     TEXT NOT NULL REFERENCES sources(id),
    record_id     TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'row',     -- row | order | line | arrival
    payload       TEXT NOT NULL,                   -- canonical json
    snapshot_date TEXT,
    ingested_at   TEXT NOT NULL,
    fixed_at      TEXT,                            -- non-null = upstream fix applied
    fix_note      TEXT,
    PRIMARY KEY (source_id, record_id)
);

CREATE TABLE IF NOT EXISTS dataset_rows (
    run_id        TEXT NOT NULL REFERENCES runs(id),
    dataset_id    TEXT NOT NULL,
    row_key       TEXT NOT NULL,
    payload       TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    origin        TEXT,                            -- json {source_id, record_id} when traceable to a fact
    PRIMARY KEY (run_id, dataset_id, row_key)
);
CREATE INDEX IF NOT EXISTS idx_dataset_rows_key ON dataset_rows(dataset_id, row_key);

-- ---------------------------------------------------------------- rules
CREATE TABLE IF NOT EXISTS rule_sets (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    status        TEXT NOT NULL,                   -- draft | active | superseded
    version       INTEGER NOT NULL,
    weights       TEXT NOT NULL,                   -- json {dimension: weight}
    thresholds    TEXT NOT NULL,                   -- json {dimension: red_threshold}
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    parent_id     TEXT REFERENCES rule_sets(id),
    notes         TEXT
);
CREATE INDEX IF NOT EXISTS idx_rule_sets_status ON rule_sets(name, status, version);

CREATE TABLE IF NOT EXISTS rules (
    rule_set_id   TEXT NOT NULL REFERENCES rule_sets(id),
    rule_id       TEXT NOT NULL,
    name          TEXT NOT NULL,
    dimension     TEXT NOT NULL,
    dataset_id    TEXT NOT NULL,
    field         TEXT,
    severity      TEXT NOT NULL,                   -- low | medium | high
    expression    TEXT NOT NULL,                   -- json spec, see pipeline/checks.py
    weight        REAL NOT NULL DEFAULT 1.0,
    materiality   TEXT NOT NULL DEFAULT 'critical',-- critical | advisory
    description   TEXT,
    PRIMARY KEY (rule_set_id, rule_id)
);

-- ---------------------------------------------------------------- runs
CREATE TABLE IF NOT EXISTS runs (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,                   -- full | targeted | backfill | report_refresh | simulation
    mode          TEXT NOT NULL DEFAULT 'actual',  -- actual | estimate
    status        TEXT NOT NULL,                   -- queued | running | completed | partial | interrupted | failed
    rule_set_id   TEXT NOT NULL REFERENCES rule_sets(id),
    snapshot_date TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    requested_by  TEXT NOT NULL,
    scope         TEXT,                            -- json {datasets, record_keys, reason}
    parent_run_id TEXT REFERENCES runs(id),
    fault_at_step INTEGER,
    notes         TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_snapshot ON runs(snapshot_date, status);

CREATE TABLE IF NOT EXISTS run_steps (
    run_id        TEXT NOT NULL REFERENCES runs(id),
    step_no       INTEGER NOT NULL,
    name          TEXT NOT NULL,
    status        TEXT NOT NULL,                   -- pending | running | done | skipped | reused | failed | interrupted
    started_at    TEXT,
    finished_at   TEXT,
    detail        TEXT,
    PRIMARY KEY (run_id, step_no)
);

CREATE TABLE IF NOT EXISTS ingest_snapshots (
    id            TEXT PRIMARY KEY,
    source_id     TEXT NOT NULL REFERENCES sources(id),
    snapshot_date TEXT NOT NULL,
    expected_at   TEXT,
    arrived_at    TEXT NOT NULL,
    record_count  INTEGER NOT NULL,
    late          INTEGER NOT NULL DEFAULT 0,
    UNIQUE (source_id, snapshot_date)
);

-- ---------------------------------------------------------------- evaluation
-- One row per (run, rule, record) that the rule inspected.  This is the
-- fingerprint that lets a score be expanded back to the original fact.
CREATE TABLE IF NOT EXISTS findings (
    id            TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL REFERENCES runs(id),
    rule_set_id   TEXT NOT NULL,
    rule_id       TEXT NOT NULL,
    dataset_id    TEXT NOT NULL,
    record_id     TEXT NOT NULL,
    dimension     TEXT NOT NULL,
    field         TEXT,
    severity      TEXT NOT NULL,
    passed        INTEGER NOT NULL,
    actual        TEXT,
    expected      TEXT,
    detail        TEXT,
    scope         TEXT NOT NULL DEFAULT 'row',    -- row | partition
    partition_value TEXT,
    snapshot_date TEXT,
    reused_from   TEXT REFERENCES runs(id)
);
CREATE INDEX IF NOT EXISTS idx_findings_run_ds ON findings(run_id, dataset_id, dimension, passed);
CREATE INDEX IF NOT EXISTS idx_findings_run_rule ON findings(run_id, rule_id);
CREATE INDEX IF NOT EXISTS idx_findings_record ON findings(dataset_id, record_id);

-- A finding propagated along the lineage graph.  ``material`` = the child
-- actually consumes the violated field on that edge; traced rows are kept too
-- so the UI can distinguish "touched" from "affected".
CREATE TABLE IF NOT EXISTS lineage_hits (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES runs(id),
    root_dataset    TEXT NOT NULL,
    root_record_id  TEXT NOT NULL,
    root_finding_id TEXT NOT NULL,
    impacted_node   TEXT NOT NULL,
    node_type       TEXT NOT NULL,                -- dataset | report
    depth           INTEGER NOT NULL,
    path            TEXT NOT NULL,                -- json list of node ids
    material        INTEGER NOT NULL DEFAULT 1,
    weight          REAL NOT NULL DEFAULT 1.0,
    status          TEXT NOT NULL,                -- measured | estimated
    scope           TEXT NOT NULL DEFAULT 'row',  -- row | partition
    partition_value TEXT,
    note            TEXT
);
CREATE INDEX IF NOT EXISTS idx_lineage_hits_run_node ON lineage_hits(run_id, impacted_node);
CREATE INDEX IF NOT EXISTS idx_lineage_hits_root ON lineage_hits(run_id, root_dataset, root_record_id);

-- Scores are versioned by run + rule set; never updated in place.
CREATE TABLE IF NOT EXISTS scores (
    id            TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL REFERENCES runs(id),
    rule_set_id   TEXT NOT NULL,
    dataset_id    TEXT NOT NULL,
    dimension     TEXT NOT NULL,                  -- five dims or 'overall'
    kind          TEXT NOT NULL,                  -- actual | estimate
    passed        INTEGER NOT NULL,
    evaluated     INTEGER NOT NULL,
    violations    INTEGER NOT NULL DEFAULT 0,
    score         REAL NOT NULL,                  -- 0..100
    weight        REAL NOT NULL,
    severity      TEXT,
    computed_at   TEXT NOT NULL,
    formula       TEXT NOT NULL DEFAULT 'weighted_pass_rate',
    reused_from   TEXT REFERENCES runs(id)
);
CREATE INDEX IF NOT EXISTS idx_scores_run ON scores(run_id, dataset_id);
CREATE INDEX IF NOT EXISTS idx_scores_ds_dim ON scores(dataset_id, dimension, kind);

-- Per-record impact, materialised so the reports service never walks the graph.
CREATE TABLE IF NOT EXISTS record_impacts (
    run_id          TEXT NOT NULL REFERENCES runs(id),
    root_dataset    TEXT NOT NULL,
    record_id       TEXT NOT NULL,
    root_finding_id TEXT NOT NULL,
    min_depth       INTEGER,
    material_nodes  INTEGER NOT NULL DEFAULT 0,
    traced_nodes    INTEGER NOT NULL DEFAULT 0,
    computed_at     TEXT NOT NULL,
    PRIMARY KEY (run_id, root_dataset, record_id)
);

-- ---------------------------------------------------------------- reports
CREATE TABLE IF NOT EXISTS report_artifacts (
    run_id        TEXT NOT NULL REFERENCES runs(id),
    report_id     TEXT NOT NULL REFERENCES reports(id),
    report_name   TEXT NOT NULL,
    kind          TEXT NOT NULL,                  -- actual | estimate
    impact_status TEXT NOT NULL,                  -- measured | estimated | unknown | traced_only | clear
    material_rows INTEGER NOT NULL,               -- distinct root records materially affecting it
    traced_rows   INTEGER NOT NULL,               -- superset: root records whose lineage touches it
    affected_pct  REAL NOT NULL,
    blind_spots   TEXT,                           -- json list of {dataset,reason,severity,note}
    path_depth    INTEGER,
    impact_weight REAL NOT NULL DEFAULT 0,
    drivers       TEXT,                           -- json [{dataset, rule_id, dimension, violations}]
    cost_kind     TEXT NOT NULL DEFAULT 'estimate',
    cost_rows     INTEGER NOT NULL,               -- partition rows that must be scanned
    cost_usd      REAL NOT NULL,
    cost_seconds  REAL NOT NULL,
    cost_basis    TEXT NOT NULL,
    confidence    TEXT NOT NULL,                  -- high | medium | low | unknown
    computed_at   TEXT NOT NULL,
    PRIMARY KEY (run_id, report_id)
);

CREATE TABLE IF NOT EXISTS impact_estimates (
    id                 TEXT PRIMARY KEY,
    run_id             TEXT NOT NULL REFERENCES runs(id),
    dataset_id         TEXT NOT NULL,
    partition_key      TEXT,
    partition_value    TEXT,
    method             TEXT NOT NULL,             -- row_trace | hit_rate_extrapolation | partition_scan
    estimated_rows     INTEGER NOT NULL,
    estimated_cost_usd REAL NOT NULL,
    confidence         TEXT NOT NULL,
    explanation        TEXT
);

CREATE TABLE IF NOT EXISTS report_snapshots (
    id            TEXT PRIMARY KEY,
    report_id     TEXT NOT NULL,
    run_id        TEXT NOT NULL REFERENCES runs(id),
    rule_set_id   TEXT NOT NULL,
    generated_at  TEXT NOT NULL,
    payload       TEXT NOT NULL
);

-- ---------------------------------------------------------------- control plane
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,                  -- quality_run | backfill | resume | estimate
    payload       TEXT NOT NULL,
    status        TEXT NOT NULL,                  -- queued | running | done | failed
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created_at);

CREATE TABLE IF NOT EXISTS events (
    id            TEXT PRIMARY KEY,
    at            TEXT NOT NULL,
    actor         TEXT NOT NULL,
    action        TEXT NOT NULL,
    subject       TEXT,
    payload       TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_at ON events(at);

-- Partition-level cost book: the one place costs come from.  Impact tracing is
-- per record (measured); cost is per partition (always an estimate).
CREATE TABLE IF NOT EXISTS partition_costs (
    dataset_id      TEXT NOT NULL REFERENCES datasets(id),
    partition_key   TEXT NOT NULL,
    partition_value TEXT NOT NULL,
    rows            INTEGER NOT NULL,
    bytes           INTEGER NOT NULL,
    scan_cost_usd   REAL NOT NULL,
    rows_per_second REAL NOT NULL DEFAULT 50000,
    PRIMARY KEY (dataset_id, partition_key, partition_value)
);
