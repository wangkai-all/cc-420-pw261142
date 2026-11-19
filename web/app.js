/* Lumen Kits data-quality console — vanilla JS, no build step. */
"use strict";

const state = {
  runId: null,
  rulesetId: null,
  view: "overview",
  scorecard: null,
  impacts: null,
  runs: [],
  versions: [],
  lineage: null,
  errors: null,
};

const DIMS = ["completeness", "consistency", "timeliness", "validity", "uniqueness"];
const DIM_LABEL = {
  completeness: "Completeness", consistency: "Consistency", timeliness: "Timeliness",
  validity: "Validity", uniqueness: "Uniqueness",
};

/* ------------------------------------------------------------------ utils */
const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "html") n.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(n.dataset, v);
    else n.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return n;
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n, d = 1) => (n === null || n === undefined || Number.isNaN(n)) ? "—" : Number(n).toFixed(d);
const fmtInt = (n) => (n === null || n === undefined) ? "—" : Number(n).toLocaleString("en-US");
const money = (n) => (n === null || n === undefined) ? "—" : "$" + Number(n).toFixed(4);

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.message || data.error || res.statusText);
  return data;
}

function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isErr ? " err" : "");
  t.hidden = false;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.hidden = true; }, isErr ? 6000 : 3200);
}

/* status vocabulary — colour + icon + label, never colour alone */
const STATE_ICON = { good: "✓", warning: "!", serious: "▲", critical: "✕", neutral: "•", unknown: "?" };
function pill(kind, label) {
  return el("span", { class: `pill ${kind}` }, el("span", { class: "dot", "aria-hidden": "true" }, STATE_ICON[kind] || "•"), label);
}
function stateOf(dim) {
  if (!dim) return "neutral";
  const s = dim.state || "neutral";
  return s === "red" ? "critical" : s === "amber" ? "warning" : s === "green" ? "good" : "neutral";
}
function scoreState(score, threshold) {
  if (threshold === null || threshold === undefined) return "neutral";
  if (score < threshold * 0.9) return "critical";
  if (score < threshold) return "warning";
  return "good";
}

/* ------------------------------------------------------------- drawer */
function openDrawer({ kicker, title, body }) {
  $("#drawer-kicker").textContent = kicker || "";
  $("#drawer-title").textContent = title || "";
  $("#drawer-body").replaceChildren(...body);
  $("#drawer").hidden = false;
  document.body.style.overflow = "hidden";
}
function closeDrawer() {
  $("#drawer").hidden = true;
  document.body.style.overflow = "";
}
document.addEventListener("click", (e) => {
  if (e.target.closest("[data-close]")) closeDrawer();
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

/* ------------------------------------------------------------- shell */
async function boot() {
  $("#theme-toggle").addEventListener("click", () => {
    const cur = document.documentElement.dataset.theme;
    const next = cur === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("dataq-theme", next); } catch (_) {}
  });
  try {
    const saved = localStorage.getItem("dataq-theme");
    if (saved) document.documentElement.dataset.theme = saved;
  } catch (_) {}

  document.querySelectorAll(".tab").forEach((t) =>
    t.addEventListener("click", () => switchView(t.dataset.view))
  );
  await refreshState();
  const hash = location.hash.replace("#", "");
  switchView(hash || "overview");
}

async function refreshState() {
  const s = await api("/api/state");
  state.runs = s.runs || [];
  state.versions = s.rule_sets || [];
  state.rulesetId = s.active_rule_set ? s.active_rule_set.id : null;
  state.runId = s.run && s.run.id ? s.run.id : null;
  state.scorecardRun = s.scorecard_run || state.runId;

  const runSel = $("#run-select");
  runSel.replaceChildren(...state.runs.map((r) =>
    el("option", { value: r.id, selected: r.id === state.runId },
      `${r.id.slice(4, 12)} · ${r.kind} · ${r.status}${r.rule_set_id === state.rulesetId ? "" : " · old rules"}`)));
  runSel.onchange = () => { state.runId = runSel.value; state.scorecardRun = runSel.value; render(); };

  const rsSel = $("#ruleset-select");
  rsSel.replaceChildren(...state.versions.slice().reverse().map((v) =>
    el("option", { value: v.id, selected: v.id === state.rulesetId },
      `v${v.version} · ${v.status}${v.parent_id ? " · derived" : ""}`)));
  rsSel.onchange = () => { state.rulesetId = rsSel.value; switchView("rules"); };

  const active = s.active_rule_set;
  $("#brand-sub").textContent = active
    ? `rule set v${active.version} (${active.created_by}) · snapshot ${s.run ? s.run.snapshot_date : "—"} · ${state.runs.length} runs`
    : "no active rule set";
}

function switchView(view) {
  state.view = view;
  document.querySelectorAll(".tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.view === view));
  location.hash = view;
  render();
}

async function render() {
  const view = $("#view");
  view.replaceChildren(el("p", { class: "empty" }, "Loading…"));
  try {
    if (state.view === "overview") await renderOverview(view);
    else if (state.view === "reports") await renderReports(view);
    else if (state.view === "rules") await renderRules(view);
    else if (state.view === "runs") await renderRuns(view);
    else if (state.view === "lineage") await renderLineage(view);
  } catch (err) {
    view.replaceChildren(el("div", { class: "notice warn" }, `Could not render: ${err.message}`));
  }
}

/* ================================================================== overview */
async function renderOverview(root) {
  const run = state.scorecardRun;
  if (!run) {
    root.replaceChildren(el("div", { class: "card empty" }, "No completed run yet. Build one in Runs & recovery."));
    return;
  }
  const sc = await api(`/api/scorecard?run=${encodeURIComponent(run)}`);
  state.scorecard = sc;
  const runMeta = sc.run || {};
  const runInfo = await api(`/api/runs/${encodeURIComponent(run)}`);

  const datasets = sc.datasets || [];
  const worstDims = [];
  for (const d of datasets) {
    for (const [dim, v] of Object.entries(d.dimensions || {})) {
      if (v.state === "red" || v.state === "amber") worstDims.push({ ds: d.dataset_id, dim, ...v });
    }
  }
  worstDims.sort((a, b) => a.score - b.score);

  const impacts = await api(`/api/impacts?run=${encodeURIComponent(run)}`);
  state.impacts = impacts.reports || [];
  const measured = state.impacts.filter((r) => r.impact_status === "measured");
  const unknown = state.impacts.filter((r) => r.impact_status === "unknown" || (r.blind_spots || []).length);
  const totalViol = datasets.reduce((a, d) => a + (d.violations || 0), 0);

  const hero = el("div", { class: "grid cols-4" },
    statTile("Violations in scope", fmtInt(totalViol), `${datasets.length} datasets evaluated`, "warning"),
    statTile("Datasets below target", String(datasets.filter((d) => Object.values(d.dimensions || {}).some((v) => v.state === "red" || v.state === "amber")).length), "of " + datasets.length + " scored", "critical"),
    statTile("Reports materially affected", String(measured.length), `of ${state.impacts.length} registered reports`, "critical"),
    statTile("Reports with blind spots", String(unknown.length), "consumer lineage unknown", "neutral"),
  );

  /* score table */
  const rows = datasets.map((d) => {
    const dims = DIMS.map((dim) => {
      const v = d.dimensions ? d.dimensions[dim] : null;
      if (!v) {
        return el("div", { class: "dim uncovered", title: "No rule covers this dimension for this dataset" },
          el("div", { class: "dim-label" }, el("span", {}, DIM_LABEL[dim].slice(0, 4)), el("span", { class: "dim-score" }, "n/a")),
          el("div", { class: "bar neutral" }, el("span", { style: "width:100%" })));
      }
      const st = stateOf(v);
      return el("div", { class: "dim", title: `${v.score} vs target ${v.threshold}${v.reused_from ? " · reused" : ""}` },
        el("div", { class: "dim-label" }, el("span", {}, DIM_LABEL[dim].slice(0, 4)), el("span", { class: "dim-score num" }, fmt(v.score))),
        el("div", { class: `bar ${st}` }, el("span", { style: `width:${Math.max(2, v.score)}%` })));
    });
    return el("tr", { class: "clickable", onclick: () => openDataset(d.dataset_id) },
      el("td", {}, el("span", { class: "ds" }, d.dataset_id), el("div", { class: "muted", style: "font-size:11.5px" },
        `${fmtInt(d.evaluated)} checks · ${fmtInt(d.violations)} violations`)),
      el("td", {}, el("span", { class: "num", style: "font-weight:600" }, fmt(d.overall)), " ", pill(scoreState(d.overall, medianThreshold(d)), `${fmt(d.overall)}`)),
      el("td", {}, el("div", { class: "dims" }, dims)),
      el("td", {}, d.severity ? el("span", { class: "tag" }, d.severity) : ""));
  });

  const table = el("table", { class: "score-table" },
    el("thead", {}, el("tr", {},
      el("th", {}, "Dataset"), el("th", {}, "Overall"), el("th", {}, "Dimensions (target line)"), el("th", {}, "Worst severity"))),
    el("tbody", {}, rows));

  /* run integrity strip */
  const steps = runInfo.steps || [];
  const interrupted = steps.filter((s) => s.status === "interrupted" || s.status === "failed");
  const strip = interrupted.length
    ? el("div", { class: "notice warn" },
        `This run is ${runInfo.status}: step “${interrupted[0].name}” did not complete. `,
        el("button", { class: "btn small ghost", onclick: () => switchView("runs") }, "Open recovery →"))
    : el("div", { class: "notice" },
        `Run ${run.slice(4, 16)} · ${runMeta.kind || "full"} · rule set ${(sc.run && sc.run.rule_set_id || "").slice(3, 11)} · finished ${(runMeta.finished_at || "").replace("T", " ")}`);

  root.replaceChildren(
    el("div", { class: "card" }, el("div", { class: "card-head" },
      el("div", {}, el("h2", {}, "Quality scorecard"), el("p", { class: "sub" }, "Scores are pass rates per dimension, severity-weighted. Click a dataset to expand to the original facts."))),
      hero),
    el("div", { class: "card" }, table),
    el("div", { class: "card" }, strip,
      el("div", { class: "card-head", style: "margin-top:14px;margin-bottom:8px" },
        el("div", {}, el("h3", {}, "What is driving the loss"), el("p", { class: "sub" }, "Every point lost maps to a rule and a record. These are the dimensions furthest below target."))),
      worstList(worstDims)),
    el("div", { class: "card" }, reportSummary(state.impacts)),
  );
}

function medianThreshold(d) {
  const vals = Object.values(d.dimensions || {}).map((v) => v.threshold).filter((v) => v !== null && v !== undefined);
  if (!vals.length) return null;
  return vals.reduce((a, b) => a + b, 0) / vals.length;
}

function statTile(label, value, note, kind) {
  return el("div", { class: "card stat" },
    el("div", { class: "label" }, label),
    el("div", { class: "value" }, value),
    el("div", { class: "note" }, note));
}

function worstList(items) {
  if (!items.length) return el("p", { class: "empty" }, "No dimension is below target.");
  return el("div", { class: "table-wrap" }, el("table", { class: "table" },
    el("thead", {}, el("tr", {}, el("th", {}, "Dataset"), el("th", {}, "Dimension"), el("th", { class: "right" }, "Score"), el("th", { class: "right" }, "Target"), el("th", { class: "right" }, "Violations"), el("th", {}, "State"))),
    el("tbody", {}, items.slice(0, 10).map((r) => el("tr", { class: "clickable", onclick: () => openDataset(r.ds) },
      el("td", { class: "mono" }, r.ds),
      el("td", {}, DIM_LABEL[r.dim]),
      el("td", { class: "right num" }, fmt(r.score)),
      el("td", { class: "right num muted" }, fmt(r.threshold)),
      el("td", { class: "right num" }, fmtInt(r.violations)),
      el("td", {}, pill(r.state === "red" ? "critical" : "warning", r.state === "red" ? "Below target" : "At risk")))))));
}

function reportSummary(arts) {
  if (!arts.length) return el("p", { class: "empty" }, "No report artifacts for this run.");
  const rows = arts.map((a) => el("tr", { class: "clickable", onclick: () => openReport(a.report_id) },
    el("td", {}, el("div", { style: "font-weight:600" }, a.report_name), el("div", { class: "mono muted" }, a.report_id)),
    el("td", {}, impactPill(a)),
    el("td", { class: "right num" }, fmtInt(a.material_rows)),
    el("td", { class: "right num muted" }, fmtInt(a.traced_rows)),
    el("td", { class: "right num" }, money(a.cost_usd)),
    el("td", {}, a.blind_spots && a.blind_spots.length ? el("span", { class: "tag" }, `${a.blind_spots.length} blind spot${a.blind_spots.length > 1 ? "s" : ""}`) : el("span", { class: "muted" }, "—"))));
  return el("div", {},
    el("div", { class: "card-head" },
      el("div", {}, el("h3", {}, "Downstream reports"), el("p", { class: "sub" }, "Material rows are measured by tracing each violating record. Cost is a partition-scan estimate — the two are never mixed."))),
    el("div", { class: "table-wrap" }, el("table", { class: "table" },
      el("thead", {}, el("tr", {}, el("th", {}, "Report"), el("th", {}, "Impact"), el("th", { class: "right" }, "Affected rows"), el("th", { class: "right" }, "Traced rows"), el("th", { class: "right" }, "Recompute cost (est.)"), el("th", {}, ""))),
      el("tbody", {}, rows))));
}

/* Impact pills use the same colour+icon+label rule as scores, but the glyph
   encodes *how well we know* (solid → approximate → open → unknown), because
   that is the distinction the report page is about. */
const IMPACT_ICON = { measured: "\u25cf", estimated: "~", traced_only: "\u25cb", unknown: "?", clear: "\u2713" };
function impactPill(a) {
  const map = {
    measured: ["critical", "Measured"],
    estimated: ["warning", "Estimated"],
    traced_only: ["serious", "Touched only"],
    unknown: ["unknown", "Unknown"],
    clear: ["good", "Clear"],
  };
  const [kind, label] = map[a.impact_status] || ["neutral", a.impact_status];
  return el("span", { class: `pill ${kind}` },
    el("span", { class: "dot", "aria-hidden": "true" }, IMPACT_ICON[a.impact_status] || "\u2022"), label);
}

/* ------------------------------------------------------- dataset drawer */
async function openDataset(datasetId) {
  const run = state.scorecardRun;
  openDrawer({ kicker: "Dataset", title: datasetId, body: [el("p", { class: "empty" }, "Loading…")] });
  try {
    const d = await api(`/api/datasets/${encodeURIComponent(datasetId)}?run=${encodeURIComponent(run)}`);
    const body = [];
    body.push(el("section", {},
      el("dl", { class: "kv" },
        el("dt", {}, "Layer"), el("dd", {}, d.dataset.layer),
        el("dt", {}, "Description"), el("dd", {}, d.dataset.description || "—"),
        el("dt", {}, "Run"), el("dd", { class: "mono" }, d.run_id))));

    const overall = d.scores.find((x) => x.dimension === "overall");
    body.push(el("section", {},
      el("div", { class: "grid cols-2" },
        el("div", { class: "card", style: "box-shadow:none" },
          el("div", { class: "eyebrow" }, "Overall score"),
          el("div", { style: "font-size:34px;font-weight:660;letter-spacing:-0.02em" }, fmt(overall ? overall.score : null)),
          el("div", { class: "sub" }, `${fmtInt(overall ? overall.violations : 0)} violations of ${fmtInt(overall ? overall.evaluated : 0)} checks`)),
        el("div", { class: "card", style: "box-shadow:none" },
          el("div", { class: "eyebrow" }, "Dimension breakdown"),
          el("div", { class: "dims", style: "margin-top:6px" }, DIMS.map((dim) => {
            const v = (d.scores || []).find((x) => x.dimension === dim);
            if (!v) return el("div", { class: "dim uncovered" },
              el("div", { class: "dim-label" }, el("span", {}, DIM_LABEL[dim]), el("span", { class: "dim-score" }, "n/a")));
            return el("div", { class: "dim" },
              el("div", { class: "dim-label" }, el("span", {}, DIM_LABEL[dim]), el("span", { class: "dim-score num" }, fmt(v.score))),
              el("div", { class: `bar ${scoreState(v.score, v.threshold)}` }, el("span", { style: `width:${Math.max(2, v.score)}%` })),
              el("div", { class: "muted", style: "font-size:11px" }, `target ${fmt(v.threshold)} · ${fmtInt(v.violations)} viol.`));
          }))))));

    body.push(el("section", {},
      el("h3", {}, "Scores"),
      el("div", { class: "table-wrap" }, el("table", { class: "table" },
        el("thead", {}, el("tr", {}, el("th", {}, "Dimension"), el("th", { class: "right" }, "Score"), el("th", { class: "right" }, "Target"), el("th", { class: "right" }, "Evaluated"), el("th", { class: "right" }, "Violations"), el("th", {}, "Source"))),
        el("tbody", {}, d.scores.map((s) => el("tr", {},
          el("td", {}, DIM_LABEL[s.dimension] || s.dimension),
          el("td", { class: "right num" }, fmt(s.score)),
          el("td", { class: "right num muted" }, fmt(s.threshold)),
          el("td", { class: "right num" }, fmtInt(s.evaluated)),
          el("td", { class: "right num" }, fmtInt(s.violations)),
          el("td", {}, s.reused_from ? el("span", { class: "tag" }, "reused " + s.reused_from.slice(4, 10)) : el("span", { class: "muted" }, "fresh")))))))));

    body.push(el("section", {},
      el("h3", {}, `Rules (${d.rules.length})`),
      el("div", { class: "table-wrap" }, el("table", { class: "table" },
        el("thead", {}, el("tr", {}, el("th", {}, "Rule"), el("th", {}, "Dimension"), el("th", {}, "Materiality"), el("th", { class: "right" }, "Checks"), el("th", { class: "right" }, "Violations"))),
        el("tbody", {}, d.rules.map((r) => el("tr", {},
          el("td", {}, el("div", { style: "font-weight:600" }, r.name || r.rule_id), el("div", { class: "mono muted" }, r.rule_id), r.description ? el("div", { class: "sub" }, r.description) : null),
          el("td", {}, DIM_LABEL[r.dimension] || r.dimension),
          el("td", {}, el("span", { class: "tag" }, r.materiality || "—")),
          el("td", { class: "right num" }, fmtInt(r.evaluated)),
          el("td", { class: "right num" }, fmtInt(r.violations)))))))));

    body.push(el("section", {},
      el("h3", {}, `Violations → original facts (${d.violations.length})`),
      el("p", { class: "sub", style: "margin-bottom:8px" }, "Each row is expandable to the source record and the downstream nodes it reaches. This is how a score is traced back to a fact, not just a red light."),
      d.violations.length ? el("div", { class: "table-wrap" }, el("table", { class: "table" },
        el("thead", {}, el("tr", {}, el("th", {}, "Record"), el("th", {}, "Rule"), el("th", {}, "Actual → expected"), el("th", {}, "Downstream"))),
        el("tbody", {}, d.violations.flatMap(violationRow)))) : el("p", { class: "empty" }, "No violations.")));

    const down = await api("/api/lineage");
    $("#drawer-body").replaceChildren(...body);
  } catch (err) {
    $("#drawer-body").replaceChildren(el("div", { class: "notice warn" }, err.message));
  }
}

function violationRow(v) {
  const facts = el("div", {});
  if (v.root_fact && v.root_fact.payload) {
    facts.append(el("div", { class: "eyebrow", style: "margin:6px 0 3px" }, `source · ${v.root_fact.source_id}/${v.root_fact.record_id}`));
    facts.append(el("pre", { class: "mono", style: "white-space:pre-wrap;margin:0;background:var(--page);padding:8px;border-radius:6px;border:1px solid var(--line)" }, JSON.stringify(v.root_fact.payload, null, 2)));
  } else {
    facts.append(el("div", { class: "muted" }, "Source record not traceable (derived row)."));
  }
  if (v.downstream && v.downstream.length) {
    facts.append(el("div", { class: "eyebrow", style: "margin:10px 0 3px" }, "downstream"));
    facts.append(el("div", { class: "dims" }, v.downstream.map((h) => el("span", { class: "tag" },
      `${h.impacted_node}${h.node_type === "report" ? " (report)" : ""} · d${h.depth}${h.material ? "" : " · touched only"}${h.status === "estimated" ? " · est" : ""}`))));
  }
  if (v.root_fact && v.root_fact.source_id && v.field) {
    const src = v.root_fact.source_id, rec = v.root_fact.record_id;
    const input = el("input", { type: "text", placeholder: "repaired value for " + v.field, style: "min-width:220px" });
    const go = el("button", { class: "btn small", onclick: () => repairRecord(src, rec, v.field, input.value, go) },
      "Repair upstream & recompute affected");
    facts.append(el("div", { class: "eyebrow", style: "margin:12px 0 3px" }, "fix the data, not the score"));
    facts.append(el("div", { class: "form-row" }, input, go,
      el("span", { class: "sub", style: "max-width:280px" }, "Writes the value to " + src + "/" + rec +
        " and re-runs only this dataset's downstream closure. Everything else is carried over from this run.")));
  }
  const detailRow = el("tr", { hidden: true }, el("td", { colspan: "4" }, facts));
  const row = el("tr", { class: "clickable", onclick: () => { detailRow.hidden = !detailRow.hidden; } },
    el("td", { class: "mono" }, v.record_id),
    el("td", {}, el("div", { class: "mono", style: "font-size:11.5px" }, v.rule_id), v.field ? el("div", { class: "muted" }, v.field) : null),
    el("td", {}, el("span", { class: "mono" }, String(v.actual ?? "∅")), " → ", el("span", { class: "mono" }, String(v.expected ?? "∅"))),
    el("td", { class: "muted", style: "font-size:12px" }, v.detail || ""));
  return [row, detailRow];
}

async function repairRecord(sourceId, recordId, field, value, btn) {
  if (!value) { toast("Enter the repaired value first", true); return; }
  btn.disabled = true;
  try {
    const out = await api("/api/fix", { method: "POST", body: {
      source_id: sourceId, record_ids: [recordId], patches: [{ [field]: value }],
      note: `repaired ${field} on ${sourceId}/${recordId} from the drill-down`,
    } });
    toast(`Recomputed only: ${out.recomputed_datasets.join(", ")}`);
    await refreshState();
    state.runId = out.run_id; state.scorecardRun = out.run_id;
    $("#drawer").hidden = true;
    switchView("overview");
  } catch (e) {
    toast(e.message, true);
    btn.disabled = false;
  }
}

/* ================================================================== reports */
async function renderReports(root) {
  const run = state.runId;
  if (!run) return root.replaceChildren(el("div", { class: "card empty" }, "No run selected."));
  const data = await api(`/api/impacts?run=${encodeURIComponent(run)}`);
  const arts = data.reports || [];
  const count = (k) => String(arts.filter((a) => a.impact_status === k).length);
  const hero = el("div", { class: "grid cols-5" },
    statTile("Measured impact", count("measured"), "specific rows traced to the report", "critical"),
    statTile("Estimated only", count("estimated"), "partition evidence, no row count", "warning"),
    statTile("Touched only", count("traced_only"), "lineage reaches it, no broken field", "serious"),
    statTile("Unknown lineage", count("unknown"), "must not read as “no impact”", "neutral"),
    statTile("Total recompute cost", "$" + arts.reduce((a, r) => a + (r.cost_usd || 0), 0).toFixed(3), "partition-scan estimate", "neutral"));
  const legend = el("div", { class: "legend", style: "margin-top:12px" },
    el("span", {}, impactPill({ impact_status: "measured" }), " at least one broken field on a specific traced row"),
    el("span", {}, impactPill({ impact_status: "estimated" }), " only partition-level evidence, no row key"),
    el("span", {}, impactPill({ impact_status: "traced_only" }), " lineage reaches it but no reported field is broken"),
    el("span", {}, impactPill({ impact_status: "unknown" }), " a declared lineage gap hides the consumer"));

  const graph = await api("/api/lineage");
  const gaps = graph.gaps || [];

  const impactCard = el("div", { class: "card" },
    el("div", { class: "card-head" },
      el("div", {}, el("h2", {}, "Report impact"),
        el("p", { class: "sub" }, "Measured rows vs estimated cost are separate columns with separate provenance. A report we cannot see fully is marked Unknown, not Clear."))),
    hero, legend);

  const artsTable = el("div", { class: "card" },
    el("div", { class: "table-wrap" }, el("table", { class: "table" },
      el("thead", {}, el("tr", {},
        el("th", {}, "Report"), el("th", {}, "Impact"), el("th", {}, "Confidence"),
        el("th", { class: "right" }, "Material rows"), el("th", { class: "right" }, "Traced rows"),
        el("th", { class: "right" }, "Affected %"),
        el("th", { class: "right" }, "Cost rows (est.)"), el("th", { class: "right" }, "Cost $ (est.)"),
        el("th", { class: "right" }, "Seconds (est.)"), el("th", {}, "Blind spots"))),
      el("tbody", {}, arts.map((a) => el("tr", { class: "clickable", onclick: () => openReport(a.report_id) },
        el("td", {},
          el("div", { style: "font-weight:600" }, a.report_name),
          el("div", { class: "mono muted" }, a.report_id),
          a.report ? el("div", { class: "sub" }, `${a.report.criticality} · ${a.report.owner_team}`) : null),
        el("td", {}, impactPill(a)),
        el("td", {}, el("span", { class: "tag" }, a.confidence)),
        el("td", { class: "right num" }, fmtInt(a.material_rows)),
        el("td", { class: "right num muted" }, fmtInt(a.traced_rows)),
        el("td", { class: "right num" }, fmt(a.affected_pct, 2) + "%"),
        el("td", { class: "right num muted" }, fmtInt(a.cost_rows)),
        el("td", { class: "right num" }, money(a.cost_usd)),
        el("td", { class: "right num muted" }, fmt(a.cost_seconds, 3)),
        el("td", {}, a.blind_spots && a.blind_spots.length
          ? el("span", { class: "tag" }, a.blind_spots.map((g) => g.reason).join(", "))
          : el("span", { class: "muted" }, "—"))))))));

  const gapsCard = el("div", { class: "card" },
    el("div", { class: "card-head" },
      el("div", {}, el("h3", {}, "Unknown lineage is not \u201cno impact\u201d"),
        el("p", { class: "sub" }, "These consumers are declared as gaps in the registry. Reports that depend on them stay Unknown even when every traced rule passes."))),
    el("div", { class: "table-wrap" }, el("table", { class: "table" },
      el("thead", {}, el("tr", {}, el("th", {}, "Dataset"), el("th", {}, "Direction"), el("th", {}, "Reason"), el("th", {}, "Severity"), el("th", {}, "Note"))),
      el("tbody", {}, gaps.map((g) => el("tr", {},
        el("td", { class: "mono" }, g.dataset_id),
        el("td", {}, g.direction),
        el("td", {}, el("span", { class: "tag" }, g.reason)),
        el("td", {}, el("span", { class: "tag" }, g.severity)),
        el("td", { class: "muted" }, g.note)))))));

  root.replaceChildren(impactCard, artsTable, gapsCard);
}

async function openReport(reportId) {
  const run = state.runId;
  openDrawer({ kicker: "Report", title: reportId, body: [el("p", { class: "empty" }, "Loading…")] });
  try {
    const d = await api(`/api/impacts/${encodeURIComponent(reportId)}?run=${encodeURIComponent(run)}`);
    const a = d.artifact;
    const body = [];
    body.push(el("section", {},
      el("div", { class: "form-row", style: "margin-bottom:10px" }, impactPill(a), el("span", { class: "tag" }, "confidence: " + a.confidence)),
      el("dl", { class: "kv" },
        el("dt", {}, "Criticality"), el("dd", {}, d.report.criticality + " · " + d.report.owner_team),
        el("dt", {}, "Material rows"), el("dd", {}, `${fmtInt(a.material_rows)} (${fmt(a.affected_pct, 2)}% of base partition)`),
        el("dt", {}, "Traced rows"), el("dd", {}, `${fmtInt(a.traced_rows)} — lineage reaches these, but not through a broken field`),
        el("dt", {}, "Shortest path"), el("dd", {}, a.path_depth === null ? "—" : a.path_depth + " hop(s)"),
        el("dt", {}, "Recompute cost"), el("dd", {}, `${money(a.cost_usd)} · ${fmt(a.cost_seconds, 3)}s · ${fmtInt(a.cost_rows)} rows — `, el("em", {}, "estimated, not measured")),
        el("dt", {}, "Basis"), el("dd", { class: "muted" }, a.cost_basis))));

    body.push(el("section", {},
      el("h3", {}, "Upstream path"),
      d.upstream.length ? el("div", { class: "table-wrap" }, el("table", { class: "table" },
        el("thead", {}, el("tr", {}, el("th", {}, "Dataset"), el("th", {}, "Layer"), el("th", { class: "right" }, "Root records"), el("th", {}, "Material"), el("th", { class: "right" }, "Depth"))),
        el("tbody", {}, d.upstream.map((u) => el("tr", {},
          el("td", { class: "mono" }, u.dataset_id),
          el("td", {}, u.layer),
          el("td", { class: "right num" }, fmtInt(u.root_records)),
          el("td", {}, u.material ? pill("critical", "Affected") : el("span", { class: "muted" }, "touched only")),
          el("td", { class: "right num" }, u.depth === null ? "—" : u.depth)))))) : el("p", { class: "empty" }, "No upstream rows traced.")));

    if (a.drivers && a.drivers.length) {
      body.push(el("section", {},
        el("h3", {}, "Drivers"),
        el("div", { class: "dims" }, a.drivers.map((x) => el("span", { class: "tag" }, `${x.dataset} · ${x.rule_id} · ${x.violations} records`)))));
    }
    if (a.blind_spots && a.blind_spots.length) {
      body.push(el("section", {},
        el("h3", {}, "Blind spots (unknown consumers)"),
        el("div", { class: "notice unknown" }, a.blind_spots.map((g) => el("div", {}, `${g.dataset}: ${g.reason} — ${g.note}`)))));
    }
    if (d.estimates && d.estimates.length) {
      body.push(el("section", {},
        el("h3", {}, "Partition estimates"),
        el("div", { class: "table-wrap" }, el("table", { class: "table" },
          el("thead", {}, el("tr", {}, el("th", {}, "Dataset"), el("th", {}, "Method"), el("th", { class: "right" }, "Rows (est.)"), el("th", { class: "right" }, "Cost (est.)"))),
          el("tbody", {}, d.estimates.map((e) => el("tr", {},
            el("td", { class: "mono" }, e.dataset_id),
            el("td", {}, el("span", { class: "tag" }, e.method), el("div", { class: "sub" }, e.explanation)),
            el("td", { class: "right num" }, fmtInt(e.estimated_rows)),
            el("td", { class: "right num" }, money(e.estimated_cost_usd)))))))));
    }
    $("#drawer-body").replaceChildren(...body);
  } catch (err) {
    $("#drawer-body").replaceChildren(el("div", { class: "notice warn" }, err.message));
  }
}

/* ================================================================== rules */
async function renderRules(root) {
  const versions = (await api("/api/rules")).versions || [];
  const active = versions.find((v) => v.status === "active");
  const selected = versions.find((v) => v.id === state.rulesetId) || active;
  if (!selected) return root.replaceChildren(el("div", { class: "card empty" }, "No rule sets."));
  const rs = await api(`/api/rules/${encodeURIComponent(selected.id)}`);

  const weightInputs = {};
  const sliders = DIMS.map((dim) => {
    const input = el("input", { type: "range", min: "0", max: "0.6", step: "0.01", value: String(rs.weights[dim] ?? 0) });
    const out = el("span", { class: "num", style: "min-width:44px;text-align:right" }, fmt(rs.weights[dim] ?? 0, 2));
    input.addEventListener("input", () => { out.textContent = fmt(input.value, 2); });
    weightInputs[dim] = input;
    return el("div", { class: "rule-row", style: "grid-template-columns:130px 1fr 90px" },
      el("div", {}, el("div", { class: "rname" }, DIM_LABEL[dim]), el("div", { class: "rmeta" }, `${rs.rule_count_by_dimension[dim] || 0} rules`)),
      input, out);
  });

  const edits = {};
  const ruleRows = rs.rules.map((r) => {
    const sev = el("select", { class: "inline" }, ["low", "medium", "high"].map((s) => el("option", { value: s, selected: s === r.severity }, s)));
    const mat = el("select", { class: "inline" }, ["critical", "advisory"].map((s) => el("option", { value: s, selected: s === r.materiality }, s)));
    sev.addEventListener("change", () => reviseSeverity(r.rule_id, sev.value, mat.value));
    mat.addEventListener("change", () => reviseSeverity(r.rule_id, sev.value, mat.value));
    edits[r.rule_id] = { sev, mat };
    return el("tr", {},
      el("td", {}, el("div", { style: "font-weight:600" }, r.name), el("div", { class: "mono muted" }, r.rule_id), el("div", { class: "sub" }, r.dataset_id + (r.field ? " · " + r.field : ""))),
      el("td", {}, DIM_LABEL[r.dimension]),
      el("td", {}, sev),
      el("td", {}, mat),
      el("td", { class: "right num" }, fmt(rs.effective_weights[r.rule_id], 2)));
  });

  const history = await api(`/api/history/stg_customer?dimension=overall`);
  const hist = history.history || [];

  root.replaceChildren(
    el("div", { class: "card" },
      el("div", { class: "card-head" },
        el("div", {}, el("h2", {}, `Rule set v${rs.version}`), el("p", { class: "sub" }, `${rs.status} · created by ${rs.created_by} · ${rs.rules.length} rules${rs.parent_id ? " · derived from v" + (rs.version - 1) : ""}`)),
        el("span", { class: "tag" }, rs.id)),
      el("h3", { style: "margin-bottom:6px" }, "Dimension weights"),
      el("p", { class: "sub", style: "margin-bottom:8px" }, "Changing any weight creates a new version. Historic scores keep pointing at the version that produced them."),
      el("div", {}, sliders),
      el("div", { class: "form-row", style: "margin-top:14px" },
        el("button", { class: "btn", onclick: () => reviseWeights(weightInputs) }, "Create new version"),
        el("span", { class: "muted" }, "v" + rs.version + " → v" + (rs.version + 1)))),

    el("div", { class: "card" },
      el("div", { class: "card-head" }, el("div", {}, el("h3", {}, "Rules"), el("p", { class: "sub" }, "Materiality decides whether a violation counts as an affected report row or only as attention."))),
      el("div", { class: "table-wrap" }, el("table", { class: "table" },
        el("thead", {}, el("tr", {}, el("th", {}, "Rule"), el("th", {}, "Dimension"), el("th", {}, "Severity"), el("th", {}, "Materiality"), el("th", { class: "right" }, "Effective weight"))),
        el("tbody", {}, ruleRows)))),

    el("div", { class: "card" },
      el("h3", {}, "Score history · stg_customer"),
      el("p", { class: "sub", style: "margin-bottom:10px" }, "Each point is a run under a specific rule-set version. Prior versions are never overwritten."),
      hist.length ? historyChart(hist) : el("p", { class: "empty" }, "No history yet."),
      historyTable(hist)));
}

function reviseWeights(inputs) {
  const weights = {};
  for (const [dim, input] of Object.entries(inputs)) weights[dim] = Number(input.value);
  const parent = state.rulesetId;
  api("/api/rules/revise", { method: "POST", body: { parent_id: parent, weights, actor: "ui", notes: "weight change from UI" } })
    .then((out) => {
      toast(`Created rule set v${out.rule_set.version}`);
      return refreshState().then(() => switchView("rules"));
    })
    .catch((e) => toast(e.message, true));
}

function reviseSeverity(ruleId, severity, materiality) {
  const parent = (state.versions.find((v) => v.id === state.rulesetId) || {}).id;
  api("/api/rules/revise", { method: "POST", body: { parent_id: parent, rule_patches: { [ruleId]: { severity, materiality } }, actor: "ui" } })
    .then((out) => { toast("New version v" + out.rule_set.version); return refreshState().then(() => switchView("rules")); })
    .catch((e) => toast(e.message, true));
}

function historyChart(hist) {
  const W = 520, H = 90, pad = 8;
  const scores = hist.map((h) => h.score);
  const min = Math.min(...scores, 100) - 1, max = 100;
  const span = Math.max(1, max - min);
  const pts = hist.map((h, i) => {
    const x = pad + (i / Math.max(1, hist.length - 1)) * (W - pad * 2);
    const y = H - pad - ((h.score - min) / span) * (H - pad * 2);
    return [x, y];
  });
  const path = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("class", "spark");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "stg_customer overall score over runs");
  const axis = document.createElementNS(ns, "line");
  axis.setAttribute("x1", pad); axis.setAttribute("x2", W - pad);
  axis.setAttribute("y1", H - pad); axis.setAttribute("y2", H - pad);
  axis.setAttribute("stroke", "var(--line)");
  svg.append(axis);
  const p = document.createElementNS(ns, "path");
  p.setAttribute("d", path); p.setAttribute("fill", "none");
  p.setAttribute("stroke", "var(--s1)"); p.setAttribute("stroke-width", "2");
  svg.append(p);
  for (const [x, y] of pts) {
    const c = document.createElementNS(ns, "circle");
    c.setAttribute("cx", x); c.setAttribute("cy", y); c.setAttribute("r", "3.5");
    c.setAttribute("fill", "var(--s1)"); c.setAttribute("stroke", "var(--surface-1)"); c.setAttribute("stroke-width", "2");
    svg.append(c);
  }
  return el("div", {}, svg, el("div", { class: "legend" },
    el("span", {}, el("span", { class: "swatch", style: "background:var(--s1)" }), "overall score per run"),
    el("span", { class: "muted" }, `${hist.length} runs · ${hist[0].score.toFixed(1)} → ${hist[hist.length - 1].score.toFixed(1)}`)));
}

function historyTable(hist) {
  return el("div", { class: "table-wrap" }, el("table", { class: "table" },
    el("thead", {}, el("tr", {}, el("th", {}, "Run"), el("th", {}, "Rule version"), el("th", {}, "Kind"), el("th", { class: "right" }, "Score"), el("th", { class: "right" }, "Violations"), el("th", {}, "Computed"))),
    el("tbody", {}, hist.map((h) => el("tr", {},
      el("td", { class: "mono" }, h.run_id.slice(4, 16)),
      el("td", {}, "v" + h.rule_set_version + (h.rule_set_id === state.rulesetId ? " (active)" : "")),
      el("td", {}, el("span", { class: "tag" }, h.run_kind)),
      el("td", { class: "right num" }, fmt(h.score)),
      el("td", { class: "right num" }, fmtInt(h.violations)),
      el("td", { class: "muted" }, (h.computed_at || "").replace("T", " ").slice(0, 16)))))));
}

/* ================================================================== runs */
async function renderRuns(root) {
  await refreshState();
  const selected = state.runId;
  const detail = selected ? await api(`/api/runs/${encodeURIComponent(selected)}`) : null;
  const runs = state.runs;

  const runRows = runs.map((r) => el("tr", { class: "clickable" + (r.id === selected ? "" : ""), onclick: () => { state.runId = r.id; state.scorecardRun = r.id; switchView("runs"); } },
    el("td", { class: "mono" }, r.id.slice(4, 16)),
    el("td", {}, el("span", { class: "tag" }, r.kind)),
    el("td", {}, el("span", { class: "tag" }, r.status)),
    el("td", { class: "mono muted" }, r.rule_set_id.slice(3, 11)),
    el("td", { class: "muted" }, (r.started_at || "").replace("T", " ").slice(0, 16)),
    el("td", { class: "right" }, r.parent_run_id ? el("span", { class: "tag" }, "← " + r.parent_run_id.slice(4, 10)) : el("span", { class: "muted" }, "—"))));

  const stepRows = detail ? detail.steps.map((s) => {
    const cls = ["done", "reused", "interrupted", "failed", "pending", "running"].includes(s.status) ? s.status : "pending";
    const glyph = { done: "✓", reused: "↺", interrupted: "✕", failed: "✕", pending: "·", running: "…" }[cls];
    return el("div", { class: "step " + cls },
      el("span", { class: "glyph" }, glyph),
      el("span", { class: "name" }, s.name),
      el("span", { class: "tag" }, s.status),
      el("span", { class: "detail" }, s.detail || ""));
  }) : [];

  const actionCard = el("div", { class: "card" },
    el("div", { class: "card-head" }, el("div", {}, el("h3", {}, "Recovery drills"), el("p", { class: "sub" }, "Interrupt a run mid-flight and resume it. Completed steps are kept; the failed step is redone; no result is fabricated for steps that never ran."))),
    el("div", { class: "form-row" },
      el("label", { class: "field", style: "text-transform:none;letter-spacing:0;color:var(--ink-2)" }, el("span", {}, "Interrupt at step"),
        el("select", { id: "fault-step" }, ["transform", "evaluate", "trace", "score", "reports"].map((s) => el("option", { value: s }, s)))),
      el("button", { class: "btn danger", onclick: simulateInterrupt }, "Simulate crash"),
      detail && (detail.status === "interrupted" || detail.status === "failed")
        ? el("button", { class: "btn", onclick: () => resumeRun(detail.id) }, "Resume this run")
        : el("span", { class: "muted" }, "Only an interrupted run can be resumed.")),
    el("p", { class: "sub", style: "margin-top:10px;color:var(--ink-3)" }, "The web process is single-threaded per request; the “crash” raises inside the worker after the preceding steps have committed."));

  root.replaceChildren(
    el("div", { class: "card" },
      el("div", { class: "card-head" }, el("div", {}, el("h2", {}, "Runs"), el("p", { class: "sub" }, "Every run is immutable history. Targeted re-runs point back at the run they derived from."))),
      el("div", { class: "table-wrap" }, el("table", { class: "table" },
        el("thead", {}, el("tr", {}, el("th", {}, "Run"), el("th", {}, "Kind"), el("th", {}, "Status"), el("th", {}, "Rules"), el("th", {}, "Started"), el("th", { class: "right" }, "Parent"))),
        el("tbody", {}, runRows)))),
    detail ? el("div", { class: "card" },
      el("div", { class: "card-head" }, el("div", {}, el("h3", {}, `Steps · ${detail.id.slice(4, 16)}`), el("p", { class: "sub" }, `status ${detail.status} · snapshot ${detail.snapshot_date} · ${detail.kind}`))),
      el("div", { class: "steps" }, stepRows)) : null,
    actionCard,
  );
}

async function simulateInterrupt() {
  const step = $("#fault-step").value;
  try {
    const out = await api("/api/simulate-interrupt", { method: "POST", body: { step } });
    toast(`Interrupted at ${step}`);
    await refreshState();
    state.runId = out.run.id; state.scorecardRun = out.run.id;
    switchView("runs");
  } catch (e) { toast(e.message, true); }
}

async function resumeRun(runId) {
  try {
    const out = await api("/api/runs/resume", { method: "POST", body: { run_id: runId } });
    toast("Resumed: " + out.status);
    await refreshState();
    switchView("runs");
  } catch (e) { toast(e.message, true); }
}

/* ================================================================== lineage */
async function renderLineage(root) {
  const graph = await api("/api/lineage");
  const layers = {};
  for (const d of graph.datasets) (layers[d.layer] = layers[d.layer] || []).push(d);
  const order = ["raw", "staging", "dim", "fct", "mart"];
  const cols = order.filter((l) => layers[l]).map((layer) => el("div", { class: "card layer-col" },
    el("div", { class: "layer-title" }, layer),
    layers[layer].map((d) => el("div", { class: "node", onclick: () => showLineageDetail(d.id) },
      el("div", { style: "font-weight:600" }, d.name), el("div", { class: "nid" }, d.id)))));
  cols.push(el("div", { class: "card layer-col" },
    el("div", { class: "layer-title" }, "reports"),
    graph.reports.map((r) => el("div", { class: "node report", onclick: () => { state.view = "reports"; openReport(r.id); } },
      el("div", { style: "font-weight:600" }, r.name), el("div", { class: "nid" }, r.id + " · " + r.criticality)))));

  const edgeRows = graph.edges.map((e) => el("tr", {},
    el("td", { class: "mono" }, e.parent), el("td", {}, "→"), el("td", { class: "mono" }, e.child),
    el("td", {}, el("span", { class: "tag" }, e.child_type)),
    el("td", {}, el("span", { class: "tag" }, e.propagation)),
    el("td", { class: "right num" }, fmt(e.impact_weight, 2)),
    el("td", { class: "muted" }, e.fields ? e.fields.join(", ") : "all fields")));

  root.replaceChildren(
    el("div", { class: "card" },
      el("div", { class: "card-head" }, el("div", {}, el("h2", {}, "Lineage"), el("p", { class: "sub" }, "Edges carry field lists and an impact weight. A field absent from an edge's list is traced but not material — that is how the same error hits two reports differently."))),
      el("div", { class: "lineage-graph" }, cols)),
    el("div", { class: "card" },
      el("div", { class: "card-head" }, el("h3", {}, "Edges")),
      el("div", { class: "table-wrap" }, el("table", { class: "table" },
        el("thead", {}, el("tr", {}, el("th", {}, "Parent"), el("th", {}, ""), el("th", {}, "Child"), el("th", {}, "Type"), el("th", {}, "Propagation"), el("th", { class: "right" }, "Weight"), el("th", {}, "Fields consumed"))),
        el("tbody", {}, edgeRows)))),
    el("div", { class: "card" },
      el("div", { class: "card-head" }, el("h3", {}, "Unknown lineage (declared gaps)"), el("p", { class: "sub" }, "A gap is data, not a hole to ignore: it forces “Unknown” on affected reports.")),
      el("div", { class: "table-wrap" }, el("table", { class: "table" },
        el("thead", {}, el("tr", {}, el("th", {}, "Dataset"), el("th", {}, "Direction"), el("th", {}, "Reason"), el("th", {}, "Severity"), el("th", {}, "Note"))),
        el("tbody", {}, graph.gaps.map((g) => el("tr", {},
          el("td", { class: "mono" }, g.dataset_id),
          el("td", {}, g.direction),
          el("td", {}, el("span", { class: "tag" }, g.reason)),
          el("td", {}, g.severity),
          el("td", { class: "muted" }, g.note))))))),
  );
}

async function showLineageDetail(datasetId) {
  openDrawer({ kicker: "Lineage", title: datasetId, body: [el("p", { class: "empty" }, "Loading…")] });
  try {
    const d = await api(`/api/lineage/${encodeURIComponent(datasetId)}`);
    const list = (arr) => arr.length ? el("div", { class: "table-wrap" }, el("table", { class: "table" },
      el("thead", {}, el("tr", {}, el("th", {}, "Node"), el("th", {}, "Depth"))),
      el("tbody", {}, arr.map((x) => el("tr", {},
        el("td", {}, el("span", { class: "mono" }, x.id), (x.node && x.node.criticality) ? el("span", { class: "tag", style: "margin-left:6px" }, x.node.criticality) : null),
        el("td", { class: "num" }, x.depth)))))) : el("p", { class: "empty" }, "None registered.");
    $("#drawer-body").replaceChildren(
      el("section", {}, el("h3", {}, "Upstream"), list(d.upstream)),
      el("section", {}, el("h3", {}, "Downstream"), list(d.downstream)),
      d.gaps && d.gaps.length ? el("section", {}, el("h3", {}, "Known gaps"), el("div", { class: "notice unknown" }, d.gaps.map((g) => el("div", {}, `${g.direction}: ${g.reason} — ${g.note}`)))) : null,
    );
  } catch (e) {
    $("#drawer-body").replaceChildren(el("div", { class: "notice warn" }, e.message));
  }
}

boot();
