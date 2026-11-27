"""Compliance cases on critical-material lots: open, decide (keep_hold or accept_with_action_plan) and reopen.

A lot has at most one case; reopening updates it in place, so the ledger history keeps every state. An open case
holds every battery that uses the lot (readiness reason compliance_review_open). Only a Compliance user decides; the
Supplier agent may escalate but never clears or decides a case.
"""

import json
import re
import uuid
from datetime import date, datetime

from . import domain
from .store import ConflictError

DECISIONS = ("keep_hold", "accept_with_action_plan")
STATES = ("open", "accepted")
CASE_ID = re.compile(r"^CC-[A-Z0-9]{8}$")
DUE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _service():
    from . import service
    return service


def _minutes(start, end):
    def parse(value):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return max(0, int((parse(end) - parse(start)).total_seconds() // 60))


def _join(ids, limit=400):
    text = ",".join(ids)
    while len(text) > limit and "," in text:
        text = text.split(",", 1)[1]
    return text[:limit]


class ComplianceCommands:
    """Mixed into PassportService; uses its store, clock, audit, command plumbing and lot re-evaluation."""

    # ---- shared steps (inside a transaction) ----------------------------------------------------
    def _case_for_lot(self, tx, lot_id):
        cases = tx.select("ComplianceCases", lot_id=lot_id)
        return cases[-1] if cases else None

    def _reopen(self, tx, case, evidence_ids, reason_code, actor):
        prior = domain.split_ids(case["opening_evidence"])
        opening = prior + [e for e in evidence_ids if e not in prior]
        now = self.clock()
        try:
            tx.update("ComplianceCases", {
                "case_id": case["case_id"], "status": "open", "opening_evidence": _join(opening),
                "reopen_count": case["reopen_count"] + 1, "reopened_utc": now}, case["row_version"])
        except ConflictError as exc:
            raise _service().CommandError(409, "version_conflict", str(exc)) from exc
        self._audit(tx, actor, "compliance_case_reopened", case["case_id"],
                    {"lot_id": case["lot_id"], "reason_code": reason_code, "evidence_ids": evidence_ids})
        self._reevaluate_lot(tx, case["lot_id"], actor, {"kind": "clean_case_rule", "review_id": None,
                                                         "evidence_ids": []})
        return {"case_id": case["case_id"], "outcome": "reopened", "reason_code": reason_code,
                "row_version": case["row_version"] + 1}

    def _open_case(self, tx, lot_id, evidence_ids, reason_code, actor, agent_task_id=None):
        """Open a case for the lot, reopen an accepted one, or report the open one (one open case per lot)."""
        case = self._case_for_lot(tx, lot_id)
        if case and case["status"] == "open":
            return {"case_id": case["case_id"], "outcome": "already_open", "reason_code": case["reason_code"],
                    "row_version": case["row_version"]}
        if case:
            return self._reopen(tx, case, evidence_ids, reason_code, actor)
        case_id = f"CC-{uuid.uuid4().hex[:8].upper()}"
        tx.insert("ComplianceCases", {
            "case_id": case_id, "lot_id": lot_id, "status": "open", "reason_code": reason_code,
            "opening_evidence": _join(evidence_ids), "agent_task_id": agent_task_id, "opened_by": actor,
            "opened_utc": self.clock(), "reopen_count": 0, "row_version": 1})
        self._audit(tx, actor, "compliance_case_opened", case_id,
                    {"lot_id": lot_id, "reason_code": reason_code, "evidence_ids": evidence_ids,
                     "agent_task_id": agent_task_id})
        self._reevaluate_lot(tx, lot_id, actor, {"kind": "clean_case_rule", "review_id": None, "evidence_ids": []})
        return {"case_id": case_id, "outcome": "opened", "reason_code": reason_code, "row_version": 1}

    def _reopen_on_adverse(self, tx, lot_id, evidence_id, actor):
        """New adverse_report evidence for a lot whose case was accepted reopens that case."""
        case = self._case_for_lot(tx, lot_id)
        if not case or case["status"] != "accepted":
            return None
        return self._reopen(tx, case, [evidence_id], "new_adverse_report", actor)

    # ---- commands -------------------------------------------------------------------------------
    def open_case(self, lot_id, reason, evidence_ids, actor, command_id):
        svc = _service()
        evidence_ids = sorted(set(svc.as_list(evidence_ids)))
        reason = (reason or "").strip()

        def action(tx):
            if not svc.LOT_ID.match(lot_id or ""):
                raise svc.CommandError(422, "bad_lot", "lot_id must look like LOT-A")
            lots = tx.select("PackageLots", lot_id=lot_id)
            if not lots:
                raise svc.CommandError(404, "lot_not_found", "Import a package that delivers this lot first.")
            if lots[-1]["material"] not in domain.CRITICAL:
                raise svc.CommandError(422, "not_critical_lot", "Compliance cases cover critical-material lots "
                                       "(cobalt, graphite, lithium, nickel).")
            if not 5 <= len(reason) <= 400:
                raise svc.CommandError(422, "reason_required", "Give a reason of 5-400 characters.")
            self._evidence_for_lot(tx, evidence_ids, lot_id)
            case = self._case_for_lot(tx, lot_id)
            if case and case["status"] == "open":
                raise svc.CommandError(409, "case_already_open", f"{lot_id} already has an open case.",
                                       case_id=case["case_id"])
            result = self._open_case(tx, lot_id, evidence_ids, "compliance_user", actor)
            return 201, {**result, "lot_id": lot_id}

        payload = {"lot_id": lot_id, "reason": reason, "evidence_ids": evidence_ids, "subject": lot_id or ""}
        return self._execute(command_id, "open_compliance_case", payload, actor, action)

    def decide_case(self, case_id, decision, expected_row_version, owner, due_date, evidence_ids, reason, actor,
                    command_id):
        svc = _service()
        evidence_ids = sorted(set(svc.as_list(evidence_ids)))
        reason, owner, due_date = (reason or "").strip(), (owner or "").strip(), (due_date or "").strip()

        def action(tx):
            case = tx.get("ComplianceCases", case_id)
            if not case:
                raise svc.CommandError(404, "case_not_found", f"No compliance case {case_id}.")
            if decision not in DECISIONS:
                raise svc.CommandError(422, "bad_decision", f"decision must be one of {list(DECISIONS)}.")
            if case["status"] != "open":
                raise svc.CommandError(409, "case_not_open", f"{case_id} is {case['status']}.")
            if case["row_version"] != expected_row_version:
                raise svc.CommandError(409, "version_conflict", "The case changed since you read it. Reload and "
                                       "retry.", current_row_version=case["row_version"])
            if not 5 <= len(reason) <= 400:
                raise svc.CommandError(422, "reason_required", "Give a reason of 5-400 characters.")
            if evidence_ids or decision == "accept_with_action_plan":
                self._evidence_for_lot(tx, evidence_ids, case["lot_id"])
            accepted = decision == "accept_with_action_plan"
            if accepted:
                if not 2 <= len(owner) <= 100:
                    raise svc.CommandError(422, "owner_required", "Name the action-plan owner (2-100 characters).")
                try:
                    valid_date = bool(DUE_DATE.match(due_date)) and date.fromisoformat(due_date)
                except ValueError:
                    valid_date = False
                if not valid_date:
                    raise svc.CommandError(422, "due_date_required", "Give the action-plan due date as YYYY-MM-DD.")
                if not set(evidence_ids) - set(domain.split_ids(case["opening_evidence"])):
                    raise svc.CommandError(422, "new_evidence_required", "Accepting needs at least one evidence item "
                                           "that was not there when the case opened.")
            now = self.clock()
            change = {"case_id": case_id, "decision": decision, "decided_by": actor, "decided_utc": now}
            if accepted:
                change.update(status="accepted", action_owner=owner, due_date=due_date)
            try:
                tx.update("ComplianceCases", change, expected_row_version)
            except ConflictError as exc:
                raise svc.CommandError(409, "version_conflict", str(exc)) from exc
            decision_id = f"CD-{uuid.uuid4().hex[:8].upper()}"
            tx.insert("ComplianceDecisions", {
                "decision_id": decision_id, "case_id": case_id, "lot_id": case["lot_id"], "decision": decision,
                "action_owner": owner if accepted else None, "due_date": due_date if accepted else None,
                "evidence_ids": ",".join(evidence_ids), "reason": reason,
                "case_row_version": expected_row_version + 1, "decided_by": actor, "decided_utc": now})
            self._audit(tx, actor, "compliance_case_decided", case_id,
                        {"lot_id": case["lot_id"], "decision": decision, "decision_id": decision_id,
                         "evidence_ids": evidence_ids})
            affected = []
            if accepted:
                affected = self._reevaluate_lot(tx, case["lot_id"], actor, {
                    "kind": "compliance_accepted", "review_id": None, "case_id": case_id,
                    "evidence_ids": sorted(set(domain.split_ids(case["opening_evidence"])) | set(evidence_ids))})
                self._enqueue_lot_triggers(tx, case["lot_id"])
            return 200, {"case_id": case_id, "decision_id": decision_id, "decision": decision,
                         "status": "accepted" if accepted else "open", "row_version": expected_row_version + 1,
                         "age_minutes": _minutes(case["reopened_utc"] or case["opened_utc"], now),
                         "lot_id": case["lot_id"], "batteries": affected}

        payload = {"case_id": case_id, "decision": decision, "expected_row_version": expected_row_version,
                   "owner": owner, "due_date": due_date, "evidence_ids": evidence_ids, "reason": reason,
                   "subject": case_id or ""}
        return self._execute(command_id, "decide_compliance_case", payload, actor, action)

    # ---- reads ----------------------------------------------------------------------------------
    def list_cases(self):
        with self.store.read() as tx:
            return sorted(tx.select("ComplianceCases"), key=lambda c: c["opened_utc"])

    def case_detail(self, case_id):
        """The case with its decisions, the lot's evidence, the batteries it holds and the agent's proposal."""
        if not CASE_ID.match(case_id or ""):
            return None
        with self.store.read() as tx:
            case = tx.get("ComplianceCases", case_id)
            if not case:
                return None
            lot_id = case["lot_id"]
            batteries = [{"battery_id": s["battery_id"], "eligibility": s["eligibility"],
                          "hold_reason": s["hold_reason"], "committed_version": s["committed_version"]}
                         for s in tx.select("PassportState") if lot_id in domain.split_ids(s["lot_ids"])]
            proposal = None
            if case["agent_task_id"]:
                result = tx.get("AgentResults", case["agent_task_id"])
                if result:
                    body = json.loads(result["result_json"])
                    mark = next((m for m in body.get("provenance") or [] if m.get("field") == "reason"), None)
                    proposal = {"task_id": case["agent_task_id"], **(body.get("decision") or {}),
                                "reason_provenance": mark}
            evidence = [{k: e[k] for k in ("evidence_id", "kind", "file_name", "sha256", "uploaded_utc")}
                        for e in tx.select("Evidence", lot_id=lot_id)]
            decisions = sorted(tx.select("ComplianceDecisions", case_id=case_id), key=lambda d: d["decided_utc"])
            return {"case": case, "decisions": decisions, "evidence": evidence, "batteries": batteries,
                    "proposal": proposal, "labels": ["synthetic", "illustrative"]}
