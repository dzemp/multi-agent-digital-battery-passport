"""Drive one series from what the passport app reports, one bounded agent task at a time.

The runner is a fixed graph, not an AI planner. It reads the application's state, calls the one agent whose input is
missing or stale, and stops at the first surprise. Agents propose; the application's gates decide; only a human
Reviewer resolves a hold. Every act writes an evidence register (JSON and Markdown) under output/runs.
"""

import hashlib
import json
import re
import subprocess
import time
from datetime import datetime, timezone

from data import metrics, signed_messages, simulator

from . import ROOT

BANNER = "Synthetic data - not a legal passport"
OUTPUT = ROOT / "output" / "runs"
ROLE3 = {"supplier": "sup", "carbon": "car", "issuance": "iss", "lifecycle": "lif"}
PASS, FAIL, STOPPED = "PASS", "FAIL", "STOPPED"
DEFAULT_REASON = ("Human reviewer compared the corrected supplier claim with the transport record; "
                  "both now name Mine South.")
ACTIONS = {"request_review": "review_requested", "no_issue": "no_issue", "calculate": "calculated",
           "publish": "published", "recommend": "recommended", "defer": "deferred",
           "escalate_compliance": "compliance_escalated",
           **{kind: "recorded" for kind in ("record_soh", "record_ownership_change", "request_repurpose",
                                            "record_end_of_life", "record_recycling_closure")}}
KEEP_HOLD_REASON = "Compliance keeps the hold until an independent site audit answers the adverse report."
ACCEPT_REASON = ("The site audit found no minors on site and partly confirmed unsafe shafts; supply continues under "
                 "the corrective action plan.")
ACTION_OWNER, ACTION_DUE = "Proseware Cobalt supplier quality lead", "2027-01-31"
PUBLIC_GROUPS = ["identity", "footprint"]
# Runners whose text is not AI-generated (app/passport/provenance.py); the app's own mark wins when it is returned.
SCRIPTED_HOSTING = ("local_scripted_double", "local_test_double")
# The field groups each viewer should see (app/passport/access.py); the run checks the app against this list.
VIEW_GROUPS = {"public": PUBLIC_GROUPS, "OEM": PUBLIC_GROUPS + ["performance", "second_life", "warranty", "recall"],
               "Recycler": PUBLIC_GROUPS + ["performance", "composition"],
               "Authority": PUBLIC_GROUPS + ["performance", "composition", "calculation", "supply_chain",
                                            "due_diligence", "ledger", "decisions"]}


class Stop(Exception):
    def __init__(self, verdict, message):
        super().__init__(message)
        self.verdict, self.message = verdict, message


class NoAnswer(Exception):
    """Raised by an HTTP client when the app could not be reached at all."""


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def step_limit(series):
    """Agent calls one act may make: one Supplier call per critical lot, one Carbon and one Issuance call per battery,
    plus two spare."""
    return len(series.lots) + 2 + 2 * len(series.batteries)


def outcome(status, body):
    """A short name for an agent answer, such as published, review_requested or an error code."""
    if not isinstance(body, dict):
        return f"http_{status}"
    if status >= 400:
        return body.get("error") or f"http_{status}"
    result = body.get("outcome") if isinstance(body.get("outcome"), dict) else {}
    if result.get("status") in ("deferred", "already_recorded", "awaiting_legal_review", "quarantined"):
        return result["status"]
    action = (body.get("decision") or {}).get("action")
    if action == "publish" and result.get("committed_now") is False:
        return "already_published"
    return ACTIONS.get(action, action or "unknown")


def body_of(response):
    try:
        body = response.json()
    except ValueError:
        body = {"message": (response.text or "")[:300]}
    return body if isinstance(body, dict) else {"value": body}


def problem(body):
    detail = body.get("detail")
    if isinstance(detail, dict):
        detail = detail.get("error") or detail.get("message")
    return body.get("error") or detail or body.get("message") or "no detail"


def call(http, method, path, what, **kwargs):
    """One request; an app that does not answer stops the act, because rerunning the same command is safe."""
    try:
        return http.request(method, path, **kwargs)
    except NoAnswer as error:
        raise Stop(STOPPED, f"{what}: the app did not answer ({error}). Rerun the same command.") from None


def read(http, path, what):
    response = call(http, "GET", path, what)
    body = body_of(response)
    if response.status_code >= 400:
        raise Stop(FAIL, f"{what} failed with HTTP {response.status_code}: {problem(body)}")
    return body


def task_id(series, role, subject, digest, extra=""):
    """Same observed state -> same task (an interrupted call replays); any new result -> a fresh task."""
    value = hashlib.sha256(json.dumps([series.name, role, subject, digest, extra]).encode()).hexdigest()[:20]
    return f"sc-{'chk' if extra.startswith('check') else ROLE3[role]}-{value}"


# ---- what the application reports ------------------------------------------------------------------------------
class View:
    def __init__(self, series, runs, reviews, evidence, batteries, cases=()):
        self.series = series
        self.tasks = sorted((t for t in runs["tasks"] if t["subject"] in series.subjects),
                            key=lambda t: (t.get("created_utc") or "", t["task_id"]))
        ids = {t["task_id"] for t in self.tasks}
        self.results = {r["task_id"]: r for r in runs["results"] if r.get("task_id") in ids}
        self.facts = sorted((f for f in runs["facts"] if f["subject"] in series.subjects),
                            key=lambda f: (f.get("recorded_utc") or "", f["fact_id"]))
        self.reviews = [r for r in reviews if r["lot_id"] in series.lots]
        self.evidence = [e for e in evidence if e["lot_id"] in series.lots]
        self.cases = [c for c in cases if c["lot_id"] in series.lots]
        self.batteries = batteries

    def state(self, battery):
        body = self.batteries.get(battery)
        return body["state"] if body else None

    def committed(self, battery):
        state = self.state(battery)
        return bool(state and state.get("committed_version"))

    def committed_count(self):
        return sum(self.committed(b) for b in self.series.batteries)

    def blocked(self, battery):
        """The data pipeline blocked the battery before any agent (its factory inputs are incomplete)."""
        return (self.state(battery) or {}).get("eligibility") == "blocked"

    def lots_of(self, battery):
        """The battery's critical lots: the lots the Supplier agent checks, once per lot for the whole series."""
        return [lot for lot in (self.state(battery) or {}).get("lot_ids", "").split(",") if lot in self.series.lots]

    def open_review(self, lot):
        return next((r for r in self.reviews if r["lot_id"] == lot and r["status"] == "open"), None)

    def case(self, lot):
        return next((c for c in reversed(self.cases) if c["lot_id"] == lot), None)

    def open_case(self, lot):
        case = self.case(lot)
        return case if case and case["status"] == "open" else None

    def lot_evidence(self, lot):
        return sorted((e for e in self.evidence if e["lot_id"] == lot),
                      key=lambda e: (e["uploaded_utc"], e["evidence_id"]))

    def undecidable(self):
        """Lots of unpublished batteries whose open review no Reviewer can resolve yet: a decision must cite a
        document that was not on file when the review opened, and none has arrived, so the app's hold page offers no
        decision. Reviews a varied series leaves open on purpose are not counted."""
        lots = []
        for lot in dict.fromkeys(lot for b in self.series.batteries if not self.committed(b)
                                 for lot in self.lots_of(b)):
            if lot in self.series.open_lots:
                continue
            review = self.open_review(lot)
            if review is not None:
                opening = set(filter(None, (review.get("opening_evidence") or "").split(",")))
                if all(e["evidence_id"] in opening for e in self.lot_evidence(lot)):
                    lots.append(lot)
        return lots

    def unblock(self, lots):
        """What makes the undecidable reviews of these lots decidable. Automatic checks after deployment can open the
        held lot's review without prepare, which sends the supplier's corrected claim; any other review needs a
        document a Reviewer uploads."""
        held, steps = self.series.lots[0], []
        if held in lots and not any(e["kind"] == "corrected_claim" for e in self.lot_evidence(held)):
            steps.append(f"run prepare: it sends the supplier's corrected claim for {held}, which the Reviewer needs "
                         "before deciding")
            lots = [lot for lot in lots if lot != held]
        if lots:
            steps.append(f"as a Reviewer, upload a document for {', '.join(lots)} under Upload evidence on the "
                         "operations console, then resolve the hold with it: every document on file was cited when "
                         "the hold opened")
        return "; and ".join(steps)

    def supplier_fact(self, lot):
        facts = [f for f in self.facts if f["kind"] == "supplier" and f["subject"] == lot]
        return facts[-1] if facts else None

    def supplier_current(self, lot):
        """Current means the latest Supplier assessment is newer than every evidence file for the lot."""
        fact, rows = self.supplier_fact(lot), self.lot_evidence(lot)
        return bool(fact and rows and fact["recorded_utc"] > max(e["uploaded_utc"] for e in rows))

    def lot_clear(self, lot):
        fact = self.supplier_fact(lot)
        return (self.open_review(lot) is None and self.open_case(lot) is None and self.supplier_current(lot)
                and json.loads(fact["data_json"]).get("action") == "no_issue")

    def carbon_current(self, battery):
        state = self.state(battery) or {}
        return any(f["kind"] == "carbon" and f["subject"] == battery
                   and json.loads(f["data_json"]).get("input_hash") == state.get("input_hash") for f in self.facts)

    def digest(self):
        value = [sorted(e["evidence_id"] for e in self.evidence),
                 sorted([r["review_id"], r["status"], r["row_version"]] for r in self.reviews),
                 sorted([c["case_id"], c["status"], c["row_version"]] for c in self.cases),
                 sorted(f["fact_id"] for f in self.facts), sorted(self.results),
                 [[b, (self.state(b) or {}).get("input_hash"), (self.state(b) or {}).get("committed_version")]
                  for b in self.series.batteries]]
        return hashlib.sha256(json.dumps(value).encode()).hexdigest()

    def task_outcome(self, task):
        body = self.results.get(task["task_id"])
        if body is None:
            return "no result yet"
        return outcome(400 if "error" in body else 200, body)

    def paths(self):
        """Per battery, the state-driven sequence of agent and human steps (autonomy evidence)."""
        result = {}
        for battery in self.series.batteries:
            subjects = set(self.lots_of(battery)) | {battery}
            events = [(t.get("created_utc") or "", f"{t['role']} {t['subject']}: {self.task_outcome(t)}")
                      for t in self.tasks if t["subject"] in subjects and not t["task_id"].startswith("sc-chk-")]
            for review in self.reviews:
                if review["lot_id"] in subjects and review.get("resolved_utc"):
                    events.append((review["resolved_utc"], f"human Reviewer resolved {review['review_id']}"))
            for case in self.cases:
                if case["lot_id"] in subjects and case.get("decided_utc"):
                    events.append((case["decided_utc"], f"human Compliance decided {case['case_id']}: "
                                                        f"{case['decision']}"))
            result[battery] = [text for _, text in sorted(events)]
        return result


def plan(view):
    """The fixed graph: the first missing or stale input decides the next agent."""
    series = view.series
    missing = [b for b in series.batteries if view.state(b) is None]
    if missing:
        shown = ", ".join(missing[:3]) + (f" and {len(missing) - 3} more" if len(missing) > 3 else "")
        return "stop", f"{shown} not imported yet; run prepare first"
    pending = [b for b in series.batteries if not view.committed(b) and not view.blocked(b)]
    if not pending:
        return "done", None
    lots = list(dict.fromkeys(lot for b in pending for lot in view.lots_of(b)))
    for lot in lots:
        if view.open_review(lot) is None and view.open_case(lot) is None and not view.supplier_current(lot):
            if not view.lot_evidence(lot):
                return "stop", f"{lot} has no evidence yet; run prepare first"
            return "supplier", lot
    for battery in pending:
        if not view.carbon_current(battery):
            return "carbon", battery
    for battery in pending:
        if all(view.lot_clear(lot) for lot in view.lots_of(battery)):
            return "issuance", battery
    waiting = [lot for lot in lots if view.open_review(lot) or view.open_case(lot)]
    if waiting:
        return "handoff", waiting
    return "stop", "no rule applies to the current state"


def battery_line(view, battery):
    state = view.state(battery)
    if state is None:
        return "Not imported", "-", "run prepare"
    lots = ", ".join(view.lots_of(battery))
    if view.committed(battery):
        life, version = state.get("lifecycle_status") or "active", str(state["committed_version"])
        if life in ("repurposed", "recycled"):
            return f"Closed - {life}", version, "closed; its earlier versions stay in the ledger"
        return "Published", version, {"second_life_pending": "second-life request awaiting a Reviewer",
                                      "end_of_life": f"end of life, held by {state.get('holder_id')}"}.get(
            life, "passport committed to the ledger")
    if state.get("eligibility") == "held":
        reviews = [view.open_review(lot)["review_id"] for lot in view.lots_of(battery) if view.open_review(lot)]
        cases = [view.open_case(lot)["case_id"] for lot in view.lots_of(battery) if view.open_case(lot)]
        if cases:
            return "Held", "-", f"compliance case open ({', '.join(cases)})"
        return "Held", "-", f"evidence under human review ({', '.join(reviews)})" if reviews else "held"
    if state.get("eligibility") == "blocked":
        return "Blocked", "-", "factory inputs incomplete"
    return "Not yet published", "-", f"awaiting agent checks for {lots}"


def describe(step, series):
    kind, value = step
    if kind == "handoff":
        lots = ", ".join(value or [])
        if value and set(value) <= set(series.open_lots):
            return (f"Nothing: {lots} stay open on purpose in this series (a Reviewer or Compliance Officer may "
                    "still decide them in the app)")
        return f"Human decision on {lots} (Reviewer: sourcing review; Compliance: case)"
    if kind == "done":
        if series.blocked:
            return (f"Nothing: all {len(series.batteries) - len(series.blocked)} publishable passports are "
                    f"published; the data pipeline blocked {len(series.blocked)}")
        return f"Nothing: all {len(series.batteries)} passports are published"
    return {"supplier": f"Supplier agent checks the evidence for {value}",
            "carbon": f"Carbon agent requests the illustrative calculation for {value}",
            "issuance": f"Issuance agent applies the publication gates for {value}",
            "stop": f"Stopped: {value}"}[kind]


# ---- evidence register -----------------------------------------------------------------------------------------
REDACTIONS = [
    (re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"), "[id-redacted]"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[account-redacted]"),
    (re.compile(r"\b[A-Za-z0-9.-]+\.(?:azurecontainerapps\.io|azurewebsites\.net|windows\.net|azure\.com)\b"),
     "[host-redacted]"),
]


def redact(value):
    if isinstance(value, str):
        for pattern, replacement in REDACTIONS:
            value = pattern.sub(replacement, value)
        return value
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    return value


def git_revision():
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                                text=True, timeout=15).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT,
                               capture_output=True, text=True, timeout=15).stdout.strip()
        return {"commit": commit or "unknown", "dirty": bool(dirty)}
    except (OSError, subprocess.SubprocessError):
        return {"commit": "unknown", "dirty": None}


class Register:
    """One record per act: what was asked, what the application decided, and the measured cost and time."""

    def __init__(self, series, act, mode, prices=None, out_dir=OUTPUT, clock=utc_now):
        started = clock()
        self.out_dir, self.prices = out_dir, prices
        self.run_id = f"{started.replace('-', '').replace(':', '')}-{series.name}-{act}"
        self.data = {"banner": BANNER, "run_id": self.run_id, "date_utc": started, **git_revision(),
                     "series": series.name, "series_id": series.series_id, "batteries": len(series.batteries),
                     "act": act,
                     "mode": "local run with a scripted double (not AI)" if mode == "local" else "tenant",
                     "evidence_level": "local" if mode == "local" else "tenant observed",
                     "app_version": None, "steps": [], "human": [], "checkpoints": [], "denials": [],
                     "paths": None, "cost": None, "verdict": None, "message": None}
        if series.varied:
            kinds = [series.issues[b] for b in series.issues]
            self.data["seeded_issues"] = {kind: kinds.count(kind) for kind in simulator.ISSUES if kind in kinds}
            self.data["accuracy"] = []

    def step(self, row):
        self.data["steps"].append(row)

    def deviations(self):
        return [a for a in self.data.get("accuracy") or [] if not a["match"]]

    def accuracy_summary(self):
        """How many agent answers were as expected and how many checkpoints differed, for a varied series."""
        if "accuracy" not in self.data:
            return None
        rows, failed = self.data["accuracy"], sum(not c["ok"] for c in self.data["checkpoints"])
        answers = f"Agent answers as expected: {len(rows) - len(self.deviations())} of {len(rows)}; " if rows else ""
        checkpoints = f"checkpoints that differed: {failed}."
        return answers + checkpoints if answers else checkpoints.capitalize()

    def checkpoint(self, name, expected, observed):
        ok = expected == observed
        self.data["checkpoints"].append({"name": name, "expected": expected, "observed": observed, "ok": ok})
        return ok

    def denial(self, name, expected, observed, exercised=True):
        result = "not exercised" if not exercised else ("PASS" if observed == expected else "FAIL")
        self.data["denials"].append({"name": name, "expected": expected, "observed": observed, "result": result})
        return result

    def cost(self):
        steps = [s for s in self.data["steps"] if s.get("role") in ROLE3]
        if not self.prices:
            return {"status": "not measured", "note": "Set prices_file in config/mvp.toml to price model tokens."}
        total, unpriced = 0.0, 0
        for step in steps:
            price = (self.prices.get("models") or {}).get(step.get("model") or "")
            if price is None or step.get("input_tokens") is None or step.get("output_tokens") is None:
                unpriced += 1
                continue
            step["model_cost"] = round(step["input_tokens"] / 1e6 * price["input_per_million"]
                                       + step["output_tokens"] / 1e6 * price["output_per_million"], 6)
            total += step["model_cost"]
        published = sum(1 for s in steps if s.get("outcome") == "published")
        return {"status": "partially measured" if unpriced else "measured", "currency": self.prices.get("currency"),
                "price_source": self.prices.get("source"), "model_token_cost": round(total, 6),
                "per_published_passport": round(total / published, 6) if published else None,
                "unpriced_agent_calls": unpriced,
                "scope": "model tokens only; excludes hosting, storage, SQL, Foundry and review labour"}

    def finish(self, verdict, message):
        self.data["verdict"], self.data["message"] = verdict, message
        self.data["cost"] = self.cost()
        return self.write()

    def markdown(self):
        d = self.data
        lines = [f"# Scenario evidence register: {d['act']} ({d['verdict']})", "", f"> {d['banner']}", "",
                 f"- Run: `{d['run_id']}`", f"- Date (UTC): {d['date_utc']}",
                 f"- Commit: `{d['commit']}`{' (uncommitted changes present)' if d['dirty'] else ''}",
                 f"- Series: {d['series']} ({d['series_id']}, {d['batteries']} batteries)", f"- Mode: {d['mode']}",
                 f"- Evidence level: **{d['evidence_level']}**", f"- App version: {d['app_version'] or 'unknown'}",
                 f"- Verdict: **{d['verdict']}** - {d['message']}", ""]
        if d.get("seeded_issues"):
            lines.insert(-2, "- Seeded data issues in the added batteries: "
                         + ", ".join(f"{kind} {count}" for kind, count in d["seeded_issues"].items()))
        if "accuracy" in d:
            rows = d["accuracy"]
            lines += ["## Expected vs actual agent outcomes", "",
                      (f"{len(rows) - len(self.deviations())} of {len(rows)} agent answers as expected. " if rows
                       else "No agent answers were compared in this act. ")
                      + "In a series with seeded data issues the run records a different answer and continues; it "
                      "never forces the expected one.", ""]
            if rows:
                lines += ["| Role | Calls | As expected | Different |", "|---|---|---|---|"]
                for role in dict.fromkeys(a["role"] for a in rows):
                    mine = [a for a in rows if a["role"] == role]
                    good = sum(a["match"] for a in mine)
                    lines.append(f"| {role} | {len(mine)} | {good} | {len(mine) - good} |")
                lines.append("")
            seeded = [a for a in rows if a["issue"] != "-"]
            if seeded:
                lines += ["### Subjects with a seeded issue", "",
                          "| Act | Role | Subject | Seeded issue | Expected | Actual | Match |",
                          "|---|---|---|---|---|---|---|"]
                lines += [f"| {a['act']} | {a['role']} | {a['subject']} | {a['issue']} | {a['expected']} | "
                          f"{a['actual']} | {'yes' if a['match'] else 'NO'} |" for a in seeded] + [""]
            if self.deviations():
                lines += ["### Different from expected", "", "| Act | Role | Subject | Seeded issue | Expected | "
                          "Actual |", "|---|---|---|---|---|---|"]
                lines += [f"| {a['act']} | {a['role']} | {a['subject']} | {a['issue']} | {a['expected']} | "
                          f"{a['actual']} |" for a in self.deviations()] + [""]
        if d["steps"]:
            lines += ["## Agent steps", "",
                      "Agent reasons marked *(AI-generated)* were written by a model; the provenance ID links each to "
                      "its record in the app (`POST /provenance/verify`). Application messages are the app's own "
                      "text.", "",
                      "| # | Role | Subject | Outcome | HTTP | Model (hosting) | Tokens in/out | "
                      "Latency ms | Wall ms | Tool reads | Cited evidence | Agent reason | Provenance ID | "
                      "App message |",
                      "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
            for n, s in enumerate(d["steps"], 1):
                reads = " > ".join(s.get("tool_reads") or []) or "-"
                reason = s.get("agent_reason") or ""
                if reason and s.get("agent_reason_ai"):
                    reason = f"*(AI-generated)* {reason}"
                lines.append(f"| {n} | {s['role']} | {s['subject']} | {s['outcome']} | {s['http_status']} | "
                             f"{s.get('model') or '-'} ({s.get('hosting') or '-'}) | "
                             f"{s.get('input_tokens') if s.get('input_tokens') is not None else '-'}/"
                             f"{s.get('output_tokens') if s.get('output_tokens') is not None else '-'} | "
                             f"{s.get('latency_ms') if s.get('latency_ms') is not None else '-'} | {s['wall_ms']} | "
                             f"{reads} | {', '.join(s.get('cited_evidence') or []) or '-'} | "
                             f"{(reason or '-').replace('|', '/')} | {s.get('provenance_id') or '-'} | "
                             f"{(s.get('app_message') or '-').replace('|', '/')} |")
            lines.append("")
        if d["human"]:
            lines += ["## Human decisions", ""] + [f"- {h['what']}: {h['detail']}" for h in d["human"]] + [""]
        if d["checkpoints"]:
            lines += ["## Checkpoints", "", "| Checkpoint | Expected | Observed | OK |", "|---|---|---|---|"]
            lines += [f"| {c['name']} | {c['expected']} | {c['observed']} | {'yes' if c['ok'] else 'NO'} |"
                      for c in d["checkpoints"]] + [""]
        if d["denials"]:
            lines += ["## Denied-access checks", "", "| Check | Expected | Observed | Result |", "|---|---|---|---|"]
            lines += [f"| {c['name']} | {c['expected']} | {c['observed']} | {c['result']} |" for c in d["denials"]]
            lines.append("")
        if d["paths"]:
            lines += ["## Decision path per battery (autonomy evidence)", ""]
            lines += [f"- **{b}:** {' -> '.join(p) or 'no steps yet'}" for b, p in d["paths"].items()] + [""]
        if d.get("lifecycle"):
            lines += ["## Lifecycle status", ""] + [f"- {b}: {s}" for b, s in d["lifecycle"].items()] + [""]
        if d.get("role_views"):
            lines += [f"## Role views of {d['role_views']['passport']}", "", "| Viewer | Field groups |", "|---|---|"]
            lines += [f"| {name} | {groups} |" for name, groups in d["role_views"]["views"].items()] + [""]
        for m in d.get("measures") or []:
            cover, foot, supply, second = (m["coverage"], m["verified_footprint"], m["supplier_completeness"],
                                           m["second_life"])
            lines += [f"## Success measures {m['when']} (synthetic; illustrative)", "",
                      f"- Passport coverage: {cover['published']} of {cover['produced']} produced batteries published",
                      f"- Verified footprint: {foot['verified']} of {foot['published']} published, average "
                      f"{foot['avg_kg_co2e_per_kwh']} kg CO2e/kWh",
                      f"- Supplier data completeness: {supply['complete']} of {supply['lots']} lots",
                      f"- Second life: {second['repurposed']} repurposed, {second['value_eur_illustrative']} EUR",
                      "", "| Plant | Produced | Published | Verified | kg CO2e/kWh |", "|---|---|---|---|---|"]
            lines += [f"| {p['plant_id']} | {p['produced']} | {p['published']} | {p['verified_footprint_batteries']} | "
                      f"{p['avg_footprint_per_kwh']} |" for p in m["plants"]] + [""]
        if d.get("workbook"):
            lines += ["## Executive workbook", "", f"- {d['workbook']}", ""]
        cost = d["cost"] or {}
        lines += ["## Cost", "", f"- Status: {cost.get('status')}"]
        if cost.get("status") != "not measured":
            lines += [f"- Model-token cost: {cost['model_token_cost']} {cost.get('currency') or ''}",
                      f"- Per published passport in this run: {cost['per_published_passport'] or 'none published'}",
                      f"- Prices: {cost.get('price_source') or 'not stated'}", f"- Scope: {cost['scope']}"]
        else:
            lines.append(f"- {cost.get('note')}")
        return "\n".join(lines) + "\n"

    def write(self):
        self.data = redact(self.data)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / f"{self.run_id}.json"
        path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        path.with_suffix(".md").write_text(self.markdown(), encoding="utf-8")
        return path


# ---- the acts --------------------------------------------------------------------------------------------------
class Runner:
    """Runs one act against one app. seed(runner) imports the series' package and opening evidence, or raises Stop;
    the tenant and the local run provide their own. Clients answer request(method, path, **kwargs) like httpx."""

    def __init__(self, http, series, register, *, seed=None, anonymous=None, confirm=input, sleep=time.sleep,
                 out=print, roleless=None, agent_http=None, double=None, reviewer_only=None, lifecycle_http=None,
                 viewers=None, workbook=None, app_url=""):
        self.http, self.series, self.register, self.seed = http, series, register, seed
        self.anonymous, self.confirm, self.sleep, self.out = anonymous, confirm, sleep, out
        self.roleless, self.agent_http, self.double = roleless, agent_http, double
        self.reviewer_only, self.lifecycle_http = reviewer_only, lifecycle_http
        self.viewers, self.workbook, self.app_url = viewers or {}, workbook, app_url.rstrip("/")
        # Lifecycle events this runner already recorded, by message ID, so the story does not send them twice.
        self.staged = {}

    # -- shared ----------------------------------------------------------------------------------------------
    def observe(self):
        c = self.series
        runs = read(self.http, "/agents/runs", "Reading agent runs")
        reviews = read(self.http, "/reviews", "Reading reviews")["reviews"]
        evidence = read(self.http, "/evidence", "Reading evidence")["evidence"]
        cases = read(self.http, "/compliance/cases", "Reading compliance cases")["cases"]
        batteries = {}
        for battery in c.batteries:
            path = f"/passports/{battery}/restricted"
            response = call(self.http, "GET", path, f"Reading {battery}")
            batteries[battery] = None if response.status_code == 404 else read(self.http, path, f"Reading {battery}")
        return View(c, runs, reviews, evidence, batteries, cases)

    def agent(self, role, subject, view, event=None, extra=""):
        task = task_id(self.series, role, subject, view.digest(), extra)
        data = {"task_id": task, "subject": subject, **({"event": event} if event is not None else {})}
        self.out(f"  {role.title()} agent -> {subject} (task {task}); a model call can take up to two minutes...")
        started = time.monotonic()
        try:
            response = self.http.request("POST", f"/agents/{role}/run", json=data)
        except NoAnswer as error:
            raise Stop(STOPPED, f"{role} {subject}: the app did not answer ({error}). Rerun the same command; the "
                                "same task replays.") from None
        body = body_of(response)
        name = outcome(response.status_code, body)
        decision, execution = body.get("decision") or {}, body.get("execution") or {}
        agent_reason = decision.get("reason") or ""
        mark = next((m for m in body.get("provenance") or [] if m.get("field") == "reason"), None) or {}
        app_message = body.get("message") or ("" if agent_reason else problem(body))
        self.register.step({
            "act": self.register.data["act"], "role": role, "subject": subject, "task_id": task,
            "http_status": response.status_code, "outcome": name, "action": decision.get("action"),
            "agent_reason": agent_reason[:200],
            "agent_reason_ai": bool(agent_reason) and mark.get("ai_generated",
                                                                execution.get("hosting") not in SCRIPTED_HOSTING),
            "provenance_id": mark.get("provenance_id"), "app_message": app_message[:200],
            "cited_evidence": decision.get("evidence_ids") or [],
            "model": execution.get("model"), "hosting": execution.get("hosting"),
            "agent_version": execution.get("agent_version"), "input_tokens": execution.get("input_tokens"),
            "output_tokens": execution.get("output_tokens"), "latency_ms": execution.get("latency_ms"),
            "wall_ms": round((time.monotonic() - started) * 1000),
            "failed_gates": (body.get("gates") or {}).get("reasons"), "tool_reads": None})
        said = (f"agent reason{' (AI-generated)' if mark.get('ai_generated') else ''}: {agent_reason[:160]}"
                if agent_reason else f"app message: {(app_message or '-')[:160]}")
        self.out(f"    {name} (HTTP {response.status_code}); {said}")
        return response.status_code, name, body

    def drive(self, expect, act_name, patient=False):
        """Call the next agent until the plan reaches a hand-off, the end or a stop. A patient drive runs beside the
        app's own workflow, which reassesses after each human decision: an answer voided because that workflow
        changed the inputs (409 stale_task) is retried from the new state."""
        attempted, last, limit = set(), {}, step_limit(self.series)
        for _ in range(limit):
            view = self.observe()
            step = plan(view)
            if step[0] in ("done", "handoff", "stop"):
                if step[0] == "stop" and attempted:
                    raise Stop(STOPPED, f"{act_name} made no progress: {step[1]}.")
                return view, step
            if step in attempted:
                note = last.get(step, "")
                raise Stop(STOPPED, f"no progress: the {step[0]} agent for {step[1]} did not change the state "
                                    f"({note}). Nothing was published by this step. Read its reason; rerun the act "
                                    "to retry with a fresh task.")
            attempted.add(step)
            status, name, body = self.agent(step[0], step[1], view)
            last[step] = f"last outcome {name}: {(body.get('decision') or {}).get('reason') or problem(body)}"[:300]
            if patient and status == 409 and name == "stale_task":
                attempted.discard(step)
                continue
            if status >= 400:
                gates = (body.get("gates") or {}).get("reasons")
                raise Stop(FAIL, f"{step[0]} {step[1]} returned HTTP {status} {name}"
                                 f"{'; failed gates: ' + '; '.join(gates) if gates else ''}.")
            if name == "deferred":
                continue
            allowed = expect(step[0], step[1])
            matched = bool(allowed) and name in allowed
            if self.series.varied:
                self.register.data["accuracy"].append({
                    "act": self.register.data["act"], "role": step[0], "subject": step[1],
                    "issue": self.series.issue_of(step[1]) or "-",
                    "expected": " or ".join(sorted(allowed)) if allowed else "no call", "actual": name,
                    "match": matched})
                if not matched:
                    self.out(f"    different from expected ({' or '.join(sorted(allowed or ['no call']))}); "
                             "recorded, and the run continues")
                continue
            if not matched:
                raise Stop(FAIL, f"expected {' or '.join(sorted(allowed or ['no call']))} from {step[0]} {step[1]}, "
                                 f"got {name}. Do not edit the ledger to force the story.")
        raise Stop(STOPPED, f"{act_name} exceeded {limit} agent calls.")

    def tool_reads(self):
        tasks = {s["task_id"] for s in self.register.data["steps"]}
        if not tasks:
            return
        try:
            events = read(self.http, "/audit?limit=500", "Reading the audit trail")["events"]
        except Stop:
            return
        reads = {}
        for event in events:
            if event.get("action") == "agent_tool_read" and event.get("subject") in tasks:
                reads.setdefault(event["subject"], []).append(json.loads(event["detail"]).get("section"))
        for step in self.register.data["steps"]:
            step["tool_reads"] = reads.get(step["task_id"], [])

    def execute(self, act, **options):
        """Run one act and always write its register. Returns PASS, FAIL or STOPPED."""
        try:
            self.register.data["app_version"] = read(self.http, "/health/live", "Reading the app version").get(
                "version")
            message = getattr(self, f"act_{act}")(**options)
            verdict = PASS
        except Stop as stop:
            verdict, message = stop.verdict, stop.message
        summary = self.register.accuracy_summary()
        if summary:
            message = f"{message} {summary}"
        self.tool_reads()
        path = self.register.finish(verdict, message)
        self.out(f"{verdict}: {message}")
        self.out(f"Evidence register: {path.with_suffix('.md')}")
        return verdict

    def checkpoint(self, view, name, committed, open_lots, case_lots=()):
        c = self.series
        observed = {"published": sorted(b for b in c.batteries if view.committed(b)),
                    "open_reviews": sorted(lot for lot in c.lots if view.open_review(lot)),
                    "open_cases": sorted(lot for lot in c.lots if view.open_case(lot))}
        expected = {"published": sorted(committed), "open_reviews": sorted(open_lots), "open_cases": sorted(case_lots)}
        if not self.register.checkpoint(name, expected, observed):
            if c.varied:
                self.out(f"  checkpoint '{name}' differed; recorded, and the run continues.")
                return
            raise Stop(FAIL, f"checkpoint '{name}' expected {expected}, observed {observed}.")

    # -- prepare ---------------------------------------------------------------------------------------------
    def act_prepare(self, patient=False):
        c = self.series
        total = len(c.batteries)
        self.out(f"Prepare for series {c.name}: {total} batteries and the opening evidence.")
        if self.seed is None:
            raise Stop(STOPPED, "this runner cannot import a package.")
        self.seed(self)
        view = self.observe()
        if all(view.committed(b) for b in c.final_published):
            raise Stop(STOPPED, f"series {c.name} is already complete ({view.committed_count()} of {total}). Use "
                                "status or aftercare, or a new --tag.")
        expected = {("supplier", lot): {"no_issue"} for lot in c.lots}
        expected.update({("supplier", lot): {"review_requested"} for lot in c.review_lots})
        expected.update({("supplier", lot): {"compliance_escalated"} for lot in c.case_lots})
        published = {"published", "already_published"} if patient else {"published"}
        view, step = self.drive(lambda role, subject: expected.get((role, subject)) or (
            published if role == "issuance" and subject in c.clean else
            {"calculated"} if role == "carbon" else None), "prepare", patient=patient)
        if step[0] != "handoff":
            if not c.varied:
                raise Stop(FAIL, f"expected a human hand-off after prepare, got: {describe(step, c)}.")
            self.register.checkpoint("a human hand-off after prepare", "handoff", step[0])
        if c.varied:
            name = (f"after prepare: {len(c.clean)} of {total} published, {len(c.blocked)} blocked by the pipeline, "
                    f"{', '.join(c.review_lots)} held for review, {', '.join(c.case_lots)} in a compliance case")
        else:
            name = (f"after prepare: {len(c.clean)} of {total} published, {c.lots[0]} held, {c.case_lot} in a "
                    "compliance case")
        self.checkpoint(view, name, c.clean, c.review_lots, c.case_lots)
        # The supplier's corrected claim arrives while the hold is open, so a Reviewer can choose it in the app.
        # An open review stops the Supplier agent from reassessing the lot, so this does not change the plan.
        for lot in c.corrected:
            self.out(f"  the supplier sends a corrected signed claim for {lot} through the connector:")
            self.send(c.correction(lot), f"Sending the corrected claim for {lot}")
        message = (f"{len(c.clean)} of {total} passports published. {', '.join(c.held)} are held: the supplier claim "
                   f"and the transport record name different mines. {', '.join(c.compliance_held)} is held: the "
                   f"Supplier agent escalated {c.case_lot} to Compliance after an adverse report.")
        if c.varied:
            message += (f" Seeded issues: {len(c.blocked)} batteries blocked by the data pipeline, "
                        f"{len(c.review_lots) - 1} more lots held for a sourcing review and {len(c.case_lots) - 1} "
                        "more in a compliance case.")
        return f"{message} Next: the human decisions (review, or the hold and case screens in the app)."

    def upload(self, item):
        what = f"Uploading {item.lot} {item.kind}"
        response = call(self.http, "POST", "/evidence", what,
                        data={"kind": item.kind, "lot_id": item.lot, "command_id": item.command_id},
                        files={"file": (item.name, item.data, "application/json")})
        body = body_of(response)
        if response.status_code >= 400:
            raise Stop(FAIL, f"{what} failed with HTTP {response.status_code}: {problem(body)}")
        self.out(f"  evidence {item.lot} {item.kind}: {body['evidence_id']}"
                 f"{' (already uploaded)' if body.get('replayed') else ''}")
        return body["evidence_id"]

    def send(self, envelope, what):
        """Deliver one signed partner message through the app's mock connector; a resend returns the first result."""
        response = call(self.http, "POST", "/connector/v1/messages", what, json={"envelope": envelope})
        body = body_of(response)
        if response.status_code >= 400:
            raise Stop(FAIL, f"{what} failed with HTTP {response.status_code}: {problem(body)}")
        self.out(f"  connector (mock) {body.get('message_id')}: {body.get('result')}"
                 f"{' (already received)' if body.get('replayed') else ''}")
        return body

    def connect(self):
        """Record the mock connector agreements, then send every lot's signed claim version 1."""
        c = self.series
        for agreement in c.agreements():
            what = f"Recording the connector agreement {agreement['agreement_id']}"
            response = call(self.http, "POST", "/connector/v1/agreements", what, json=agreement)
            if response.status_code >= 400:
                raise Stop(FAIL, f"{what} failed with HTTP {response.status_code}: {problem(body_of(response))}")
        verified = 0
        for envelope in c.claims():
            what = f"Sending the signed claim {envelope['payload']['message_id']}"
            response = call(self.http, "POST", "/connector/v1/messages", what, json={"envelope": envelope})
            body = body_of(response)
            if response.status_code >= 400 or body.get("result") != "verified":
                raise Stop(FAIL, f"{what} failed with HTTP {response.status_code}: {problem(body)}")
            verified += 1
        self.out(f"  connector (mock): {len(c.agreements())} agreements, {verified} signed claims verified")

    # -- human decision --------------------------------------------------------------------------------------
    def site_of(self, evidence_id):
        response = call(self.http, "GET", f"/evidence/{evidence_id}/content", f"Reading evidence {evidence_id}")
        if response.status_code >= 400:
            raise Stop(FAIL, f"reading evidence {evidence_id} failed with HTTP {response.status_code}.")
        data = json.loads(response.content)
        return data.get("origin_site") or data.get("loaded_at_site") or "-"

    def act_review(self, reason=DEFAULT_REASON, yes=False):
        reason = (reason or "").strip()
        if not 5 <= len(reason) <= 400:
            raise Stop(STOPPED, "give a reason of 5-400 characters.")
        c = self.series
        first = " ".join(self.resolve_review(lot, reason if lot == c.lots[0] else self.issue_reason(lot), yes)
                         for lot in c.corrected)
        denied, results = False, []
        for lot in c.case_lots:
            result, ran = self.decide_case(yes, lot, denials=not denied)
            results.append(result)
            denied = denied or ran
        second = " ".join(filter(None, results))
        if not self.register.data["human"]:
            return f"{first} {second} Next: complete."
        open_reviews = [lot for lot in c.open_lots if lot in c.review_lots]
        open_cases = [lot for lot in c.open_lots if lot in c.case_lots]
        name = f"after the human decisions: still {len(c.clean)} of {len(c.batteries)} (people do not publish)"
        if open_reviews or open_cases:
            name += f", {', '.join(open_reviews + open_cases)} left open on purpose"
        self.checkpoint(self.observe(), name, c.clean, open_reviews, open_cases)
        return f"{first} {second} Nothing was published. Next: complete."

    def issue_reason(self, lot):
        """The Reviewer's reason for a seeded mismatch lot whose supplier sent a corrected claim."""
        site = self.series.correction(lot)["payload"]["origin_site"]
        return (f"Human reviewer compared the corrected supplier claim with the transport record; both now name "
                f"{site}.")

    def resolve_review(self, lot, reason, yes):
        c = self.series
        view = self.observe()
        review = view.open_review(lot)
        if review is None:
            if any(r["lot_id"] == lot and r["status"] == "resolved" for r in view.reviews):
                self.out(f"The {lot} review is already resolved.")
                return f"The {lot} review is already resolved."
            if c.varied:
                self.out(f"{lot} has no open review; nothing to resolve (recorded in the checkpoints).")
                return f"{lot} had no open review."
            raise Stop(STOPPED, f"{lot} has no open review. Run prepare first.")
        correction = self.send(c.correction(lot), f"Sending the corrected claim for {lot}")["evidence_id"]
        self.out(f"Open review {review['review_id']} for {lot}, opened by {review.get('opened_by')}:")
        origin = {"ai": " (AI-generated)", "scripted": " (scripted, not AI)"}.get(review.get("reason_origin"), "")
        self.out(f"  agent reason{origin}: {review.get('reason')}")
        for row in self.observe().lot_evidence(lot):
            label = "NEW " if row["evidence_id"] == correction else ""
            self.out(f"  {label}{row['kind']:<16} {row['evidence_id']}: {self.site_of(row['evidence_id'])}")
        self.out(f"  your reason: {reason}")
        self.out("This records YOUR decision as the human Reviewer. No agent can do this, and it publishes nothing.")
        if not yes and self.confirm("Type RESOLVE to record it, or press Enter to stop: ").strip() != "RESOLVE":
            raise Stop(STOPPED, "not confirmed; nothing changed.")
        digest = hashlib.sha256(json.dumps([review["review_id"], review["row_version"], correction, reason])
                                .encode()).hexdigest()[:16]
        response = call(self.http, "POST", f"/reviews/{review['review_id']}/resolve",
                        f"Resolving {review['review_id']}", json={
                            "expected_row_version": review["row_version"], "reason": reason,
                            "evidence_ids": [correction], "command_id": f"sc-resolve-{digest}"})
        if response.status_code == 409:
            raise Stop(STOPPED, "the review changed while you read it. Run review again to reread it.")
        if response.status_code >= 400:
            raise Stop(FAIL, f"resolving {review['review_id']} failed with HTTP {response.status_code}: "
                             f"{problem(body_of(response))}")
        self.register.data["human"].append({"what": f"Reviewer resolved {review['review_id']} for {lot}",
                                            "detail": f"reason: {reason}; new evidence {correction}; "
                                                      "identity recorded in the app audit trail, not here"})
        self.out(f"  Reviewer resolved {review['review_id']} for {lot}.")
        return f"{review['review_id']} resolved by a human Reviewer."

    def decide_case(self, yes, lot=None, denials=True):
        """The Compliance part of the review act for one case: denied attempts first (once per run), then keep_hold,
        the audit report and the accepted action plan. A case left open on purpose gets keep_hold only. Each decision
        uses a command ID derived from the case state, so a rerun replays. Returns (message, denials ran)."""
        c = self.series
        lot = lot or c.case_lot
        case = self.observe().case(lot)
        if case is None:
            if c.varied:
                self.out(f"{lot} has no compliance case; nothing to decide (recorded in the checkpoints).")
                return f"{lot} had no compliance case.", False
            raise Stop(STOPPED, f"{lot} has no compliance case. Run prepare first.")
        if case["status"] != "open":
            self.out(f"The {lot} compliance case {case['case_id']} is already {case['status']}.")
            return f"The {lot} compliance case {case['case_id']} is already {case['status']}.", False
        case_id = case["case_id"]
        detail = read(self.http, f"/compliance/cases/{case_id}", f"Reading {case_id}")
        kept = any(d["decision"] == "keep_hold" for d in detail.get("decisions") or [])
        stays_open = lot in c.open_lots
        if stays_open and kept:
            self.out(f"The {lot} compliance case {case_id} stays on hold; no audit report has arrived.")
            return f"{case_id} stays on hold.", False
        self.out(f"Open compliance case {case_id} for {lot}, opened by {case['opened_by']} ({case['reason_code']}).")
        proposal = detail.get("proposal") or {}
        if proposal:
            mark = proposal.get("reason_provenance") or {}
            origin = " (AI-generated)" if mark.get("ai_generated") else ""
            self.out(f"  agent proposal{origin}: {proposal.get('action')}: {(proposal.get('reason') or '-')[:160]}")
        if denials:
            self.case_denials(case)
        self.out("This records YOUR decisions as Compliance. No agent or Reviewer can do this; it publishes nothing.")
        if not yes and self.confirm("Type RESOLVE to record them, or press Enter to stop: ").strip() != "RESOLVE":
            raise Stop(STOPPED, "not confirmed; the compliance case is unchanged.")
        if not kept:
            case = self.decide(case, {"decision": "keep_hold", "reason": KEEP_HOLD_REASON}, [])
        if stays_open:
            return f"{case_id} kept on hold by Compliance; no audit report has arrived, so it stays open.", denials
        audit = self.upload(c.audit_evidence(lot))
        case = self.decide(case, {"decision": "accept_with_action_plan", "reason": ACCEPT_REASON,
                                  "owner": ACTION_OWNER, "due_date": ACTION_DUE}, [audit])
        return (f"{case_id} accepted with an action plan by Compliance (owner and due date {ACTION_DUE} recorded).",
                denials)

    def decide(self, case, fields, evidence_ids):
        """Record one Compliance decision; returns the case with its new row version."""
        case_id = case["case_id"]
        digest = hashlib.sha256(json.dumps([case_id, case["row_version"], fields["decision"], evidence_ids])
                                .encode()).hexdigest()[:16]
        response = call(self.http, "POST", f"/compliance/cases/{case_id}/decisions", f"Deciding {case_id}", json={
            **fields, "expected_row_version": case["row_version"], "evidence_ids": evidence_ids,
            "command_id": f"sc-case-{digest}"})
        body = body_of(response)
        if response.status_code == 409:
            raise Stop(STOPPED, f"the case changed while you read it ({problem(body)}). Run review again.")
        if response.status_code >= 400:
            raise Stop(FAIL, f"deciding {case_id} failed with HTTP {response.status_code}: {problem(body)}")
        detail = ", ".join(f"{k} {fields[k]}" for k in ("owner", "due_date") if k in fields)
        self.register.data["human"].append({
            "what": f"Compliance recorded {fields['decision']} on {case_id} for {case['lot_id']}",
            "detail": f"reason: {fields['reason']}; {detail + '; ' if detail else ''}evidence "
                      f"{', '.join(evidence_ids) or 'none'}; identity recorded in the app audit trail, not here"})
        self.out(f"  Compliance {fields['decision']} on {case_id}: case {body.get('status')} "
                 f"(row version {body.get('row_version')})")
        return {**case, "row_version": body.get("row_version"), "status": body.get("status")}

    def case_denials(self, case):
        """A Reviewer cannot decide a compliance case, an agent identity cannot either, and the Supplier agent cannot
        clear the lot while the case is open."""
        c, case_id, record = self.series, case["case_id"], self.register.denial
        attempt = {"decision": "accept_with_action_plan", "expected_row_version": case["row_version"],
                   "reason": "Attempt by a role that may not decide.", "owner": "nobody", "due_date": ACTION_DUE,
                   "evidence_ids": [], "command_id": f"sc-chk-case-{case_id.lower()}"}
        path = f"/compliance/cases/{case_id}/decisions"
        checks = [("Reviewer decides a compliance case", "403", self.reviewer_only,
                   "needs a Reviewer-only account"),
                  ("Supplier agent identity decides a compliance case", "403", self.agent_http,
                   "agent identities are not available to a person")]
        for name, expected, client, missing in checks:
            if client is None:
                record(name, expected, missing, exercised=False)
            else:
                record(name, expected, self.status_of(client, "POST", path, json=attempt))
        name = "Supplier agent clears a lot with an open compliance case"
        if self.double is not None:
            self.double.force_action = "no_issue"
            try:
                task = task_id(c, "supplier", case["lot_id"], self.observe().digest(), "check-clear-case")
                observed = self.status_of(self.http, "POST", "/agents/supplier/run",
                                          json={"task_id": task, "subject": case["lot_id"]})
            finally:
                self.double.force_action = None
            record(name, "409 compliance_case_open", observed)
        else:
            record(name, "409 compliance_case_open", "needs a scripted double; a real model is not forced to "
                   "misbehave", exercised=False)
        for row in self.register.data["denials"]:
            self.out(f"  denied - {row['name']}: expected {row['expected']}, observed {row['observed']} "
                     f"({row['result']})")
        failed = [d["name"] for d in self.register.data["denials"] if d["result"] == "FAIL"]
        if failed:
            raise Stop(FAIL, f"denied-access checks failed: {', '.join(failed)}.")

    # -- complete --------------------------------------------------------------------------------------------
    def act_complete(self, wait=0, patient=False):
        """Wait up to wait seconds for the human decisions, then let the agents reassess and publish. A patient run
        (the story) keeps waiting while a hold is not yet decidable, and says so only when the situation changes."""
        c = self.series
        deadline, shown = time.monotonic() + wait, None
        while True:
            view = self.observe()
            waiting = [lot for lot in dict.fromkeys(lot for b in c.batteries if not view.committed(b)
                                                     for lot in view.lots_of(b))
                       if lot not in c.open_lots and (view.open_review(lot) or view.open_case(lot))]
            stuck = view.undecidable()
            if stuck and not patient and not c.varied:
                raise Stop(STOPPED, f"the Reviewer cannot decide {', '.join(stuck)} yet: no document has arrived "
                                    f"since the hold opened. Next: {view.unblock(stuck)}; then run complete again.")
            if not waiting or time.monotonic() >= deadline:
                break
            note = f"Waiting for the human decision on {', '.join(waiting)}" + (
                f" ({', '.join(stuck)} needs a new document first: {view.unblock(stuck)})" if stuck else "")
            if not patient:
                self.out(f"{note}; checking again in 10 seconds...")
            elif note != shown:
                self.out(f"{note} in the app; checking every 10 seconds...")
                shown = note
            self.sleep(10)
        if waiting:
            if c.varied:
                self.register.checkpoint("human decisions recorded before complete", [], waiting)
                self.out(f"  the human decision on {', '.join(waiting)} is still open; recorded, and the run "
                         "continues.")
            elif patient:
                raise Stop(STOPPED, f"the human decision on {', '.join(waiting)} is still open after the wait. Decide "
                                    "in the app, then run story again; it continues where the story stands.")
            else:
                raise Stop(STOPPED, f"the human decision on {', '.join(waiting)} is still open. Run review, or resolve "
                                    "it in the app, then run complete again (or use complete --wait).")
        total = len(c.batteries)
        self.out(f"Complete for series {c.name}: agents reassess the corrected and accepted lots and publish the held "
                 "batteries.")
        expected = {("supplier", lot): {"no_issue"} for lot in c.corrected + c.audited_lots}
        released = c.held + c.compliance_held + c.released_issue
        # Beside the app's workflow, a held battery may already be published when its Issuance task runs.
        published = {"published", "already_published"} if patient else {"published"}
        view, step = self.drive(lambda role, subject: expected.get((role, subject)) or (
            published if role == "issuance" and subject in released else
            {"calculated"} if role == "carbon" else None), "complete", patient=patient)
        if not c.varied:
            if step[0] != "done":
                raise Stop(FAIL, f"expected all passports published after complete, got: {describe(step, c)}.")
            self.checkpoint(view, f"after complete: {total} of {total} published, no open reviews or cases",
                            c.batteries, [], [])
            self.show_measures("after complete")
            return (f"{total} of {total} passports published. Next: aftercare (lifecycle event, ledger check and "
                    "denied access).")
        if step[0] not in ("done", "handoff"):
            self.register.checkpoint("complete ends with nothing left for an agent", "done or handoff", step[0])
        open_reviews = [lot for lot in c.open_lots if lot in c.review_lots]
        open_cases = [lot for lot in c.open_lots if lot in c.case_lots]
        self.checkpoint(view, f"after complete: {len(c.final_published)} of {total} published, {len(c.blocked)} "
                              f"blocked by the pipeline, {len(c.open_held)} held on purpose", c.final_published,
                        open_reviews, open_cases)
        self.show_measures("after complete")
        held = f"; {', '.join(c.open_held)} stay held ({', '.join(c.open_lots)} open on purpose)" if c.open_held else ""
        return (f"{self.observe().committed_count()} of {total} passports published; the data pipeline blocked "
                f"{len(c.blocked)} ({', '.join(c.blocked) or 'none'}){held}. Next: aftercare (lifecycle event, ledger "
                "check and denied access).")

    # -- aftercare -------------------------------------------------------------------------------------------
    def act_aftercare(self, lifecycle=True, wait=0):
        c = self.series
        view = self.observe()
        total = len(c.batteries)
        committed = [b for b in c.batteries if view.committed(b)]
        if not c.varied and len(committed) != total:
            raise Stop(STOPPED, f"{len(committed)} of {total} published; run complete first.")
        if c.varied:
            story = [c.batteries[2], c.batteries[4]]
            if not all(view.committed(b) for b in story):
                raise Stop(STOPPED, f"{' and '.join(story)} are not published yet; run complete first.")
            if len(committed) < len(c.final_published):
                self.out(f"  {len(committed)} of {len(c.final_published)} expected passports are published; aftercare "
                         "checks the published ones.")
        if lifecycle:
            event = self.lifecycle_story(wait=wait)
        else:
            first = c.story_messages()[0]
            routed = self.send(first, f"Sending the state-of-health event for {first['payload']['battery_id']}")
            event = {**routed["lifecycle_event"],
                     "expected_passport_version": self.version_of(first["payload"]["battery_id"])}
            self.register.checkpoint("lifecycle story", "exercised", "not exercised (--skip-lifecycle)")
        for _batch in range(20):
            response = call(self.http, "POST", "/admin/outbox/drain", "Draining the outbox", json={})
            if response.status_code >= 400:
                raise Stop(FAIL, f"draining the outbox failed with HTTP {response.status_code}.")
            processed = body_of(response).get("processed")
            if not isinstance(processed, list):
                raise Stop(FAIL, "draining the outbox returned no processed-item list.")
            if len(processed) < 50:
                break
        else:
            raise Stop(FAIL, "the outbox still had work after 20 drain batches.")
        checked = committed + ([c.successor] if lifecycle else [])
        verified = {}
        for battery in checked:
            number = read(self.http, f"/passports/{battery}", f"Reading {battery}")["passport_version"]
            result = read(self.http, f"/passports/{battery}/versions/{number}/verify", f"Verifying {battery}")
            verified[battery] = f"{result['current_blob']}/{result['recorded_version']}"
        if not self.register.checkpoint("snapshot integrity (current blob / recorded version)",
                                        {b: "match/match" for b in checked}, verified):
            raise Stop(FAIL, f"snapshot integrity did not match: {verified}.")
        self.out(f"  snapshot integrity checked at {utc_now()} for {len(checked)} passports: all match.")
        ledger_problem = self.ledger_check(committed)
        self.denials(view, event, lifecycle)
        self.register.data["paths"] = self.observe().paths()
        if lifecycle:
            self.role_views(c.successor)
            if self.viewers.get("OEM"):
                self.oem_aftercare()
        failed = [d["name"] for d in self.register.data["denials"] if d["result"] == "FAIL"]
        if failed:
            raise Stop(FAIL, f"denied-access checks failed: {', '.join(failed)}.")
        if ledger_problem:
            raise Stop(FAIL, ledger_problem)
        if not lifecycle:
            return "Snapshot integrity, SQL ledger verification and denied-access checks complete (no lifecycle story)."
        status = self.lifecycle_summary(checked)
        if c.extra:
            unpublished = total - len(committed)
            rest = f"; {unpublished} unpublished ones stay held or blocked" if unpublished else ""
            self.out(f"  the lifecycle story covers {c.batteries[2]} and {c.batteries[4]}; the other published "
                     f"batteries stay active{rest}.")
        export = self.show_measures("after aftercare")
        if self.workbook:
            self.out(f"  {self.workbook}")
            self.register.data["workbook"] = self.workbook
        mine = set(c.batteries) | {c.successor}
        result = metrics.measures([r for r in export["rows"] if r["battery_id"] in mine], [])
        expected = {"produced": total, "published": len(committed), "repurposed": 1}
        if not self.register.checkpoint("measures after aftercare (successor not a produced battery)", expected,
                                        {**{k: result["coverage"][k] for k in ("produced", "published")},
                                         "repurposed": result["second_life"]["repurposed"]}):
            raise Stop(FAIL, f"the measures after aftercare were {result['coverage']}, {result['second_life']}.")
        return (f"Lifecycle story recorded: {c.batteries[2]} {status[c.batteries[2]]} as {c.successor} "
                f"({status[c.successor]}), {c.batteries[4]} {status[c.batteries[4]]}. Snapshot integrity, SQL ledger "
                "verification and denied-access checks complete.")

    def oem_aftercare(self):
        c, client = self.series, self.viewers["OEM"]
        near, over = c.batteries[2], c.batteries[4]
        warranty = {}
        for physical, passport in ((near, c.successor), (over, over)):
            item = read(client, f"/passports/{passport}/view", "Reading OEM warranty").get("warranty") or {}
            warranty[physical] = [item.get(key) for key in ("physical_battery_id", "actual_cycles",
                                                            "warranty_cycles", "cycle_status")]
        if not self.register.checkpoint("OEM cycle-limit review",
                                        {near: [near, 97, 100, "near_limit"], over: [over, 106, 100, "exceeded"]},
                                        warranty):
            raise Stop(FAIL, f"the warranty review of {near} and {over} was {warranty}.")
        fleet = read(client, f"/oem/fleet/view?series_id={c.series_id}", "Reading OEM recall exposure")
        matched = [row for row in fleet["packs"] if row["physical_battery_id"] == near]
        observed = [(row["passport_id"], row["holder_id"]) for row in matched]
        if not self.register.checkpoint("one affected physical pack follows its successor and holder",
                                        [(c.successor, "SECONDLIFE-RELECLOUD")], observed):
            raise Stop(FAIL, f"the fleet match for {near} was {observed}.")

    def role_views(self, battery):
        """The field groups each viewer role sees on one passport. Roles come from the sign-in only; a deployed run
        has no test users with viewer roles unless they were assigned."""
        clients = {"public": self.anonymous, **self.viewers}
        self.out(f"  role views of {battery} (field groups from GET /passports/{battery}/view):")
        observed, views = {}, {}
        for name in VIEW_GROUPS:
            client = clients.get(name)
            if client is None:
                views[name] = f"not exercised - needs a test user with Passport.{name} (python -m dbp_mvp roles assign)"
                self.out(f"    {name:<10} {views[name]}")
                continue
            body = body_of(call(client, "GET", f"/passports/{battery}/view", f"Reading the {name} view of {battery}"))
            observed[name] = body.get("field_groups")
            views[name] = ", ".join(observed[name] or []) or body.get("error") or "-"
            self.out(f"    {name:<10} {views[name]}")
        self.register.data["role_views"] = {"passport": battery, "views": views}
        if not self.register.checkpoint("field groups per viewer role", {k: VIEW_GROUPS[k] for k in observed},
                                        observed):
            raise Stop(FAIL, f"the role views of {battery} were {observed}.")

    def show_measures(self, when):
        """The four success measures (data/metrics.py) over the app's status export, overall and per plant."""
        export = read(self.http, "/report/status", "Reading the status export")
        result = metrics.measures(export["rows"], export["lot_claims"])
        cover, footprint = result["coverage"], result["verified_footprint"]
        supply, second = result["supplier_completeness"], result["second_life"]
        self.register.data.setdefault("measures", []).append(
            {"when": when, "export_id": export["export_id"], "coverage": cover, "verified_footprint": footprint,
             "supplier_completeness": supply, "second_life": second, "plants": result["plants"]})

        def share(value):
            return "-" if value is None else f"{value:.1f} %"

        def kwh(value):
            return "-" if value is None else f"{value:.2f}"
        self.out(f"  measures {when} (synthetic; footprints and values illustrative; export {export['export_id']}):")
        self.out(f"    passport coverage           {cover['published']} of {cover['produced']} produced batteries "
                 f"published ({share(cover['coverage_pct'])}; successor passports not counted)")
        self.out(f"    verified footprint          {footprint['verified']} of {footprint['published']} published "
                 f"batteries, average {kwh(footprint['avg_kg_co2e_per_kwh'])} kg CO2e/kWh")
        self.out(f"    supplier data completeness  {supply['complete']} of {supply['lots']} lots complete with a "
                 f"verified claim ({share(supply['complete_pct'])})")
        self.out(f"    second life                 {second['repurposed']} repurposed, "
                 f"{second['value_eur_illustrative']:,.0f} EUR (illustrative)")
        self.out(f"    {'plant':<10} {'produced':>8} {'published':>9} {'coverage':>9} {'verified':>8} "
                 f"{'kg CO2e/kWh':>11}")
        for plant in result["plants"]:
            self.out(f"    {plant['plant_id']:<10} {plant['produced']:>8} {plant['published']:>9} "
                     f"{share(plant['coverage_pct']):>9} {plant['verified_footprint_batteries']:>8} "
                     f"{kwh(plant['avg_footprint_per_kwh']):>11}")
        return export

    def version_of(self, battery):
        return read(self.http, f"/passports/{battery}/restricted", f"Reading {battery}")["state"]["committed_version"]

    def lifecycle_story(self, wait=0, stage=False):
        """Send each story event through the connector and let the Lifecycle agent record it before sending the next,
        because each event depends on the state the previous one left. A Reviewer decides B-003's second life.
        stage=True sends only B-003's events and leaves its second-life request for a Reviewer in the app; a later
        call skips the events already staged. Returns the first routed event, which the denied-access checks reuse."""
        first = None
        for envelope in self.series.story_messages():
            payload = envelope["payload"]
            battery, kind = payload["battery_id"], payload["event_type"]
            if stage and battery != self.series.batteries[2]:
                break
            event = self.staged.get(payload["message_id"])
            if event is not None:
                first = first or event
                if kind == "request_repurpose":
                    self.approve_second_life(battery, wait=wait)
                continue
            routed = self.send(envelope, f"Sending {kind} for {battery}")
            if routed.get("result") != "routed":
                raise Stop(FAIL, f"{payload['message_id']} was {routed.get('result')} ({routed.get('reason_code')}); "
                                 "expected routed.")
            event = {**routed["lifecycle_event"], "expected_passport_version": self.version_of(battery)}
            status, name, _ = self.agent("lifecycle", battery, self.observe(), event, kind)
            if status >= 400:
                raise Stop(STOPPED, f"the Lifecycle agent returned {name}. Check the hosted Lifecycle agent "
                                    "(python -m dbp_mvp deploy --from B4), or rerun with --skip-lifecycle.")
            if name not in ("recorded", "already_recorded"):
                raise Stop(FAIL, f"the Lifecycle agent answered {name} for {kind} on {battery}; expected recorded.")
            if stage:
                self.staged[payload["message_id"]] = event
            if first is None:
                first = event
                _, second, _ = self.agent("lifecycle", battery, self.observe(), event, "second")
                once = "recorded or already_recorded"
                self.register.checkpoint("lifecycle event recorded once, replay recognised",
                                         {"first": once, "second": "already_recorded"},
                                         {"first": once, "second": second})
                if second != "already_recorded":
                    raise Stop(FAIL, f"the repeated Lifecycle task answered {second}; expected already_recorded.")
            if kind == "record_ownership_change":
                self.order_check(battery)
            if kind == "request_repurpose":
                if stage:
                    request = self.second_life_request(battery)
                    self.out(f"  {battery}'s second-life request {request['request_id']} awaits a Reviewer in the "
                             "app; this run will not approve it.")
                else:
                    self.approve_second_life(battery, wait=wait)
        return first

    def second_life_request(self, battery):
        rows = [r for r in read(self.http, "/second-life", "Reading second-life requests")["requests"]
                if r["battery_id"] == battery]
        if not rows:
            raise Stop(FAIL, f"no second-life request was recorded for {battery}.")
        return rows[-1]

    def approve_second_life(self, battery, wait=0):
        request = self.second_life_request(battery)
        if wait and request["status"] == "pending":
            self.out(f"  Waiting for a Reviewer at /second-life/{request['request_id']}; this run will not approve it.")
            remaining = wait
            while request["status"] == "pending" and remaining > 0:
                delay = min(10, remaining)
                self.sleep(delay)
                remaining -= delay
                detail = read(self.http, f"/second-life/{request['request_id']}", "Waiting for the human decision")
                request = detail["request"]
            if request["status"] == "pending":
                raise Stop(STOPPED, "second-life approval is still pending. Decide in the app, then rerun aftercare "
                                   "with --wait. No approval was submitted by this run.")
        replayed = request["status"] != "pending"
        if not replayed:
            what = f"Approving second-life request {request['request_id']}"
            response = call(self.http, "POST", f"/second-life/{request['request_id']}/decision", what, json={
                "decision": "approve", "expected_row_version": request["row_version"],
                "command_id": f"second-life-{request['request_id']}-approve",
                "reason": f"Safety assessment {request['safety_assessment_ref']} read; state of health "
                          f"{request['soh_pct']}% meets the second-life minimum."})
            body = body_of(response)
            if response.status_code >= 400:
                raise Stop(FAIL, f"{what} failed with HTTP {response.status_code}: {problem(body)}")
            request = {**request, "status": body["status"]}
        if request["status"] != "approved":
            raise Stop(FAIL, f"second-life request {request['request_id']} is {request['status']}; expected approved.")
        self.out(f"  Reviewer approves second-life request {request['request_id']} for {battery}"
                 f"{' (already decided)' if replayed else ''}: {battery} repurposed, successor "
                 f"{request['successor_id']} created and linked (illustrative value "
                 f"{request['illustrative_value_eur']:,.0f} EUR)")
        links = read(self.http, f"/passports/{request['successor_id']}/links", "Reading the successor's links")
        observed = {"successor": [link["battery_id"] for link in read(self.http, f"/passports/{battery}/links",
                                                                       "Reading the links")["links"]],
                    "predecessor": [link["battery_id"] for link in links["links"]]}
        if not self.register.checkpoint("second life approved, passports linked both ways",
                                        {"successor": [request["successor_id"]], "predecessor": [battery]}, observed):
            raise Stop(FAIL, f"the second-life links were {observed}.")

    def check_message(self, issuer, kind, battery, issued, name, **details):
        """A signed check event with its own message ID, so the story's messages are untouched."""
        payload = {**signed_messages.event_payload(issuer, kind, battery, issued, **details),
                   "message_id": f"MSG-CHECK-{name}-{battery}"}
        return signed_messages.sign(payload, simulator.signing_keys(self.series.output)[issuer])

    def connector_answer(self, envelope, replay=False):
        try:
            response = self.http.request("POST", "/connector/v1/messages", json={"envelope": envelope})
        except NoAnswer as error:
            return f"no answer ({error})"
        body = body_of(response)
        result = body.get("result")
        answer = result if result in ("routed", "verified") else body.get("reason_code") or body.get("error") or result
        return f"{response.status_code} {answer}{' (replayed)' if replay and body.get('replayed') else ''}"

    def order_check(self, battery):
        """A state-of-health reading dated before the ownership change just recorded is quarantined."""
        envelope = self.check_message("OEM-ADVENTUREWORKS", "record_soh", battery, "2026-10-29T09:00:00Z", "ORDER",
                                      soh_pct=87, measured_utc="2026-10-29T08:30:00Z")
        self.register.denial("lifecycle event older than the last recorded one", "202 event_out_of_order",
                             self.connector_answer(envelope))

    def lifecycle_summary(self, batteries):
        status = {}
        for battery in batteries:
            state = read(self.http, f"/passports/{battery}/restricted", f"Reading {battery}")["state"]
            status[battery] = state["lifecycle_status"]
            self.out(f"  lifecycle {battery:<11} {state['lifecycle_status']:<20} holder {state['holder_id']}")
        self.register.data["lifecycle"] = status
        return status

    def ledger_check(self, batteries=None):
        """The database ledger check (sys.sp_verify_database_ledger against the anchored digests) for the published
        batteries. Locally the in-memory store has no ledger, so 'unavailable' is the expected, honest answer."""
        batteries = list(self.series.batteries if batteries is None else batteries)
        local = self.register.data["evidence_level"] == "local"
        allowed = {"unavailable"} if local else {"verified", "chain_verified_anchor_pending"}
        label = "unavailable (in-memory store has no SQL ledger)" if local else \
            "verified (or chain verified, digest anchor pending)"
        observed, reasons = {}, {}
        for battery in batteries:
            result = read(self.http, f"/ledger/verify?battery={battery}", f"Checking the SQL ledger for {battery}")
            observed[battery] = label if result["status"] in allowed else result["status"]
            reasons[battery] = result.get("reason_code")
        self.register.checkpoint("SQL ledger verification (sp_verify_database_ledger)",
                                 {b: label for b in batteries}, observed)
        bad = {b: f"{observed[b]} ({reasons[b]})" for b in batteries if observed[b] != label}
        self.out(f"  SQL ledger: {label}" if not bad else f"  SQL ledger: {observed}")
        return (f"SQL ledger verification did not pass: {bad}. See the ledger section of app/README.md; "
                "python -m dbp_mvp ledger verify checks the digests directly.") if bad else None

    def status_of(self, client, method, path, **kwargs):
        try:
            response = client.request(method, path, **kwargs)
        except NoAnswer as error:
            return f"no answer ({error})"
        body = body_of(response)
        code = body.get("error") if isinstance(body.get("error"), str) else None
        return f"{response.status_code} {code}" if code else str(response.status_code)

    def denials(self, view, event, lifecycle=True):
        c = self.series
        held, any_task = c.held[0], next((t["task_id"] for t in view.tasks), "sc-none-00000000")
        record = self.register.denial
        if self.anonymous is not None:
            record("anonymous reader opens a restricted passport", "401",
                   self.status_of(self.anonymous, "GET", f"/passports/{held}/restricted"))
        else:
            record("anonymous reader opens a restricted passport", "401", "-", exercised=False)
        if self.roleless is not None:
            record("signed-in user without a role opens a restricted passport", "403",
                   self.status_of(self.roleless, "GET", f"/passports/{held}/restricted"))
        else:
            record("signed-in user without a role opens a restricted passport", "403",
                   "needs a second, role-less account", exercised=False)
        record("staff token reads an agent's tool section", "403",
               self.status_of(self.http, "GET", f"/agents/tasks/{any_task}/evidence"))
        for name, expected, envelope in self.forged_messages():
            record(name, expected, self.status_of(self.http, "POST", "/connector/v1/messages",
                                                  json={"envelope": envelope}))
        for name, change in (("Lifecycle task with an event the connector did not route", {"issuer": "OEM-OTHER"}),
                             ("event names another battery", {"battery_id": held})):
            changed = {**event, **change}
            task = task_id(c, "lifecycle", event["battery_id"], json.dumps(changed, sort_keys=True), "check")
            record(name, "403 event_scope", self.status_of(self.http, "POST", "/agents/lifecycle/run", json={
                "task_id": task, "subject": event["battery_id"], "event": changed}))
        if self.agent_http is not None:
            lifecycle_task = next((t["task_id"] for t in reversed(self.observe().tasks) if t["role"] == "lifecycle"),
                                  any_task)
            record("Supplier agent identity reads a Lifecycle task", "403 tool_scope",
                   self.status_of(self.agent_http, "GET", f"/agents/tasks/{lifecycle_task}/event"))
        else:
            record("Supplier agent identity reads a Lifecycle task", "403 tool_scope",
                   "agent identities are not available to a person", exercised=False)
        self.lifecycle_denials(lifecycle)
        if self.double is not None:
            foreign = [e["evidence_id"] for e in view.lot_evidence(c.lots[0])][:1]
            self.double.foreign_citation = foreign
            try:
                task = task_id(c, "supplier", c.lots[1], self.observe().digest(), "check-foreign-citation")
                observed = self.status_of(self.http, "POST", "/agents/supplier/run",
                                          json={"task_id": task, "subject": c.lots[1]})
            finally:
                self.double.foreign_citation = None
            record("Supplier agent cites another supplier's evidence", "422 citation_required", observed)
        else:
            record("Supplier agent cites another supplier's evidence", "422 citation_required",
                   "needs a scripted double; a real model is not forced to misbehave", exercised=False)
        for row in self.register.data["denials"]:
            self.out(f"  denied access - {row['name']}: expected {row['expected']}, observed {row['observed']} "
                     f"({row['result']})")

    def lifecycle_denials(self, lifecycle):
        c = self.series
        record, story = self.register.denial, c.story_messages()
        checks = [("event on a closed passport", "202 passport_closed"),
                  ("event the lifecycle state does not allow", "202 state_not_allowed"),
                  ("the same signed event sent again", "201 routed (replayed)"),
                  ("Lifecycle agent identity decides a second-life request", "403")]
        if not lifecycle:
            for name, expected in checks:
                record(name, expected, "needs the lifecycle story (--skip-lifecycle)", exercised=False)
            return
        record(*checks[0], self.connector_answer(self.check_message(
            "OEM-ADVENTUREWORKS", "record_soh", c.batteries[2], "2026-11-10T09:00:00Z", "CLOSED", soh_pct=86,
            measured_utc="2026-11-10T08:30:00Z")))
        record(*checks[1], self.connector_answer(self.check_message(
            "RECYCLER-TREY", "record_recycling_closure", c.held[0], "2026-11-10T09:00:00Z", "STATE",
            recovered_kg={"cobalt": 1.0}, recovery_label="illustrative")))
        record(*checks[2], self.connector_answer(story[0], replay=True))
        request = next((r for r in read(self.http, "/second-life", "Reading second-life requests")["requests"]
                        if r["battery_id"] == c.batteries[2]), None)
        if self.lifecycle_http is not None and request is not None:
            record(*checks[3], self.status_of(self.lifecycle_http, "POST",
                                              f"/second-life/{request['request_id']}/decision", json={
                                                  "decision": "reject", "expected_row_version": request["row_version"],
                                                  "reason": "An agent may not decide.",
                                                  "command_id": "second-life-agent-check"}))
        else:
            record(*checks[3], "agent identities are not available to a person", exercised=False)

    def forged_messages(self):
        """[(check, expected answer, envelope)] for the connector: the series' state-of-health event re-signed by a
        party not registered for that event type, signed with another party's key, and altered after signing. Each
        has its own message ID, so the genuine message is untouched."""
        c = self.series
        genuine = c.soh_message()
        payload, battery = genuine["payload"], genuine["payload"]["battery_id"]
        keys = simulator.signing_keys(c.output)
        other = next(p for p in sorted(keys) if p != payload["issuer"])
        unauthorised = {**payload, "issuer": other, "message_id": f"MSG-CHECK-PARTY-{battery}"}
        wrong_key = signed_messages.sign({**payload, "message_id": f"MSG-CHECK-KEY-{battery}"}, keys[other])
        wrong_key["signature"]["key_id"] = signed_messages.key_id(other)
        altered = {**genuine, "payload": {**payload, "soh_pct": 99, "message_id": f"MSG-CHECK-ALTERED-{battery}"}}
        return [("connector message from a party not registered for the event type", "403 event_type_not_allowed",
                 signed_messages.sign(unauthorised, keys[other])),
                ("connector message signed with another party's key", "403 issuer_mismatch", wrong_key),
                ("connector message altered after signing", "403 bad_signature", altered)]

    # -- the whole story, unattended -------------------------------------------------------------------------
    def act_story(self, wait=0, anchor=None):
        """Play the whole story while people make the human decisions in the app: prepare, the partner messages and
        B-003's lifecycle events up to its second-life request, then wait for the decisions, complete and aftercare.
        Every step starts from what the app reports, so a rerun continues where the story stands. anchor(), if
        given, anchors the SQL ledger after complete and after aftercare."""
        c = self.series
        if c.varied:
            raise Stop(STOPPED, f"series {c.name} has {len(c.issues)} batteries with seeded data issues, whose human "
                                "decisions the story does not hand off. Run prepare, review --yes and complete "
                                "instead.")
        deadline = time.monotonic() + wait
        view = self.observe()
        if (view.state(c.batteries[4]) or {}).get("lifecycle_status") == "recycled":
            raise Stop(STOPPED, f"series {c.name} has already played its whole story; run status shows it. Use a new "
                                "--tag to play it again.")
        prepared = (all(view.committed(b) for b in c.clean) and view.case(c.case_lot) is not None
                    and any(r["lot_id"] == c.lots[0] for r in view.reviews))
        if prepared:
            self.out(f"Prepare is already done for series {c.name} ({view.committed_count()} of {len(c.batteries)} "
                     "published); the story continues.")
            if view.open_review(c.lots[0]):
                self.send(c.correction(), f"Sending the corrected claim for {c.lots[0]}")
        else:
            self.act_prepare(patient=True)
        view = self.observe()
        if view.open_case(c.case_lot):
            self.out(f"  the independent site audit report for {c.case_lot} arrives, ready for Compliance to cite:")
            self.upload(c.audit_evidence())
        second_life = c.batteries[2]
        if view.committed(second_life) and (view.state(second_life) or {}).get("lifecycle_status") != "repurposed":
            self.out(f"  partners send {second_life}'s lifecycle messages, up to its second-life request:")
            self.lifecycle_story(stage=True)
        self.handoff()
        self.act_complete(wait=max(0, deadline - time.monotonic()), patient=True)
        if anchor:
            anchor()
        message = self.act_aftercare(lifecycle=True, wait=max(1, round(deadline - time.monotonic())))
        if anchor:
            anchor()
        return f"Story complete. {message}"

    def handoff(self):
        """Tell the person who started the story which decisions now wait in the app, and where."""
        c, base = self.series, self.app_url
        view = self.observe()
        review, case = view.open_review(c.lots[0]), view.open_case(c.case_lot)
        battery = c.batteries[2]
        request = next((r for r in read(self.http, "/second-life", "Reading second-life requests")["requests"]
                        if r["battery_id"] == battery and r["status"] == "pending"), None)
        decisions = [text for ready, text in (
            (review, f"Reviewer: resolve the {c.lots[0]} hold with the supplier's corrected claim "
                     f"({base}/holds/{review['review_id'] if review else ''})"),
            (case, f"Compliance Officer: accept the {c.case_lot} case with an action plan, citing the site audit "
                   f"report ({base}/cases/{case['case_id'] if case else ''})"),
            (request, f"Reviewer: approve {battery}'s second-life request "
                      f"({base}/second-life/{request['request_id'] if request else ''})")) if ready]
        self.out(f"\nPassport readiness: {base}/exec?series={c.series_id}")
        if decisions:
            self.out("The story now waits for these decisions in the app, in any order (its Next step leads to each):")
            for number, text in enumerate(decisions, 1):
                self.out(f"  {number}. {text}")
        self.out("Leave this window running: the agents continue after each decision, and nothing here needs your "
                 "input.")

    # -- status ----------------------------------------------------------------------------------------------
    def show_status(self):
        c = self.series
        view = self.observe()
        self.out(BANNER)
        self.out(f"Series {c.name} ({c.series_id}): {view.committed_count()} of {len(c.batteries)} passports "
                 "published")
        self.out(f"{'Battery':<13} {'Lot':<16} {'Passport':<20} {'Version':<8} Why")
        for battery in c.batteries:
            label, version, why = battery_line(view, battery)
            lots = ", ".join(view.lots_of(battery)) or "-"
            self.out(f"{battery:<13} {lots:<16} {label:<20} {version:<8} {why}")
        response = call(self.http, "GET", f"/passports/{c.successor}/restricted", f"Reading {c.successor}")
        if response.status_code == 200:
            state = body_of(response)["state"]
            self.out(f"{c.successor:<13} {'-':<16} {'Second life':<20} {state['committed_version']!s:<8} "
                     f"{state['lifecycle_status']}, successor of {c.batteries[2]}, held by {state['holder_id']} "
                     "(not counted in the series)")
        step, stuck = plan(view), view.undecidable()
        self.out(f"Next step: {view.unblock(stuck) if stuck else describe(step, c)}")
        return view
