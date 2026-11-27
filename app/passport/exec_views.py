"""Read models for the executive screens: plain-language views over the ledger tables.

Pure reads. Nothing here writes, calls a model or publishes. Unknown numbers stay "not measured" rather than 0.
Agent output is always labelled as a proposal; the application's own checks are shown separately.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from . import access, domain, lifecycle, metrics, prompt_shields, provenance
from .provenance import AI_HOSTING, SCRIPTED_HOSTING
from .service import EVIDENCE_KINDS, MAX_JSON_EVIDENCE_BYTES
from .store import parse_utc

NOT_MEASURED = "not measured"
CARBON_LABEL = "Illustrative estimate"
SYNTHETIC = "Synthetic data"
ROLE_TITLES = {"supplier": "Supplier", "carbon": "Carbon", "issuance": "Issuance", "lifecycle": "Lifecycle",
               "watch": "Watch"}
AUTONOMY = {"supplier": "L3 - acts, escalates risk", "carbon": "L4 - autonomous with verifier evidence",
            "issuance": "L4 - autonomous", "lifecycle": "L4 - autonomous", "watch": "L2 - recommends"}
REASON_LABELS = {"ai": "AI-generated proposal", "scripted": "Scripted proposal (not AI)", "person": "Reason given",
                 "system": "Opened automatically by Prompt Shields (not AI text)"}
SYSTEM_ACTORS = {prompt_shields.ACTOR: "Prompt Shields"}
OUTCOMES = {  # code -> (plain text, short path text, tone)
    "review_requested": ("Asked for a human review", "review requested", "warn"),
    "compliance_escalated": ("Escalated to Compliance", "compliance case", "warn"),
    "no_issue": ("No issue found", "no issue", "good"),
    "calculated": ("Carbon footprint calculated (illustrative estimate)", "", "good"),
    "published": ("Published", "", "good"),
    "already_published": ("Already published - nothing changed", "no change", "info"),
    "recorded": ("Lifecycle event recorded", "", "good"),
    "already_recorded": ("Already recorded - nothing changed", "no change", "info"),
    "quarantined": ("Lifecycle event quarantined - not applied", "quarantined", "warn"),
    "recommended": ("Recommendation recorded for legal review", "recommendation", "info"),
    "awaiting_legal_review": ("Recommendation awaiting legal review", "recommendation", "info"),
    "deferred": ("Deferred - took no action", "deferred", "info"),
    "prompt_attack_detected": ("Stopped by Prompt Shields - a person reviews the evidence", "prompt shield", "warn"),
    "running": ("No result yet", "running", "info"),
}
ACTION_OUTCOMES = {"request_review": "review_requested", "escalate_compliance": "compliance_escalated",
                   "no_issue": "no_issue", "calculate": "calculated",
                   "publish": "published", "recommend": "recommended",
                   **{kind: "recorded" for kind in ("record_soh", "record_ownership_change", "request_repurpose",
                                                    "record_end_of_life", "record_recycling_closure")}}
CHECKS = {
    "supplier": "Cited evidence must belong to this lot; only a person can clear an open review or compliance case.",
    "carbon": "The number comes from the application's deterministic calculator, not the model.",
    "issuance": "Publication gates: inputs complete, no open review or compliance case, supplier cleared on current "
                "evidence, calculation present.",
    "lifecycle": "The app's state machine checks issuer, holder, state and order; it records the event once or "
                 "quarantines it. Only a Reviewer decides a second life.",
    "watch": "Recommendations never change the active policy.",
}
STATUS = {  # code -> (text, tone)
    "published": ("Published", "good"),
    "published_outdated": ("Published - newer inputs awaiting checks", "warn"),
    "held": ("On hold - evidence under review", "warn"),
    "blocked": ("Not yet issued - inputs incomplete", "bad"),
    "pending": ("Awaiting agent checks", "info"),
}
SHORT_STATUS = {"published": "Published", "published_outdated": "Update pending", "held": "On hold",
                "blocked": "Inputs incomplete", "pending": "Awaiting checks"}
PUBLIC_NOT_ISSUED = "Not yet issued - evidence under review"
LEDGER_TITLES = {"verified": "Ledger verified",
                 "chain_verified_anchor_pending": "Ledger chain verified - anchor pending",
                 "failed": "Ledger verification FAILED", "unavailable": "Ledger verification not available"}
LEDGER_TONES = {"verified": "good", "chain_verified_anchor_pending": "warn", "failed": "bad", "unavailable": "info"}


def when(text, precise=False):
    if not text:
        return ""
    moment = parse_utc(text) if isinstance(text, str) else text
    moment = moment.astimezone(timezone.utc)
    if precise:
        return (moment.strftime("%d %b %Y, %H:%M:%S.") + f"{moment.microsecond // 1000:03d} UTC").lstrip("0")
    return moment.strftime("%d %b %Y, %H:%M UTC").lstrip("0")


def minutes_between(start, end):
    return max(0, int((parse_utc(end) - parse_utc(start)).total_seconds() // 60))


def age_text(minutes):
    if minutes < 60:
        return f"{minutes} min"
    if minutes < 48 * 60:
        return f"{minutes // 60} h {minutes % 60} min"
    return f"{minutes // (24 * 60)} days"


def money(value, currency):
    text = f"{value:,.2f}" if value >= 1 else f"{value:.6f}".rstrip("0").rstrip(".")
    return f"{currency} {text if text not in ('', '0') else '0'}"


def per_kwh(value):
    """The calculator stores exact decimals; executives read two decimal places."""
    try:
        return f"{Decimal(str(value)).quantize(Decimal('0.01')):,} kg CO2e/kWh"
    except (InvalidOperation, ValueError):
        return f"{value} kg CO2e/kWh"


def person(actor):
    """'name (id)' -> 'name'. The full identity stays in the audit trail."""
    return (actor or "").rsplit(" (", 1)[0] or "unknown"


def now_of(service):
    value = service.clock()
    return value if isinstance(value, str) else datetime.now(timezone.utc).isoformat()


def battery_status(state):
    if state["committed_version"]:
        outdated = state["committed_input_hash"] and state["committed_input_hash"] != state["input_hash"]
        code = "published_outdated" if outdated else "published"
    elif state["eligibility"] == domain.HELD:
        code = "held"
    elif state["eligibility"] == domain.BLOCKED:
        code = "blocked"
    else:
        code = "pending"
    text, tone = STATUS[code]
    return {"code": code, "text": text, "tone": tone}


# ---- agent runs ------------------------------------------------------------------------------------
def outcome_code(status_code, body):
    """The same mapping the command line prints: an application refusal wins over the model's proposal."""
    if body is None:
        return "running"
    if (status_code or 0) >= 400 or "error" in body:
        return body.get("error") or f"http_{status_code}"
    result = body.get("outcome") or {}
    if result.get("status") in ("deferred", "already_recorded", "awaiting_legal_review", "quarantined"):
        return result["status"]
    action = (body.get("decision") or {}).get("action")
    if action == "publish" and not result.get("committed_now"):
        return "already_published"
    return ACTION_OUTCOMES.get(action, action or "unknown")


def price_of(prices, model, tokens_in, tokens_out):
    entry = ((prices or {}).get("models") or {}).get(model or "")
    if entry is None or tokens_in is None or tokens_out is None:
        return None
    return tokens_in / 1e6 * entry["input_per_million"] + tokens_out / 1e6 * entry["output_per_million"]


def run_record(task, result_row, prices):
    body = json.loads(result_row["result_json"]) if result_row else None
    execution = (body or {}).get("execution") or {}
    code = outcome_code(result_row["status_code"] if result_row else None, body)
    refused = code not in OUTCOMES and code not in ("unknown",)
    text, short, tone = OUTCOMES.get(code, (f"Refused by the application ({code})", "refused", "bad"))
    hosting = execution.get("hosting")
    if hosting in AI_HOSTING:
        label = "AI proposal"
    elif hosting in SCRIPTED_HOSTING:
        label = "Scripted local run (not AI)"
    elif refused and not execution:
        label = "Proposal refused"
    else:
        label = "Agent proposal"
    tokens_in, tokens_out = execution.get("input_tokens"), execution.get("output_tokens")
    measured = tokens_in is not None and tokens_out is not None
    cost = price_of(prices, execution.get("model"), tokens_in, tokens_out)
    decision = (body or {}).get("decision") or {}
    marks = {m.get("field"): m for m in (body or {}).get("provenance") or [] if isinstance(m, dict)}
    reason = decision.get("reason") or ""
    role = task["role"]
    actor = task.get("actor")
    origin = ("Automatic application workflow" if actor == "workflow:application-event" else
              f"Operator/API request by {person(actor)}" if actor else "Not recorded")
    completed = result_row.get("recorded_utc") if result_row else None
    return {
        "task_id": task["task_id"], "role": role, "role_title": ROLE_TITLES.get(role, role.title()),
        "subject": task["subject"], "at": task["created_utc"], "when": when(task["created_utc"]),
        "origin": origin, "completed_when": when(completed, precise=True) if completed else
        ("Not recorded" if result_row else "No result yet"),
        "code": code, "text": text, "short": short, "tone": tone, "refused": refused, "label": label,
        "is_ai": hosting in AI_HOSTING, "scripted": hosting in SCRIPTED_HOSTING, "autonomy": AUTONOMY.get(role, ""),
        "model": execution.get("model") or "not recorded",
        "model_version": execution.get("model_version") or "",
        "tokens": f"{tokens_in:,} in / {tokens_out:,} out" if measured else NOT_MEASURED,
        "token_total": tokens_in + tokens_out if measured else None,
        "latency": f"{execution['latency_ms']:,} ms" if execution.get("latency_ms") is not None else NOT_MEASURED,
        "cost_value": cost,
        "cost": money(cost, prices["currency"]) if cost is not None else NOT_MEASURED,
        "reason": reason,
        "reason_ai": bool(reason) and (marks["reason"]["ai_generated"] if "reason" in marks
                                       else provenance.ai_generated(execution)),
        "provenance_id": (marks.get("reason") or {}).get("provenance_id") or "",
        "app_message": (body or {}).get("message") or "",
        "check": CHECKS.get(role, ""),
        "gate": ("No result yet" if code == "running" else
                 f"Refused by the application ({code})" if refused else "Accepted by the application"),
        "gate_tone": "info" if code == "running" else ("bad" if refused else "good"),
        "committed_now": bool(((body or {}).get("outcome") or {}).get("committed_now")),
        "committed_version": ((body or {}).get("outcome") or {}).get("committed_version"),
    }


def run_totals(runs, prices, published):
    total = len(runs)
    measured = [r for r in runs if r["token_total"] is not None]
    tokens = sum(r["token_total"] for r in measured)
    if not measured:
        token_text = f"tokens {NOT_MEASURED}"
    elif len(measured) < total:
        token_text = f"{tokens:,} tokens ({len(measured)} of {total} runs measured)"
    else:
        token_text = f"{tokens:,} tokens"
    if not prices:
        cost = {"status": NOT_MEASURED, "text": NOT_MEASURED, "per_passport": NOT_MEASURED,
                "note": "No model prices are configured (DBP_PRICES_B64), so no cost is claimed."}
    else:
        priced = [r for r in runs if r["cost_value"] is not None]
        value = sum(r["cost_value"] for r in priced)
        status = NOT_MEASURED if not priced else ("measured" if len(priced) == total else "partially measured")
        currency = prices["currency"]
        cost = {"status": status, "text": money(value, currency) if priced else NOT_MEASURED,
                "per_passport": money(value / published, currency) if priced and published else NOT_MEASURED,
                "note": f"Model tokens only ({len(priced)} of {total} runs priced"
                        f"{', price list ' + prices['source'] if prices.get('source') else ''}); excludes hosting, "
                        "storage, SQL, Foundry and review labour."}
    refused = sum(1 for r in runs if r["refused"])
    return {"runs": total, "refused": refused, "tokens": token_text, "cost": cost,
            "runs_text": f"{total} agent run{'s' if total != 1 else ''}"
                         + (f" ({refused} refused by the application)" if refused else "")}


# ---- passport readiness (/exec) --------------------------------------------------------------------
def series_label(series_id):
    """Executives read 'production series MAIN'; the series ID stays the key in URLs and records."""
    text = str(series_id or "")
    return text[len("SERIES-"):] if text.upper().startswith("SERIES-") and len(text) > len("SERIES-") else text


def all_series(tx):
    packages = sorted(tx.select("Packages"), key=lambda p: (p["build_sequence"], p["imported_utc"]))
    ordered = []
    for package in reversed(packages):
        if package["series_id"] not in ordered:
            ordered.append(package["series_id"])
    return ordered, packages


@dataclass(frozen=True)
class SeriesScope:
    series_id: str
    options: list
    builds: set
    produced: set
    batteries: set
    lots: set

    @property
    def all(self):
        return self.series_id == "all"

    @property
    def valid(self):
        return self.all or self.series_id in self.options

    @property
    def label(self):
        return "All production series" if self.all else f"Production series {series_label(self.series_id)}"

    def includes(self, value, group="batteries"):
        return self.all or value in getattr(self, group)


def production_scope(service, series_id=None, battery=None, lot=None):
    """Resolve actual package membership, including successors; a tag's spelling is not membership."""
    with service.store.read() as tx:
        ordered, packages = all_series(tx)
        states = {s["battery_id"]: s for s in tx.select("PassportState")}
        links = tx.select("PassportLinks", link_type="successor")
        members, lots = {}, {}
        for package in packages:
            key = package["series_id"]
            members.setdefault(key, set()).update(
                r["battery_id"] for r in tx.select("PackageBatteries", build_id=package["build_id"]))
            lots.setdefault(key, set()).update(
                r["lot_id"] for r in tx.select("PackageLots", build_id=package["build_id"]))
        successors = {link["linked_id"] for link in links}
        produced = {key: ids - successors for key, ids in members.items()}
        for ids in members.values():
            while additions := {link["linked_id"] for link in links if link["battery_id"] in ids} - ids:
                ids.update(additions)
        if series_id is None:
            series_id = next((key for key in ordered if battery in members[key] or lot in lots[key]),
                             ordered[0] if ordered else "all")
        if series_id == "all":
            battery_ids, lot_ids = set(states), set().union(*lots.values())
            produced_ids = battery_ids - successors
        else:
            battery_ids, lot_ids = members.get(series_id, set()), lots.get(series_id, set())
            produced_ids = produced.get(series_id, set())
        lot_ids.update(x for b in battery_ids if b in states for x in domain.split_ids(states[b]["lot_ids"]))
        return SeriesScope(series_id, ordered,
                           {p["build_id"] for p in packages if series_id == "all" or p["series_id"] == series_id},
                           produced_ids, battery_ids, lot_ids)


def lot_evidence_state(rows):
    claim = any(r["kind"] in ("supplier_claim", "corrected_claim") for r in rows)
    transport = any(r["kind"] == "transport_record" for r in rows)
    have = int(claim) + int(transport)
    label = ("Complete" if have == 2 else "Claim only" if claim else
             "Transport only" if transport else "No claim or transport")
    missing = ("" if have == 2 else "Transport record missing" if claim else
               "Supplier claim missing" if transport else "Supplier claim and transport record missing")
    return {"complete": have == 2, "text": f"{label} ({have} of 2)", "missing": missing,
            "tone": "good" if have == 2 else "info" if claim else "warn", "files": len(rows)}


def supplier_state(facts, rows):
    if not facts:
        return {"text": "Not checked yet", "tone": "info"}
    latest = facts[-1]
    if rows and latest["recorded_utc"] < max(r["uploaded_utc"] for r in rows):
        return {"text": "New evidence since the last check", "tone": "info"}
    action = json.loads(latest["data_json"]).get("action")
    if action == "no_issue":
        return {"text": "Supplier agent found no issue", "tone": "good"}
    if action == "request_review":
        return {"text": "Supplier agent asked for a human review", "tone": "warn"}
    if action == "escalate_compliance":
        return {"text": "Supplier agent escalated to Compliance", "tone": "warn"}
    return {"text": "Supplier agent deferred", "tone": "info"}


def opened_by_text(opened_by):
    if (opened_by or "").startswith("agent:"):
        return f"{ROLE_TITLES.get(opened_by[6:], opened_by[6:])} agent"
    if opened_by in SYSTEM_ACTORS:
        return f"{SYSTEM_ACTORS[opened_by]} (automatic)"
    return person(opened_by)


def actor_role(actor):
    """A coded role for telemetry: never a person's name."""
    if actor in SYSTEM_ACTORS:
        return actor
    return actor if (actor or "").startswith("agent:") and actor[6:] in ROLE_TITLES else "reviewer"


def batteries_text(count):
    return f"{count} batter{'ies' if count != 1 else 'y'}"


def next_action(service, scope, batteries, holds, open_cases, valid, total):
    """The one thing a person should do next in the selected scope, in the order the story needs it: sourcing holds,
    compliance cases, second-life requests, then a published passport. The agents work in between; people decide."""
    if holds:
        first = holds[0]
        return {"text": f"Review the hold on {first['lot_id']}", "url": first["url"],
                "why": f"{batteries_text(len(first['batteries']))} wait for a human decision. Agents cannot clear "
                       "a hold."}
    if open_cases:
        first = min(open_cases, key=lambda c: c["opened_utc"])
        held = [b for b in batteries if first["lot_id"] in b["lots"]]
        return {"text": f"Review the compliance case on {first['lot_id']}", "url": f"/cases/{first['case_id']}",
                "why": f"A sourcing risk still needs a human decision: {batteries_text(len(held))} stay held until a "
                       "Compliance Officer decides. The agents cannot clear this case."}
    requests = [r for r in second_life_rows(service, scope) if r["open"]]
    if requests:
        first = requests[-1]
        return {"text": f"Decide the second-life request for {first['battery_id']}", "url": first["url"],
                "why": f"An operator asks to repurpose battery {first['battery_id']} for a second life. A person "
                       "approves or rejects it; the application then issues the successor passport."}
    if total and valid == total:
        successor = next((b for b in batteries if b["successor"]), None)
        if successor:
            return {"text": f"Open the second-life passport {successor['battery_id']}", "url": successor["public_url"],
                    "why": "Every battery in the selected scope has a valid published passport, and the repurposed "
                           "battery continues under its own successor passport."}
        return {"text": "Open a public passport", "url": batteries[0]["public_url"],
                "why": "Every battery in the selected scope has a valid published passport. Lifecycle events and "
                       "second-life requests appear here when they arrive."}
    working = [b for b in batteries if not b["successor"] and b["status"]["code"] in ("pending", "held",
                                                                                         "published_outdated")]
    if working:
        return {"text": None, "url": None,
                "why": f"No human decision is needed right now. The agents are still working on "
                       f"{batteries_text(len(working))}; this page shows the result as soon as they finish."}
    return {"text": None, "url": None, "why": "No human decision is needed right now."}


def series_view(service, series_id=None, prices=None, scope=None):
    scope = scope or production_scope(service, series_id)
    now = now_of(service)
    with service.store.read() as tx:
        if not scope.produced:
            return {"empty": True, "series_id": scope.series_id}
        battery_ids = sorted(scope.batteries)
        states = [s for s in (tx.get("PassportState", b) for b in battery_ids) if s]
        used = {lot for s in states for lot in domain.split_ids(s["lot_ids"])}
        lots = sorted(lot for lot in used if tx.select("Evidence", lot_id=lot) or tx.select("Reviews", lot_id=lot))
        evidence = {lot: tx.select("Evidence", lot_id=lot) for lot in lots}
        supplier_facts = {lot: sorted(tx.select("AgentFacts", kind="supplier", subject=lot),
                                      key=lambda f: f["recorded_utc"]) for lot in lots}
        reviews = [r for lot in lots for r in tx.select("Reviews", lot_id=lot)]
        subjects = set(lots) | scope.batteries
        tasks = [t for t in tx.select("AgentTasks") if t["subject"] in subjects]
        results = {t["task_id"]: tx.get("AgentResults", t["task_id"]) for t in tasks}
        carbon = {b: [f for f in tx.select("AgentFacts", kind="carbon", subject=b)] for b in battery_ids}
        receipts = {s["battery_id"]: service._views(tx, s) for s in states}
        open_cases = [c for lot in sorted(used) for c in tx.select("ComplianceCases", lot_id=lot)
                      if c["status"] == "open"]
    every = service.report_rows()
    report = [r for r in every if r["battery_id"] in battery_ids]
    measured = metrics.measures(report, [r for r in service.lot_claim_rows() if r["lot_id"] in used])
    batteries = []
    for state in states:
        status = battery_status(state)
        facts = sorted(carbon[state["battery_id"]], key=lambda f: f["recorded_utc"])
        current = [json.loads(f["data_json"]) for f in facts]
        current = [d for d in current if d.get("input_hash") == state["input_hash"]]
        version_row, _ = receipts[state["battery_id"]]
        batteries.append({
            "battery_id": state["battery_id"], "model_id": state["model_id"], "status": status,
            "successor": state["battery_id"] not in scope.produced,
            "lots": domain.split_ids(state["lot_ids"]),
            "version": version_row["version"] if version_row else None,
            "published_when": when(version_row["committed_utc"]) if version_row else "",
            "carbon": per_kwh(current[-1]["kg_co2e_per_kwh"]) if current else "not calculated yet",
            "public_url": f"/p/{state['battery_id']}", "trail_url": f"/trail/{state['battery_id']}"})
    produced = [b for b in batteries if not b["successor"]]
    total = len(produced)
    valid = sum(1 for b in produced if b["status"]["code"] == "published")
    published = sum(1 for b in produced if b["status"]["code"] in ("published", "published_outdated"))
    held = sum(1 for b in produced if b["status"]["code"] == "held")
    open_reviews = sorted((r for r in reviews if r["status"] == "open"), key=lambda r: r["opened_utc"])
    holds = []
    for review in open_reviews:
        minutes = minutes_between(review["opened_utc"], now)
        affected = [b["battery_id"] for b in batteries if review["lot_id"] in b["lots"]]
        holds.append({"review_id": review["review_id"], "lot_id": review["lot_id"], "batteries": affected,
                      "opened_by": opened_by_text(review["opened_by"]), "actor_role": actor_role(review["opened_by"]),
                      "age_minutes": minutes, "age": age_text(minutes), "url": f"/holds/{review['review_id']}"})
    lot_rows = []
    for lot in lots:
        open_review = next((h for h in holds if h["lot_id"] == lot), None)
        lot_batteries = [b for b in batteries if lot in b["lots"]]
        lot_rows.append({"lot_id": lot, "url": f"/lots/{lot}", "evidence": lot_evidence_state(evidence[lot]),
                         "supplier": supplier_state(supplier_facts[lot], evidence[lot]),
                         "hold": open_review,
                         "batteries": [b["battery_id"] for b in lot_batteries],
                         "battery_links": [{"battery_id": b["battery_id"], "url": b["public_url"]}
                                           for b in lot_batteries]})
    runs = sorted((run_record(t, results[t["task_id"]], prices) for t in tasks), key=lambda r: (r["at"], r["task_id"]))
    totals = run_totals(runs, prices, published)
    action = next_action(service, scope, batteries, holds, open_cases, valid, total)
    return {
        "empty": False, "series_id": scope.series_id, "series": series_label(scope.series_id),
        "scope_label": scope.label, "now": when(now),
        "batteries": batteries, "lots": lot_rows, "holds": holds, "runs": totals, "action": action,
        "progress": {"published": published, "valid": valid, "total": total, "held": held,
                     "text": f"{published} of {total} published",
                     "percent": round(100 * valid / total) if total else 0,
                     "coverage": f"{round(100 * valid / total)}%" if total else "0%",
                     "coverage_text": f"{valid} valid passport{'s' if valid != 1 else ''} of {total} batteries in "
                                      "scope (held batteries count)"},
        "carbon_label": CARBON_LABEL,
        "measures": measured, "tiles": measure_tiles(measured), "plants": plant_rows(measured),
        "cases_open": len(open_cases),
        "cases": [{"lot_id": c["lot_id"], "url": f"/cases/{c['case_id']}",
                   "batteries": [b["battery_id"] for b in batteries if c["lot_id"] in b["lots"]]}
                  for c in open_cases],
        "oldest_case_minutes": max((minutes_between(c["reopened_utc"] or c["opened_utc"], now) for c in open_cases),
                                   default=0),
    }


def percent_text(value):
    return NOT_MEASURED if value is None else f"{value:g}%"


def measure_tiles(measured):
    """The four success measures (metrics.py) as executive tiles."""
    cover, footprint = measured["coverage"], measured["verified_footprint"]
    supply, second = measured["supplier_completeness"], measured["second_life"]
    average = footprint["avg_kg_co2e_per_kwh"]
    return [
        {"title": "Passport coverage", "value": percent_text(cover["coverage_pct"]), "label": "synthetic",
         "text": f"{cover['published']} of {cover['produced']} produced batteries have a published passport "
                 "(second-life successors excluded)"},
        {"title": "Verified footprint", "value": f"{footprint['verified']} of {footprint['published']}",
         "label": "illustrative",
         "text": "published batteries calculated with every critical-lot claim verified; average "
                 + (per_kwh(average) if average is not None else NOT_MEASURED)},
        {"title": "Supplier data completeness", "value": percent_text(supply["complete_pct"]), "label": "synthetic",
         "text": f"{supply['complete']} of {supply['lots']} lots have every required field and a verified "
                 "signed claim"},
        {"title": "Second life", "value": str(second["repurposed"]), "label": "illustrative",
         "text": f"repurposed batter{'y' if second['repurposed'] == 1 else 'ies'}, illustrative value "
                 f"{second['value_eur_illustrative']:,.0f} EUR"},
    ]


def plant_rows(measured):
    return [{**p, "coverage": percent_text(p["coverage_pct"]),
             "footprint": per_kwh(p["avg_footprint_per_kwh"]) if p["avg_footprint_per_kwh"] is not None
             else NOT_MEASURED} for p in measured["plants"]]


def nav_view(service, series=None, battery=None, lot=None, scope=None):
    """Staff sidebar and counts use the same scope as the page."""
    scope = scope or production_scope(service, series, battery, lot)
    now = now_of(service)
    with service.store.read() as tx:
        states = {s["battery_id"]: s for s in tx.select("PassportState")}
        rows = [states[b] for b in sorted(scope.batteries) if b in states]
        reviews = [r for r in tx.select("Reviews") if scope.includes(r["lot_id"], "lots")]
    batteries = []
    for state in rows:
        status = battery_status(state)
        batteries.append({"battery_id": state["battery_id"], "status": SHORT_STATUS[status["code"]],
                          "tone": status["tone"], "public_url": f"/p/{state['battery_id']}",
                          "trail_url": f"/trail/{state['battery_id']}"})
    produced = [s for s in rows if s["battery_id"] in scope.produced]
    valid = sum(1 for s in produced if battery_status(s)["code"] == "published")
    open_reviews = sorted((r for r in reviews if r["status"] == "open"), key=lambda r: r["opened_utc"])
    resolved = sorted((r for r in reviews if r["status"] != "open"),
                      key=lambda r: r["resolved_utc"] or r["opened_utc"], reverse=True)[:3]
    holds = [{"review_id": r["review_id"], "lot_id": r["lot_id"], "url": f"/holds/{r['review_id']}", "tone": "warn",
              "text": f"Open · {age_text(minutes_between(r['opened_utc'], now))}"} for r in open_reviews]
    holds += [{"review_id": r["review_id"], "lot_id": r["lot_id"], "url": f"/holds/{r['review_id']}", "tone": "good",
               "text": "Resolved"} for r in resolved]
    return {"series_id": scope.series_id, "series": scope.label if rows else None,
            "readiness_url": "/exec",
            "batteries": batteries, "holds": holds, "open_holds": len(open_reviews),
            "progress": {"text": f"{valid}/{len(produced)}",
                         "label": f"{valid} of {len(produced)} batteries have a valid passport"} if rows else None}


def series_snapshot(service, series_id=None):
    """Numbers for the passport.series.snapshot event. holds_open counts held batteries."""
    now = now_of(service)
    view = series_view(service, series_id)
    if view["empty"]:
        return None
    with service.store.read() as tx:
        outbox = [o for o in tx.select("Outbox") if o["status"] in ("pending", "processing")]
        tasks = [t for t in tx.select("AgentTasks")
                 if t["subject"] in {subject for b in view["batteries"] for subject in (b["battery_id"], *b["lots"])}]
    latest_work = {}
    for task in tasks:
        for battery in view["batteries"]:
            if battery["successor"] or task["subject"] not in (battery["battery_id"], *battery["lots"]):
                continue
            latest_work[battery["battery_id"]] = max(
                task["created_utc"], latest_work.get(battery["battery_id"], task["created_utc"]))
    stuck_batteries = sum(
        battery["status"]["code"] == "pending" and battery["battery_id"] in latest_work and
        minutes_between(latest_work[battery["battery_id"]], now) > 15
        for battery in view["batteries"])
    oldest_outbox = max((minutes_between(o["created_utc"], now) for o in outbox), default=0)
    measured = view["measures"]
    return {"series_id": view["series_id"], "total_batteries": view["progress"]["total"],
            "published_valid": view["progress"]["valid"], "holds_open": view["progress"]["held"],
            "stuck_batteries": stuck_batteries,
            "oldest_hold_minutes": max((h["age_minutes"] for h in view["holds"]), default=0),
            "outbox_pending": len(outbox), "oldest_outbox_minutes": oldest_outbox,
            "coverage_pct": measured["coverage"]["coverage_pct"],
            "verified_footprint_batteries": measured["verified_footprint"]["verified"],
            "supplier_completeness_pct": measured["supplier_completeness"]["complete_pct"],
            "second_life_count": measured["second_life"]["repurposed"],
            "second_life_value_eur_illustrative": measured["second_life"]["value_eur_illustrative"],
            "cases_open": view["cases_open"], "oldest_case_minutes": view["oldest_case_minutes"],
            "holds": view["holds"], "plants": measured["plants"]}


# ---- public passport (/p/{id}) ---------------------------------------------------------------------
def public_ledger(last):
    """Only a result staff already observed for this exact version; anonymous requests never run SQL."""
    if not last or last.get("status") not in ("verified", "chain_verified_anchor_pending", "failed"):
        return {"tone": "info", "text": "Ledger check not shown: this version has not been verified recently"}
    moment = when(last.get("checked_utc"))
    texts = {"verified": f"Ledger verified at {moment}",
             "chain_verified_anchor_pending": f"Ledger chain verified at {moment}; this version awaits the next anchor",
             "failed": f"Ledger verification failed at {moment}"}
    return {"tone": LEDGER_TONES[last["status"]], "text": texts[last["status"]]}


def public_page(service, battery_id, ledger=None, policy=None):
    """The public panel. policy: access.project() for the caller; its identity and footprint fill the facts."""
    view = service.public_passport(battery_id)
    if view is None:
        return None
    if view["passport_status"] != "committed":
        return {"battery_id": view["battery_id"], "model_id": view["model_id"], "issued": False,
                "status": {"text": PUBLIC_NOT_ISSUED, "tone": "warn"}}
    last = ledger.last(battery_id, view["passport_version"]) if ledger else None
    closed = view["closed"]
    return {
        "battery_id": view["battery_id"], "model_id": view["model_id"], "issued": True,
        "status": {"text": lifecycle.status_label(view["lifecycle_status"]), "tone": "info"} if closed
        else {"text": "Published", "tone": "good"},
        "lifecycle": {"status": lifecycle.status_label(view["lifecycle_status"]), "closed": closed,
                      "links": [{**link, "url": f"/p/{link['battery_id']}",
                                 "text": LINK_TEXT.get(link["link_type"], link["link_type"])}
                                for link in view["links"]]},
        "version": view["passport_version"], "published_when": when(view["committed_utc"]),
        "stored_copy": "Written" if view["snapshot_state"] == "written" else "Being written",
        "ledger": public_ledger(last),
        "snapshot_sha256": view["snapshot_sha256"],
        "facts": public_facts(policy) if policy else [],
    }


def number_text(value, unit):
    return NOT_MEASURED if value is None else f"{value:g} {unit}"


def recycled_text(shares):
    parts = [f"{name} {value:g} %" for name, value in shares.items() if value is not None]
    return ", ".join(parts) or NOT_MEASURED


def public_facts(policy):
    """Identity and footprint tiles every visitor sees (access.PUBLIC)."""
    identity, footprint = policy["identity"], policy["footprint"]
    value = footprint["kg_co2e_per_kwh"]
    return [("Chemistry", identity["chemistry"] or NOT_MEASURED),
            ("Rated capacity", number_text(identity["rated_kwh"], "kWh")),
            ("Carbon footprint (illustrative)", per_kwh(value) if value is not None else NOT_MEASURED),
            ("Recycled content", recycled_text(footprint["recycled_content_pct"])),
            ("Lifecycle status", identity["lifecycle_status_label"])]


def _table(caption, head, rows):
    return {"caption": caption, "head": head, "rows": rows}


def policy_sections(policy):
    """The groups beyond the public view, as page sections, in policy order. Evidence shows references only."""
    sections = []
    for group in policy["field_groups"]:
        data, facts, tables = policy[group], [], []
        if group in ("identity", "footprint"):
            continue
        if group == "performance":
            facts = [("Current holder", data["holder_id"] or "-"),
                     ("Latest state of health", number_text(data["latest_soh_pct"], "%")),
                     ("Rated capacity", number_text(data["rated_capacity_ah"], "Ah")),
                     ("Expected cycles", str(data["expected_cycles"] or NOT_MEASURED))]
            if data["inherited_from"]:
                facts.append(("Inherited from", data["inherited_from"]))
            tables = [_table("State-of-health history", ["Measured", "State of health", "Source"],
                             [[moment(r["measured_utc"]), f"{r['soh_pct']:g} %", r["source"]]
                              for r in data["soh_history"]])]
        elif group == "second_life":
            request = data["request_status"]
            screen = {True: "Meets threshold", False: "Below threshold", None: "No measurement recorded"}
            facts = [("Health screen (not an approval)", screen[data["meets_health_screen"]]),
                     ("Minimum state of health", number_text(data["minimum_soh_pct"], "%")),
                     ("Health at request" if request else "Measured health", number_text(data["soh_pct"], "%")),
                     ("Measured on", moment(data["measured_utc"]) if data["measured_utc"] else NOT_MEASURED),
                     ("Reviewer decision", {"pending": "Awaiting reviewer", "approved": "Approved",
                                            "rejected": "Rejected", None: "No request recorded"}[request]),
                     ("Passport lifecycle", lifecycle.status_label(data["lifecycle_status"]))]
            if data["inherited_from"]:
                facts.insert(0, ("Decision inherited from", data["inherited_from"]))
            if request:
                facts.append(("Safety assessment reference", "Supplied (not a certification)"
                              if data["safety_reference_supplied"] else "Not supplied"))
                if data["decided_utc"]:
                    facts.append(("Decision date", moment(data["decided_utc"])))
                facts += [("Estimated recovery value", f"{money(data['illustrative_value_eur'], 'EUR')} "
                           f"({data['value_label']})"),
                          ("Scenario calculation", f"Rated kWh x EUR {data['eur_per_rated_kwh']:g} per rated kWh")]
        elif group == "warranty":
            trend = data["soh_trend"]
            facts = [("Physical pack (original reading source)", data["physical_battery_id"]),
                     ("Service delivery", data["delivery_id"] or "Unavailable"),
                     ("As of (review only, not a claim decision)", f"{data['as_of_utc']} · {data['feed_status']}"),
                     ("In service", data["in_service_date"] or "Unknown"),
                     ("Applicable terms", data["term_version"] or "Unknown"),
                     ("Time limit", number_text(data["warranty_months"], "months")),
                     ("Limit date", data["warranty_end_date"] or "Unknown"),
                     ("Time review", data["time_status"].replace("_", " ")),
                     ("Design specification", number_text(data["expected_cycles"], "expected cycles")),
                     ("Observed usage", number_text(data["actual_cycles"], "equivalent full cycles")),
                     ("Measured on", data["measured_utc"] or "Unknown"),
                     ("Cycle limit", number_text(data["warranty_cycles"], "EFC")),
                     ("Usage review", data["cycle_status"].replace("_", " ")),
                     ("Comparable health trend", (f"{trend['from_pct']:g}% on {trend['from_utc']} to "
                       f"{trend['to_pct']:g}% on {trend['to_utc']} ({trend['change_percentage_points']:+g} pp)")
                       if trend else "Insufficient dated observations")]
        elif group == "recall":
            facts = [("Physical pack", data["physical_battery_id"]), ("Original batch", data["batch_id"]),
                     ("Campaign feed", data["feed_status"]), ("As of", data["as_of_utc"] or "Unavailable"),
                     ("Exposure", data["exposure"].replace("_", " "))]
            tables = [_table("Matching campaigns at last delivery", ["Campaign", "Kind", "Status", "Effective"],
                             [[r["campaign_id"], r["kind"].replace("_", " "), r["status"], r["effective_date"]]
                              for r in data["campaigns"]])]
        elif group == "composition":
            facts = [("Dismantling (synthetic)", data["dismantling"]), ("Safety (synthetic)", data["safety"])]
            tables = [_table("Mass per material", ["Material", "Mass"],
                             [[m["material"], f"{m['mass_kg']:g} kg"] for m in data["materials"]])]
        elif group == "calculation":
            facts = [("Method version", data["method_version"] or "-"),
                     ("Factor set version", data["factor_set_version"] or "-"),
                     ("Allocation rule", data["allocation_rule"] or "-"), ("Batch", data["batch_id"] or "-"),
                     ("Grid factor", data["grid_factor_id"] or "-"), ("Input hash", data["input_hash"] or "-")]
        elif group == "supply_chain":
            tables = [_table("Lots and suppliers", ["Lot", "Supplier", "Material", "Region", "Origin site"],
                             [[r["lot_id"], r["supplier_id"], r["material"], r["region"], r["origin_site"]]
                              for r in data["lots"]])]
        elif group == "due_diligence":
            tables = [_table("Critical lots", ["Lot", "Material", "Signed claim", "Sourcing review",
                                               "Compliance case"],
                             [[r["lot_id"], r["material"], r["claim_status"], r["sourcing_review"],
                               r["case_state"] if not r["case_id"] else f"{r['case_id']} ({r['case_state']})"]
                              for r in data["critical_lots"]])]
        elif group == "ledger":
            tables = [_table("Passport versions", ["Version", "Committed", "Decision", "Stored copy", "SHA-256"],
                             [[str(v["version"]), moment(v["committed_utc"]), v["decision"], v["snapshot_state"],
                               v["snapshot_sha256"]] for v in data["versions"]]),
                      _table("Lifecycle events", ["Event", "Issuer", "Result", "Version"],
                             [[e["event_type"].replace("_", " "), e["issuer"], e["result"],
                               str(e["passport_version"] or "-")] for e in data["lifecycle_events"]])]
        elif group == "decisions":
            rows = [["Sourcing review", r["review_id"], r["status"], r["resolution_reason"] or
                     (f"AI-generated: {r['reason']}" if r["reason_origin"] == "ai" else r["reason"])]
                    for r in data["sourcing_reviews"]]
            rows += [["Compliance", d["case_id"], d["decision"], d["reason"]] for d in data["compliance"]]
            rows += [["Second life", r["request_id"], r["status"], r["reason"] or "-"] for r in data["second_life"]]
            tables = [_table("Decisions with reasons", ["Kind", "Record", "Outcome", "Reason"], rows)]
        elif group == "evidence":
            scope = "compliance-case evidence only" if data["scope"] == "compliance_case" else "all lot evidence"
            facts = [("Scope", f"References only ({scope}); file content is not shown here")]
            tables = [_table("Evidence references", ["Evidence", "Kind", "Lot", "SHA-256"],
                             [[r["evidence_id"], r["kind"], r["lot_id"], r["sha256"]] for r in data["references"]])]
        sections.append({"key": group, "title": access.TITLES[group], "facts": facts,
                         "tables": [t for t in tables if t["rows"]]})
    return sections


LINK_TEXT = {"successor": "Second-life successor passport", "predecessor": "Predecessor passport"}


def viewer_passports(service, scope=None):
    """Sidebar for passport viewers, including successors in the selected scope."""
    with service.store.read() as tx:
        states = sorted((s for s in tx.select("PassportState")
                         if scope is None or scope.includes(s["battery_id"])), key=lambda s: s["battery_id"])
    rows = []
    for state in states:
        status = battery_status(state)
        rows.append({"battery_id": state["battery_id"], "url": f"/p/{state['battery_id']}", "tone": status["tone"],
                     "status": SHORT_STATUS[status["code"]]})
    return rows

def qr_svg(url, scale=4):
    import segno
    from markupsafe import Markup

    # omitsize writes a viewBox instead of a fixed size, so CSS can scale the code without clipping it.
    return Markup(segno.make(url, error="m").svg_inline(scale=scale, title=f"QR code for {url}", svgclass="qr",
                                                        omitsize=True))


# ---- hold and review (/holds/{id}) ----------------------------------------------------------------
FIELDS = [("Origin site", "origin_site", "loaded_at_site"), ("Material", "material", "material"),
          ("Quantity (kg)", "quantity_kg", "quantity_kg")]


def readable_json(service, evidence_id):
    """Small, intact JSON only. Anything else is listed by name, never rendered."""
    row, data = service.evidence_content(evidence_id)
    if not row or data is None or domain.sha256(data) != row["sha256"]:
        return None
    if row["content_type"] != "application/json" or len(data) > MAX_JSON_EVIDENCE_BYTES:
        return None
    try:
        value = json.loads(data)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def comparison(claim, transport, correction):
    rows = []
    for label, claim_key, transport_key in FIELDS:
        values = [str(claim.get(claim_key, "")) if claim else "",
                  str(transport.get(transport_key, "")) if transport else "",
                  str(correction.get(claim_key, "")) if correction else ""]
        differs = bool(values[0] and values[1] and values[0].strip().lower() != values[1].strip().lower())
        fixed = bool(correction and values[2] and values[1] and values[2].strip().lower() == values[1].strip().lower())
        rows.append({"label": label, "claim": values[0] or "-", "transport": values[1] or "-",
                     "correction": values[2] or "-", "differs": differs, "fixed": fixed})
    return rows


def hold_view(service, review_id, can_resolve, scope=None):
    with service.store.read() as tx:
        review = tx.get("Reviews", review_id)
        if not review:
            return None
        lot = review["lot_id"]
        rows = sorted(tx.select("Evidence", lot_id=lot), key=lambda r: (r["uploaded_utc"], r["evidence_id"]))
        states = [s for s in tx.select("PassportState") if lot in domain.split_ids(s["lot_ids"])
                  and (scope is None or scope.includes(s["battery_id"]))]
    now = now_of(service)
    opening = set(domain.split_ids(review["opening_evidence"]))
    resolution = set(domain.split_ids(review["resolution_evidence"]))
    documents = [{"evidence_id": r["evidence_id"], "file_name": r["file_name"], "kind": r["kind"].replace("_", " "),
                  "uploaded": when(r["uploaded_utc"]), "opening": r["evidence_id"] in opening,
                  "used_to_resolve": r["evidence_id"] in resolution} for r in rows]
    candidates = [d for d in documents if not d["opening"]]
    agent_opened = (review["opened_by"] or "").startswith("agent:")
    origin = review["reason_origin"] or ("ai" if agent_opened else "person")
    view = {
        "review_id": review_id, "lot_id": lot, "open": review["status"] == "open", "row_version": review["row_version"],
        "batteries": [{"battery_id": s["battery_id"], "status": battery_status(s)} for s in states],
        "opened_by": opened_by_text(review["opened_by"]), "opened_when": when(review["opened_utc"]),
        "age": age_text(minutes_between(review["opened_utc"], review["resolved_utc"] or now)),
        "reason": review["reason"], "reason_ai": origin == "ai", "provenance_id": review["provenance_id"] or "",
        "reason_label": REASON_LABELS.get(origin, "Reason given"),
        "documents": documents, "candidates": candidates, "can_resolve": can_resolve,
        "resolved_by": person(review["resolved_by"]) if review["resolved_by"] else "",
        "resolved_when": when(review["resolved_utc"]), "resolution_reason": review["resolution_reason"] or "",
        "comparison": None,
    }
    if not can_resolve:
        return view  # an Operator sees which documents exist, never their content
    by_kind = lambda kind, pool: next((r for r in reversed(pool) if r["kind"] == kind), None)  # noqa: E731
    opening_rows = [r for r in rows if r["evidence_id"] in opening] or rows
    claim_row, transport_row = by_kind("supplier_claim", opening_rows), by_kind("transport_record", opening_rows)
    later = [r for r in rows if r["evidence_id"] not in opening]
    correction_row = by_kind("corrected_claim", [r for r in rows if r["evidence_id"] in resolution] or later)
    claim = readable_json(service, claim_row["evidence_id"]) if claim_row else None
    transport = readable_json(service, transport_row["evidence_id"]) if transport_row else None
    correction = readable_json(service, correction_row["evidence_id"]) if correction_row else None
    view["comparison"] = {"rows": comparison(claim, transport, correction), "has_correction": correction is not None,
                          "claim_name": claim_row["file_name"] if claim_row else "not uploaded",
                          "transport_name": transport_row["file_name"] if transport_row else "not uploaded",
                          "correction_name": correction_row["file_name"] if correction_row else "",
                          "unreadable": [n for n, row, value in (("claim", claim_row, claim),
                                                                  ("transport record", transport_row, transport))
                                         if row and value is None]}
    return view


# ---- supplier lot (/lots/{id}) ----------------------------------------------------------------------
def lot_view(service, lot_id, can_read, scope=None):
    with service.store.read() as tx:
        rows = sorted(tx.select("Evidence", lot_id=lot_id), key=lambda r: (r["uploaded_utc"], r["evidence_id"]))
        states = [s for s in tx.select("PassportState") if lot_id in domain.split_ids(s["lot_ids"])
                  and (scope is None or scope.includes(s["battery_id"]))]
        reviews = sorted(tx.select("Reviews", lot_id=lot_id), key=lambda r: r["opened_utc"], reverse=True)
        cases = sorted(tx.select("ComplianceCases", lot_id=lot_id), key=lambda c: c["opened_utc"], reverse=True)
        facts = sorted(tx.select("AgentFacts", kind="supplier", subject=lot_id), key=lambda f: f["recorded_utc"])
    if not rows and not states and not reviews:
        return None
    return {
        "lot_id": lot_id, "evidence": lot_evidence_state(rows), "supplier": supplier_state(facts, rows),
        "can_read": can_read,
        "documents": [{"evidence_id": r["evidence_id"], "file_name": r["file_name"],
                       "kind": r["kind"].replace("_", " "), "uploaded": when(r["uploaded_utc"]),
                       "sha256": r["sha256"], "integrity": service.verify_evidence(r["evidence_id"]),
                       "read_url": f"/evidence/{r['evidence_id']}/content" if can_read else None} for r in rows],
        "batteries": [{"battery_id": s["battery_id"], "status": battery_status(s),
                       "trail_url": f"/trail/{s['battery_id']}", "public_url": f"/p/{s['battery_id']}"}
                      for s in sorted(states, key=lambda s: s["battery_id"])],
        "holds": [{"review_id": r["review_id"], "url": f"/holds/{r['review_id']}", "open": r["status"] == "open",
                   "opened_when": when(r["opened_utc"]), "opened_by": opened_by_text(r["opened_by"])}
                  for r in reviews],
        "cases": [{"case_id": c["case_id"], "url": f"/cases/{c['case_id']}",
                   "state": CASE_STATES.get(c["status"], (c["status"], "info"))[0],
                   "tone": CASE_STATES.get(c["status"], (c["status"], "info"))[1]} for c in cases],
        "labels": ["Synthetic data", "Illustrative"],
    }


# ---- compliance cases (/cases, /cases/{id}) -------------------------------------------------------
CASE_STATES = {"open": ("Open - batteries held", "warn"), "accepted": ("Accepted with an action plan", "good")}
CASE_OPENED = {"agent_escalation": "Supplier agent escalation", "compliance_user": "Opened by Compliance",
               "new_adverse_report": "Reopened: new adverse report"}


def case_rows(service, scope=None):
    now = now_of(service)
    rows = []
    for case in reversed(service.list_cases()):
        if scope is not None and not scope.includes(case["lot_id"], "lots"):
            continue
        text, tone = CASE_STATES.get(case["status"], (case["status"], "info"))
        rows.append({"case_id": case["case_id"], "lot_id": case["lot_id"], "url": f"/cases/{case['case_id']}",
                     "state": text, "tone": tone, "open": case["status"] == "open",
                     "opened_when": when(case["opened_utc"]), "opened_by": opened_by_text(case["opened_by"]),
                     "age": age_text(minutes_between(case["reopened_utc"] or case["opened_utc"],
                                                     case["decided_utc"] if case["status"] != "open" else now))})
    return rows


def case_view(service, case_id, can_decide, scope=None):
    detail = service.case_detail(case_id)
    if detail is None:
        return None
    case = detail["case"]
    opening = set(domain.split_ids(case["opening_evidence"]))
    used = {e for d in detail["decisions"] for e in domain.split_ids(d["evidence_ids"])}
    documents = [{"evidence_id": e["evidence_id"], "file_name": e["file_name"], "kind": e["kind"].replace("_", " "),
                  "uploaded": when(e["uploaded_utc"]), "opening": e["evidence_id"] in opening,
                  "used": e["evidence_id"] in used}
                 for e in sorted(detail["evidence"], key=lambda r: (r["uploaded_utc"], r["evidence_id"]))]
    names = {d["evidence_id"]: d["file_name"] for d in documents}
    decisions = [{"decision": "Accepted with an action plan" if d["decision"] == "accept_with_action_plan"
                  else "Kept the hold", "tone": "good" if d["decision"] == "accept_with_action_plan" else "warn",
                  "by": person(d["decided_by"]), "when": when(d["decided_utc"]), "reason": d["reason"],
                  "owner": d["action_owner"] or "", "due_date": d["due_date"] or "",
                  "evidence": [names.get(e, "unknown file") for e in domain.split_ids(d["evidence_ids"])]}
                 for d in detail["decisions"]]
    proposal = detail["proposal"]
    text, tone = CASE_STATES.get(case["status"], (case["status"], "info"))
    return {
        "case_id": case_id, "lot_id": case["lot_id"], "open": case["status"] == "open", "state": text, "tone": tone,
        "row_version": case["row_version"], "opened_by": opened_by_text(case["opened_by"]),
        "opened_when": when(case["opened_utc"]), "opened_reason": CASE_OPENED.get(case["reason_code"],
                                                                                  case["reason_code"]),
        "reopen_count": case["reopen_count"], "reopened_when": when(case["reopened_utc"]),
        "owner": case["action_owner"] or "", "due_date": case["due_date"] or "",
        "batteries": [{"battery_id": b["battery_id"], "status": battery_status(_state_like(b))}
                      for b in detail["batteries"] if scope is None or scope.includes(b["battery_id"])],
        "proposal": {"action": (proposal.get("action") or "").replace("_", " "),
                     "reason": proposal.get("reason") or "", "task_id": proposal["task_id"],
                     "reason_ai": (proposal.get("reason_provenance") or {"ai_generated": True})["ai_generated"],
                     "provenance_id": (proposal.get("reason_provenance") or {}).get("provenance_id") or ""}
        if proposal else None,
        "documents": documents, "candidates": [d for d in documents if not d["opening"]],
        "decisions": decisions, "can_decide": can_decide, "labels": ["Synthetic data", "Illustrative"]}


def _state_like(battery):
    return {"committed_version": battery["committed_version"], "committed_input_hash": None, "input_hash": None,
            "eligibility": battery["eligibility"]}


def case_nav(service, scope=None):
    rows = case_rows(service, scope)
    return {"cases": rows[:6], "open_cases": sum(1 for r in rows if r["open"])}


# ---- second-life approvals (/second-life) ----------------------------------------------------------
SECOND_LIFE_STATES = {"pending": ("Awaiting a Reviewer", "warn"), "approved": ("Approved", "good"),
                      "rejected": ("Rejected", "info")}


def second_life_rows(service, scope=None):
    rows = []
    for r in reversed(service.list_second_life()):
        if scope is not None and not scope.includes(r["battery_id"]):
            continue
        text, tone = SECOND_LIFE_STATES.get(r["status"], (r["status"], "info"))
        rows.append({"request_id": r["request_id"], "url": f"/second-life/{r['request_id']}",
                     "battery_id": r["battery_id"], "successor_id": r["successor_id"],
                     "requested_by": r["requested_by"], "state": text, "tone": tone, "open": r["status"] == "pending",
                     "requested_when": moment(r["requested_utc"]), "soh_pct": r["soh_pct"]})
    return rows


def second_life_view(service, request_id, can_decide):
    detail = service.second_life_detail(request_id)
    if detail is None:
        return None
    r, life = detail["request"], detail["lifecycle"]
    text, tone = SECOND_LIFE_STATES.get(r["status"], (r["status"], "info"))
    return {
        "request_id": r["request_id"], "battery_id": r["battery_id"], "successor_id": r["successor_id"],
        "requested_by": r["requested_by"], "requested_when": moment(r["requested_utc"]),
        "safety_ref": r["safety_assessment_ref"], "intended_use": r["intended_use"], "soh_pct": r["soh_pct"],
        "minimum_soh_pct": detail["minimum_soh_pct"], "value": money(r["illustrative_value_eur"], "EUR"),
        "value_label": r["value_label"], "value_basis": detail["value_basis"], "state": text, "tone": tone,
        "open": r["status"] == "pending", "row_version": r["row_version"], "can_decide": can_decide,
        "decision_reason": r["decision_reason"] or "", "decided_by": person(r["decided_by"]) if r["decided_by"] else "",
        "decided_when": moment(r["decided_utc"]) if r["decided_utc"] else "",
        "holder": life["holder_id"], "status": lifecycle.status_label(life["status"]),
        "links": [{**link, "url": f"/p/{link['battery_id']}", "text": LINK_TEXT.get(link["link_type"], "")}
                  for link in life["links"]],
        "labels": ["Synthetic data", "Illustrative value"]}


# ---- decision trail and integrity (/trail/{id}) ---------------------------------------------------
def trail_steps(service, battery_id, prices):
    with service.store.read() as tx:
        state = tx.get("PassportState", battery_id)
        if not state:
            return None, None
        lots = domain.split_ids(state["lot_ids"])
        subjects = set(lots) | {battery_id}
        tasks = [t for t in tx.select("AgentTasks") if t["subject"] in subjects]
        results = {t["task_id"]: tx.get("AgentResults", t["task_id"]) for t in tasks}
        reviews = [r for lot in lots for r in tx.select("Reviews", lot_id=lot)]
        versions = tx.select("PassportVersions", battery_id=battery_id)
        evidence_rows = {r["evidence_id"]: r for lot in lots for r in tx.select("Evidence", lot_id=lot)}
        evidence = {key: r["file_name"] for key, r in evidence_rows.items()}
        messages = [m for subject in sorted(subjects) for m in tx.select("ConnectorMessages", subject=subject)]
        agreements = {key: tx.get("ConnectorAgreements", key)
                      for key in {m["agreement_id"] for m in messages if m["agreement_id"]}}
        cases = [c for lot in lots for c in tx.select("ComplianceCases", lot_id=lot)]
        case_ids = {c["case_id"] for c in cases}
        decisions = [d for c in cases for d in tx.select("ComplianceDecisions", case_id=c["case_id"])]
        reopens = [a for a in tx.select("AuditEvents", action="compliance_case_reopened") if a["subject"] in case_ids]
        life_events = tx.select("LifecycleEvents", battery_id=battery_id)
        requests = tx.select("SecondLifeRequests", battery_id=battery_id)
        package = tx.get("Packages", state["build_id"]) if state["build_id"] else None
        row = tx.get("PackageBatteries", state["build_id"], battery_id) if package else None
        batch = tx.get("Batches", state["build_id"], row["batch_id"]) if row and row["batch_id"] else None
        parts = len(tx.select("PackageComposition", build_id=state["build_id"], battery_id=battery_id)) \
            if package else 0
        links = tx.select("PassportLinks", battery_id=battery_id)
    steps = lifecycle_steps(life_events, requests) + link_steps(links)
    if package and row:
        steps += package_steps(package, row, batch, parts, len(lots))
    for case in cases:
        agent_opened = (case["opened_by"] or "").startswith("agent:")
        steps.append({"kind": "case", "actor": "Supplier agent (escalated)" if agent_opened else "Compliance",
                      "at": case["opened_utc"], "when": when(case["opened_utc"]),
                      "text": f"Compliance case {case['case_id']} opened for {case['lot_id']}", "short": "case opened",
                      "tone": "warn", "label": "Compliance case", "reviewer": "" if agent_opened else
                      person(case["opened_by"]), "reason": case["reason_code"].replace("_", " "),
                      "case_id": case["case_id"]})
    for audit in reopens:
        try:
            code = json.loads(audit["detail"] or "{}").get("reason_code", "")
        except ValueError:
            code = ""
        steps.append({"kind": "case", "actor": "Application rule", "at": audit["event_utc"],
                      "when": when(audit["event_utc"]), "text": f"Compliance case {audit['subject']} reopened",
                      "short": "case reopened", "tone": "warn", "label": "Compliance case", "reviewer": "",
                      "reason": code.replace("_", " "), "case_id": audit["subject"]})
    for decision in decisions:
        accepted = decision["decision"] == "accept_with_action_plan"
        plan = f"; action plan owner {decision['action_owner']}, due {decision['due_date']}" if accepted else ""
        steps.append({"kind": "human", "actor": "Compliance", "at": decision["decided_utc"],
                      "when": when(decision["decided_utc"]),
                      "text": ("Accepted with an action plan - checks re-ran; nothing was published by this step"
                               if accepted else "Kept the hold") + plan,
                      "short": "accepted" if accepted else "kept hold", "tone": "good" if accepted else "warn",
                      "label": "Compliance decision", "reviewer": person(decision["decided_by"]),
                      "reason": decision["reason"],
                      "evidence": [evidence.get(e, "unknown file") for e in domain.split_ids(decision["evidence_ids"])],
                      "case_id": decision["case_id"]})
    for message in messages:
        steps.append(connector_step(message, agreements.get(message["agreement_id"]),
                                    evidence_rows.get(message["evidence_id"]), is_lot=message["subject"] in lots))
    for task in tasks:
        record = run_record(task, results[task["task_id"]], prices)
        record.update(kind="agent", actor=f"{record['role_title']} agent")
        context = json.loads(task["context_json"])
        record["subject_label"] = "Supplier lot" if task["role"] == "supplier" else "Battery"
        record["subject_url"] = f"/lots/{task['subject']}" if task["role"] == "supplier" else f"/p/{task['subject']}"
        record["sources"] = [
            {"id": e["id"], "name": evidence.get(e["id"], e["id"]), "kind": e["kind"].replace("_", " ")}
            for e in context.get("evidence", [])]
        pinned = context.get("state") or {}
        record["inputs"] = [(label, pinned[key]) for label, key in
                            (("Package", "build_id"), ("Input hash", "input_hash")) if pinned.get(key)]
        event = context.get("event") or {}
        record["inputs"] += [(label, event[key]) for label, key in
                             (("Partner event", "event_id"), ("Event type", "event_type"), ("Sender", "issuer"))
                             if event.get(key)]
        if record["role"] == "issuance" and record["committed_now"]:
            record["text"] = f"Published version {record['committed_version']}"
        steps.append(record)
    for review in reviews:
        if review["opened_by"] in SYSTEM_ACTORS:
            steps.append({"kind": "rule", "actor": SYSTEM_ACTORS[review["opened_by"]], "at": review["opened_utc"],
                          "when": when(review["opened_utc"]), "text": f"Opened hold {review['review_id']}",
                          "short": "opened hold", "tone": "warn", "label": "Automatic check (no AI)",
                          "reason": review["reason"], "reviewer": ""})
        elif not (review["opened_by"] or "").startswith("agent:"):
            steps.append({"kind": "human", "actor": "Person", "at": review["opened_utc"],
                          "when": when(review["opened_utc"]), "text": f"Opened hold {review['review_id']}",
                          "short": "opened hold", "tone": "warn", "label": "Human decision",
                          "reviewer": person(review["opened_by"]), "reason": review["reason"]})
        if review["resolved_utc"]:
            used = [evidence.get(e, "unknown file") for e in domain.split_ids(review["resolution_evidence"])]
            steps.append({"kind": "human", "actor": "Human Reviewer", "at": review["resolved_utc"],
                          "when": when(review["resolved_utc"]), "text": "Resolved the hold - checks re-ran; "
                          "nothing was published by this step", "short": "", "tone": "good",
                          "label": "Human decision", "reviewer": person(review["resolved_by"]),
                          "reason": review["resolution_reason"], "evidence": used,
                          "review_id": review["review_id"]})
    for version in versions:
        if version["decision"] != "agent_publication":
            text = VERSION_TEXT.get(version["decision"], "Published version {}").format(version["version"])
            steps.append({"kind": "rule", "actor": "Application rule", "at": version["committed_utc"],
                          "when": when(version["committed_utc"]), "text": text,
                          "short": "", "tone": "good", "label": "Deterministic rule (no AI)",
                          "reason": version["decision"].replace("_", " "), "reviewer": ""})
    for step in steps:
        step["when"] = when(step["at"], precise=True)
    steps.sort(key=lambda s: (parse_utc(s["at"]), s.get("task_id", "")))
    return state, (steps, versions)


VERSION_TEXT = {"lifecycle_event": "Committed version {} for a lifecycle event",
                "second_life_decision": "Committed version {} after the second-life decision",
                "successor_created": "Created successor passport version {}"}


def connector_step(message, agreement, evidence, *, is_lot):
    """Describe the recorded intake result, not a later agent outcome; never expose the signed payload."""
    result = message["result"]
    status, tone = CONNECTOR_RESULTS.get(result, (result or "Not recorded", "info"))
    kind = CONNECTOR_TYPES.get(message["message_type"], message["message_type"] or "Unknown message type")
    accepted = result in ("verified", "routed", "quarantined")
    checks = ("Signature, sender permissions, audience, validity and agreement accepted at receipt." if accepted
              else "Intake not accepted; see the reason code. Sender and subject are untrusted message claims.")
    handling = {
        "verified": "Stored as lot evidence. Supplier due diligence applies to critical materials only; "
                    "receipt does not clear a sourcing review or compliance case.",
        "routed": "Passed intake gates and routed for Lifecycle assessment. This receipt does not mean the "
                  "event was applied; see the separate Lifecycle agent and application steps.",
        "quarantined": "Stopped by the lifecycle state machine at intake; not routed to an agent or applied.",
        "rejected": "Refused at intake; no evidence or lifecycle action was created by this receipt.",
    }.get(result, "No recognised intake outcome was recorded.")
    details = [
        ("Message type", kind), ("Intake result", status),
        ("Sender ID" if accepted else "Claimed sender ID", message["sender"] or "Not recorded"),
        ("Delivered by", person(message["received_by"]) if message["received_by"] else "Not recorded"),
        ("Intake checks", checks), ("Handling", handling),
        ("Agreement", message["agreement_id"] or "Not recorded"),
    ]
    if agreement:
        details += [("Receiver", agreement["receiver"]), ("Agreement purpose", agreement["purpose"]),
                    ("Agreement valid from", when(agreement["valid_from"], precise=True)),
                    ("Agreement valid until", when(agreement["valid_until"], precise=True))]
    if message["evidence_id"]:
        details.append(("Stored evidence ID", message["evidence_id"]))
        if evidence:
            details += [("Evidence file", evidence["file_name"]),
                        ("Evidence kind", evidence["kind"].replace("_", " "))]
    details += [(label, message[key] or "Not recorded") for label, key in
                (("Receipt ID", "receipt_id"), ("Correlation ID", "correlation_id"),
                 ("Payload SHA-256", "payload_sha256"))]
    reason = (message["reason_code"] or "").replace("_", " ") if message["reason_code"] != "ok" else ""
    return {"kind": "connector", "actor": f"Connector (mock) - {message['sender'] or 'unknown sender'}",
            "at": message["received_utc"], "when": when(message["received_utc"]),
            "text": f"{status}: {kind.lower()} for {message['subject']}",
            "short": "", "tone": tone, "label": "Signed partner message", "reviewer": "", "reason": reason,
            "subject": message["subject"], "subject_label": "Supplier lot" if is_lot else "Battery",
            "subject_url": f"/lots/{message['subject']}" if is_lot else f"/p/{message['subject']}",
            "receipt_id": message["receipt_id"], "message_id": message["message_id"] or "Not recorded",
            "reason_code": message["reason_code"], "details": details,
            "note": "This is a mock exchange, not a live partner integration. A valid signature authenticates "
                    "the message, not the truth of its contents. Delivery identity is separate from the sender."}


def package_steps(package, row, batch, parts, lot_count):
    """Package import (composition and lots) and the deterministic footprint calculation with its versions."""
    at = package["imported_utc"]
    steps = [{"kind": "import", "actor": f"Package import - {person(package['imported_by'])}", "at": at,
              "when": when(at), "text": f"Imported package {package['build_id']}: composition ({parts} parts) and "
                                        f"{lot_count} supplier lots", "short": "", "tone": "info",
              "label": "Composition and package import", "reviewer": "", "reason": "",
              "details": [("Rules version", package["rules_version"] or "-"),
                          ("Manifest SHA-256", package["manifest_sha256"] or "-")]}]
    if row:
        value = row["footprint_per_kwh"]
        steps.append({"kind": "calculation", "actor": "Footprint calculation at import", "at": at, "when": when(at),
                      "text": "Initial footprint (illustrative): " + per_kwh(value) if value is not None else
                              "Import-time footprint unavailable - package inputs or method failed calculation checks",
                      "short": "", "tone": "info" if value is not None else "warn",
                      "label": "Deterministic rule (no AI)", "reviewer": "", "reason": "",
                      "note": "The app calculates automatically at package import, without invoking an agent. "
                              "This step creates no Carbon-agent or independent-verifier evidence. In agent mode, "
                              "the Carbon agent later requests a fresh calculation and independent arithmetic "
                              "check; Issuance requires both before publication.",
                      "details": [("Method version", (batch or {}).get("method_version")
                                   or package["method_version"] or "-"),
                                  ("Factor set version", (batch or {}).get("factor_set_version")
                                   or package["factor_set_version"] or "-"),
                                  ("Allocation rule", (batch or {}).get("allocation_rule") or "-"),
                                  ("Grid factor", row["grid_factor_id"] or "-"),
                                  ("Input hash", row["input_hash"] or "-")]})
    return steps


def link_steps(links):
    return [{"kind": "lifecycle", "actor": "Application rule", "at": r["created_utc"], "when": when(r["created_utc"]),
             "text": f"Linked {LINK_TEXT.get(r['link_type'], r['link_type']).lower()} {r['linked_id']}",
             "short": "link", "tone": "info", "label": "Passport link", "reviewer": "", "reason": ""}
            for r in links]


def lifecycle_steps(events, requests):
    steps = []
    for e in events:
        recorded = e["result"] == "recorded"
        after = (e["state_after"] or "-").replace("_", " ")
        steps.append({"kind": "lifecycle", "actor": f"Lifecycle event - {e['issuer']}", "at": e["recorded_utc"],
                      "when": when(e["recorded_utc"]),
                      "text": (f"Recorded {e['event_type'].replace('_', ' ')}; state {after}" if recorded else
                               f"Quarantined {e['event_type'].replace('_', ' ')} - not applied"),
                      "short": "recorded" if recorded else "quarantined", "tone": "good" if recorded else "warn",
                      "label": "Lifecycle state machine", "reviewer": "",
                      "reason": "" if recorded else e["reason_code"].replace("_", " "),
                      "message_id": e["message_id"], "reason_code": e["reason_code"]})
    for r in requests:
        steps.append({"kind": "lifecycle", "actor": f"Second-life operator - {r['requested_by']}",
                      "at": r["requested_utc"], "when": when(r["requested_utc"]),
                      "text": f"Second-life request {r['request_id']} awaits a Reviewer (safety assessment "
                              f"{r['safety_assessment_ref']})", "short": "request", "tone": "warn",
                      "label": "Second-life request", "reviewer": "", "reason": ""})
        if r["decided_utc"]:
            approved = r["status"] == "approved"
            steps.append({"kind": "human", "actor": "Reviewer (second life)", "at": r["decided_utc"],
                          "when": when(r["decided_utc"]),
                          "text": (f"Approved the second life - successor {r['successor_id']} created, this passport "
                                   "closed" if approved else "Rejected the second life - the passport stays active"),
                          "short": "approved" if approved else "rejected", "tone": "good" if approved else "info",
                          "label": "Human decision", "reviewer": person(r["decided_by"]),
                          "reason": r["decision_reason"]})
    return steps


def chip(step):
    if step["kind"] == "import":
        return "Package import"
    if step["kind"] == "calculation":
        return "Import-time footprint"
    if step["kind"] == "lifecycle":
        return f"Lifecycle ({step['short']})"
    if step["kind"] == "case":
        return f"Compliance ({step['short']})"
    if step["kind"] == "human" and step["actor"] == "Compliance":
        return f"Compliance ({step['short']})"
    if step["kind"] == "human":
        return step["actor"] if step["actor"] in ("Human Reviewer", "Reviewer (second life)") \
            else "Person (opened hold)"
    if step["kind"] == "rule":
        return "Prompt Shields (opened hold)" if step["actor"] in SYSTEM_ACTORS.values() else "Application rule"
    show = step["role"] == "supplier" or step["refused"] or step["code"] in ("deferred", "already_published")
    subject = f"{step['role_title']} - {step['subject']}"
    return f"{subject} ({step['short']})" if show and step["short"] else subject


def trail_view(service, battery_id, prices=None, ledger=None):
    state, data = trail_steps(service, battery_id, prices)
    if state is None:
        return None
    steps, versions = data
    status = battery_status(state)
    current = next((v for v in versions if v["version"] == state["committed_version"]), None)
    cut = current["committed_utc"] if current else None
    path = [s for s in steps if cut is None or parse_utc(s["at"]) <= parse_utc(cut)]
    after = [s for s in steps if cut is not None and parse_utc(s["at"]) > parse_utc(cut)]
    chips = [{"text": chip(s), "tone": s["tone"] if s["kind"] == "agent" else "info", "kind": s["kind"],
              "scripted": s.get("scripted", False), "when": s["when"], "task_id": s.get("task_id")}
             for s in path if s["kind"] != "connector"]
    humans = any(s["kind"] == "human" and s["actor"] != "Reviewer (second life)" for s in path)
    if current and not humans:
        autonomy = ("No person was needed: the agents proposed, the application checked every proposal, and the "
                    "Issuance agent published within its autonomy level.")
    elif current:
        autonomy = ("A person decided the hold. Publication still came from the application gates and the Issuance "
                    "agent - not from the person.")
    else:
        autonomy = ""
    integrity = None
    if current:
        snapshot = service.verify_snapshot(battery_id, current["version"])
        integrity = {"ledger": ledger_panel(ledger.check(battery_id, current["version"]) if ledger else None),
                     "snapshot": snapshot_panel(snapshot)}
    runs = [s for s in steps if s["kind"] == "agent"]
    return {"battery_id": battery_id, "model_id": state["model_id"], "status": status,
            "version": current["version"] if current else None,
            "published_when": when(cut) if cut else "", "lots": domain.split_ids(state["lot_ids"]),
            "path": path, "after": after, "chips": chips, "autonomy": autonomy, "integrity": integrity,
            "totals": run_totals(runs, prices, 1 if current else 0)}


def ledger_panel(result):
    if result is None:
        return {"status": "unavailable", "tone": "info", "title": LEDGER_TITLES["unavailable"],
                "message": "No ledger check is configured.", "performed": False}
    status = result["status"]
    title = LEDGER_TITLES[status]
    if status == "verified" and result.get("checked_utc"):
        title = f"Ledger verified at {when(result['checked_utc'])}"
    elif status == "failed" and result.get("checked_utc"):
        title = f"Ledger verification FAILED at {when(result['checked_utc'])}"
    return {"status": status, "tone": LEDGER_TONES[status], "title": title, "message": result["message"],
            "performed": result.get("performed", False), "reason_code": result.get("reason_code")}


def snapshot_panel(result):
    if result is None:
        return {"tone": "bad", "title": "Snapshot integrity: no published record for this version"}
    current, recorded = result["current_blob"], result["recorded_version"]
    if current == "match" and recorded == "match":
        return {"tone": "good",
                "title": "Snapshot integrity: the stored passport copy matches the hash recorded at publication"}
    if current == "missing":
        return {"tone": "warn", "title": "Snapshot integrity: the stored copy is not written yet (outbox pending)"}
    if current == "match":
        return {"tone": "warn", "title": "Snapshot integrity: the stored copy matches; its version receipt is pending"}
    return {"tone": "bad", "title": "Snapshot integrity: the stored copy does NOT match the hash recorded at "
                                    "publication - investigate"}


# ---- operations console (/) -----------------------------------------------------------------------
REVIEW_STATES = {"open": ("Open", "warn"), "resolved": ("Resolved", "good")}
OUTBOX_STATES = {"pending": ("Waiting", "warn"), "processing": ("Writing", "info"), "done": ("Written", "good"),
                 "failed": ("Failed", "bad")}


def moment(value):
    try:
        return when(value)
    except (TypeError, ValueError):
        return str(value or "")


def console_result(msg):
    """A console form posts to the JSON API, which redirects back with '<HTTP status> <JSON answer>'."""
    if not msg:
        return None
    code, _, detail = msg.partition(" ")
    if not (code.isdigit() and len(code) == 3):
        return {"ok": False, "code": "", "summary": "", "detail": msg}
    try:
        body = json.loads(detail)
    except ValueError:
        body = None
    summary = ""
    if isinstance(body, dict):
        summary = body.get("message") or body.get("detail") or ""
        summary = summary if isinstance(summary, str) else ""
    return {"ok": 200 <= int(code) < 300, "code": code, "summary": summary, "detail": detail}


def scope_subjects(tx, scope):
    subjects = scope.batteries | scope.lots | scope.builds
    for table, field, parent, group in (
        ("Evidence", "evidence_id", "lot_id", "lots"),
        ("Reviews", "review_id", "lot_id", "lots"),
        ("ComplianceCases", "case_id", "lot_id", "lots"),
        ("SecondLifeRequests", "request_id", "battery_id", "batteries"),
        ("LifecycleEvents", "message_id", "battery_id", "batteries"),
    ):
        subjects.update(r[field] for r in tx.select(table) if scope.includes(r[parent], group))
    for table, field in (("AgentTasks", "task_id"), ("ConnectorMessages", "message_id")):
        subjects.update(r[field] for r in tx.select(table) if r["subject"] in subjects)
    return subjects


def console_view(service, worker, *, staff=False, reviewer=False, operator=False, msg="", scope=None):
    """The operations controls behind the executive screens. Anonymous visitors get the public list only;
    reviews, evidence and the audit log need a staff role, the snapshot queue the Operator role."""
    scoped = scope is not None and not scope.all
    board = service.board(scope.batteries if scoped else None)
    with service.store.read() as tx:
        subjects = scope_subjects(tx, scope) if scoped else None
        audit = [r for r in tx.select("AuditEvents") if subjects is None or r["subject"] in subjects][-15:]
    passports = []
    for p in service.list_passports():
        if scoped and not scope.includes(p["battery_id"]):
            continue
        issued = p["passport_status"] == "committed"
        passports.append({
            "battery_id": p["battery_id"], "model_id": p["model_id"], "url": f"/p/{p['battery_id']}",
            "status": {"text": "Published", "tone": "good"} if issued else {"text": "Not issued", "tone": "info"},
            "version": p["passport_version"], "published_when": moment(p["committed_utc"]) if issued else "",
            "copy": ({"text": "Written", "tone": "good"} if p["snapshot_state"] == "written"
                     else {"text": "Pending", "tone": "warn"}) if issued else None,
            "sha256": p["snapshot_sha256"] or ""})
    total, done = board["total"], board["committed"]
    view = {"board": {**board, "tone": "good" if total and done == total else ("warn" if total else "info")},
            "passports": passports, "result": console_result(msg), "reviews": [], "evidence": [], "audit": [],
            "outbox": [], "worker": None, "evidence_kinds": sorted(EVIDENCE_KINDS),
            "lot_ids": sorted(scope.lots) if scope is not None else []}
    if staff:
        for r in service.list_reviews():
            if scoped and not scope.includes(r["lot_id"], "lots"):
                continue
            text, tone = REVIEW_STATES.get(r["status"], (str(r["status"]).capitalize(), "info"))
            view["reviews"].append({
                "review_id": r["review_id"], "url": f"/holds/{r['review_id']}", "lot_id": r["lot_id"],
                "status": {"text": text, "tone": tone}, "row_version": r["row_version"],
                "opening": domain.split_ids(r.get("opening_evidence")),
                "resolution": domain.split_ids(r.get("resolution_evidence")),
                "reason": r.get("resolution_reason") or r.get("reason") or "",
                "reason_ai": not r.get("resolution_reason") and r.get("reason_origin") == "ai",
                "provenance_id": "" if r.get("resolution_reason") else r.get("provenance_id") or "",
                "opened_when": moment(r.get("opened_utc"))})
        view["evidence"] = [{"evidence_id": e["evidence_id"], "lot_id": e["lot_id"], "kind": e["kind"],
                             "file_name": e["file_name"], "sha256": e["sha256"] or "",
                             "uploaded_by": person(e.get("uploaded_by")),
                             "uploaded_when": moment(e.get("uploaded_utc"))}
                            for e in service.list_evidence() if not scoped or scope.includes(e["lot_id"], "lots")]
        view["audit"] = [{"event_id": a.get("event_id"), "when": moment(a.get("event_utc")),
                          "actor": person(a.get("actor")), "action": str(a.get("action") or "").replace("_", " "),
                          "subject": a.get("subject")} for a in reversed(audit)]
    if operator:
        status = worker.status() if worker else {"running": False, "paused": False, "last_error": None}
        view["worker"] = {
            "state": ({"text": "Paused", "tone": "warn"} if status["paused"] else
                      {"text": "Running", "tone": "good"} if status["running"] else
                      {"text": "Not started", "tone": "info"}),
            "last_error": None if scoped else status["last_error"]}
        for o in service.outbox():
            if subjects is not None and o["subject"] not in subjects:
                continue
            text, tone = OUTBOX_STATES.get(o["status"], (str(o["status"]).capitalize(), "info"))
            view["outbox"].append({"outbox_id": o["outbox_id"], "subject": o["subject"],
                                   "status": {"text": text, "tone": tone}, "attempts": o["attempts"],
                                   "last_error": o.get("last_error") or "",
                                   "created_when": moment(o.get("created_utc"))})
    view["reviewer"], view["staff"], view["operator"] = reviewer, staff, operator
    return view


CONNECTOR_RESULTS = {"verified": ("Verified", "good"), "routed": ("Routed to Lifecycle", "good"),
                     "quarantined": ("Quarantined - not applied", "warn"), "rejected": ("Rejected", "bad")}
CONNECTOR_TYPES = {"supplier_claim": "Supplier claim", "record_soh": "State-of-health reading",
                   "record_ownership_change": "Ownership change", "request_repurpose": "Second-life request",
                   "record_end_of_life": "End-of-life handover", "record_recycling_closure": "Recycling closure"}


def connector_view(connector, limit=25, scope=None):
    """The mock connector's party registry and latest messages (public keys shortened; no payloads)."""
    scoped = scope is not None and not scope.all
    with connector.service.store.read() as tx:
        messages = sorted((m for m in tx.select("ConnectorMessages")
                           if not scoped or m["subject"] in scope.batteries | scope.lots),
                          key=lambda m: (m["received_utc"], m["receipt_id"]))
        party_ids = {m["sender"] for m in messages}
        if scoped:
            party_ids.update(r["supplier_id"] for r in tx.select("PackageLots") if r["build_id"] in scope.builds)
            party_ids.update(r["holder_id"] for r in tx.select("PassportState") if r["battery_id"] in scope.batteries)
    parties = [p for p in connector.parties() if not scoped or p["party_id"] in party_ids]
    parties_rows = [{"party_id": p["party_id"], "name": p["name"], "role": p["role"].replace("_", " "),
                     "key_id": p["key_id"], "key": (p["public_key"] or "")[:12],
                     "scope": ", ".join([lot for lot in p["lot_ids"] if not scoped or lot in scope.lots]
                                       + p["event_types"]) or "-",
                     "status": ({"text": "Revoked", "tone": "bad"} if p["revoked"]
                                else {"text": "Active", "tone": "good"})}
                    for p in parties]
    message_rows = []
    for m in reversed(messages[-limit:]):
        text, tone = CONNECTOR_RESULTS.get(m["result"], (str(m["result"]), "info"))
        message_rows.append({"receipt_id": m["receipt_id"], "message_id": m["message_id"] or "-",
                         "sender": m["sender"] or "-", "type": (m["message_type"] or "-").replace("_", " "),
                         "subject": m["subject"] or "-", "status": {"text": text, "tone": tone},
                         "reason_code": m["reason_code"], "when": moment(m["received_utc"])})
    quarantined = [e for e in connector.service.quarantined_events()
                   if not scoped or scope.includes(e["battery_id"])]
    quarantined = [{"event_id": e["message_id"], "battery_id": e["battery_id"],
                    "event_type": e["event_type"].replace("_", " "), "issuer": e["issuer"],
                    "state": (e["state_before"] or "-").replace("_", " "), "reason_code": e["reason_code"],
                    "when": moment(e["recorded_utc"])}
                   for e in reversed(quarantined[-limit:])]
    return {"quarantined": quarantined, "label": "mock", "parties": parties_rows, "messages": message_rows,
            "agreements": sum(1 for a in connector.agreements() if not scoped or a["sender"] in party_ids)}
