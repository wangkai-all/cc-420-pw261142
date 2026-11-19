"""HTTP shim over the three services, plus the static web UI.

Kept deliberately small (stdlib only): the point of the exercise is the
lineage/scoring model, not a web framework.  Every mutating endpoint maps 1:1
to a ``pipeline.services`` method, so the CLI and the UI can never diverge.

Endpoints (all JSON unless noted):
  GET  /api/state                      dashboard: run, rule set, datasets
  GET  /api/scorecard?run=             per-dataset scores
  GET  /api/datasets/<id>?run=         drill: rules -> findings -> root facts
  GET  /api/impacts?run=               report impact + cost
  GET  /api/impacts/<report_id>?run=   one report, upstream path + estimates
  GET  /api/rules                      rule-set versions
  GET  /api/rules/<id>                 one version with its rules
  GET  /api/runs                        run list
  GET  /api/runs/<id>                   steps, scope, detail
  GET  /api/lineage[/<dataset>]         graph
  GET  /api/errors?run=&dataset=        violations
  GET  /api/history/<dataset>           score history across rule versions
  POST /api/rules/revise                weights / thresholds / severity patch
  POST /api/runs                        start a run (optionally rule set)
  POST /api/runs/<id>/resume            resume interrupted run
  POST /api/fix                         fix upstream records + targeted rerun
  POST /api/simulate-interrupt          crash drill
"""

from __future__ import annotations

import json
import mimetypes
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import db
from .services import ProcessorService, ReportService, RuleService
from . import runlog

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"


class Handler(BaseHTTPRequestHandler):
    db_path: Path = db.DEFAULT_DB
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------ plumbing
    def log_message(self, fmt, *args):  # quieter default
        pass

    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, exc: Exception, status: int = 400) -> None:
        self._json({"error": type(exc).__name__, "message": str(exc)}, status)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def _static(self, path: str) -> None:
        rel = path.lstrip("/") or "index.html"
        target = (WEB_ROOT / rel).resolve()
        if not str(target).startswith(str(WEB_ROOT.resolve())) or not target.is_file():
            target = WEB_ROOT / "index.html"
        if not target.is_file():
            self._error(FileNotFoundError(rel), 404)
            return
        data = target.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8" if ctype.startswith("text/") or "javascript" in ctype else ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ------------------------------------------------------------ routing
    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if not parts or parts[0] != "api":
            return self._static(url.path)
        try:
            with db.session(self.db_path, readonly=True) as conn:
                rules, proc, reports = RuleService(conn), ProcessorService(conn), ReportService(conn)
                self._get(parts[1:], q, rules, proc, reports)
        except KeyError as exc:
            self._error(exc, 404)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._error(exc, 500)

    def _get(self, parts, q, rules, proc, reports):
        run_id = q.get("run")
        if not parts:
            return self._json({"ok": True})
        head = parts[0]
        if head == "state":
            rid = reports.latest_run(include_interrupted=True)
            return self._json({
                "active_rule_set": rules.active(),
                "rule_sets": rules.list_rule_sets(),
                "runs": proc.runs(15),
                "latest_run": rid,
                "run": runlog.run_summary(reports.conn, rid) if rid else None,
                "scorecard_run": reports.latest_run(),
                "events": [dict(r) for r in reports.conn.execute(
                    "SELECT at,actor,action,subject,payload FROM events ORDER BY at DESC, rowid DESC LIMIT 12")],
            })
        if head == "scorecard":
            rid = run_id or reports.latest_run()
            if not rid:
                return self._json({"datasets": [], "run": None})
            return self._json(reports.scorecard(rid))
        if head == "datasets" and len(parts) > 1:
            rid = run_id or reports.latest_run(include_interrupted=True)
            return self._json(reports.dataset_detail(rid, parts[1]))
        if head == "impacts" and len(parts) > 1:
            rid = run_id or reports.latest_run(include_interrupted=True)
            return self._json(reports.report_detail(rid, parts[1]))
        if head == "impacts":
            rid = run_id or reports.latest_run(include_interrupted=True)
            return self._json({"run_id": rid, "reports": reports.impacts(rid) if rid else []})
        if head == "rules" and len(parts) > 1:
            return self._json(rules.get(parts[1]))
        if head == "rules":
            return self._json({"versions": rules.list_rule_sets()})
        if head == "runs" and len(parts) > 1 and parts[1] == "resume":
            return self._error(ValueError("resume is POST"), 405)
        if head == "runs" and len(parts) > 1:
            return self._json(runlog.run_summary(reports.conn, parts[1]))
        if head == "runs":
            return self._json({"runs": proc.runs(50)})
        if head == "lineage":
            return self._json(reports.lineage(parts[1] if len(parts) > 1 else None))
        if head == "errors":
            rid = run_id or reports.latest_run()
            return self._json({"findings": reports.errors(rid, dataset_id=q.get("dataset"), rule_id=q.get("rule"))})
        if head == "history" and len(parts) > 1:
            return self._json({"history": reports.score_history(parts[1], q.get("dimension", "overall"))})
        return self._error(KeyError(urlparse(self.path).path), 404)

    def do_POST(self):  # noqa: N802
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        try:
            body = self._body()
        except Exception as exc:  # noqa: BLE001
            return self._error(exc, 400)
        try:
            with db.session(self.db_path) as conn:
                rules, proc, reports = RuleService(conn), ProcessorService(conn), ReportService(conn)
                self._post(parts[1:], body, rules, proc, reports)
        except KeyError as exc:
            self._error(exc, 404)
        except ValueError as exc:
            self._error(exc, 409)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._error(exc, 500)

    def _post(self, parts, body, rules, proc, reports):
        if not parts:
            return self._error(KeyError("missing action"), 404)
        head = parts[0]
        if head == "rules" and len(parts) > 1 and parts[1] == "revise":
            parent = body.get("parent_id") or (rules.active() or {}).get("id")
            if not parent:
                raise ValueError("no active rule set")
            out = rules.revise(
                parent,
                actor=body.get("actor", "ui"),
                weights=body.get("weights"),
                thresholds=body.get("thresholds"),
                rule_patches=body.get("rule_patches"),
                added_rules=body.get("added_rules"),
                removed_rules=body.get("removed_rules"),
                notes=body.get("notes"),
            )
            return self._json({"rule_set": out})
        if head == "runs" and len(parts) > 1 and parts[1] == "resume":
            return self._json(proc.resume(body["run_id"], actor=body.get("actor", "ui")))
        if head == "runs":
            rid = proc.create_run(kind=body.get("kind", "full"), actor=body.get("actor", "ui"),
                                  rule_set_id=body.get("rule_set_id"),
                                  snapshot_date=body.get("snapshot_date"))
            return self._json(proc.execute(rid))
        if head == "fix":
            return self._json(proc.fix_and_rerun(
                source_id=body["source_id"], record_ids=body["record_ids"],
                patches=body.get("patches") or [{}] * len(body["record_ids"]),
                actor=body.get("actor", "ui"), note=body.get("note"),
            ))
        if head == "simulate-interrupt":
            rid = proc.simulate_interrupt(at_step=body.get("step", "evaluate"),
                                          snapshot_date=body.get("snapshot_date"))
            return self._json({"run": runlog.run_summary(proc.conn, rid)})
        return self._error(KeyError(urlparse(self.path).path), 404)


def serve(*, host: str = "127.0.0.1", port: int = 8787, db_path: Path | str = db.DEFAULT_DB) -> None:
    Handler.db_path = Path(db_path)
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"dataq web on http://{host}:{port}  (db: {db_path})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":  # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--db", default=str(db.DEFAULT_DB))
    a = ap.parse_args()
    serve(host=a.host, port=a.port, db_path=a.db)
