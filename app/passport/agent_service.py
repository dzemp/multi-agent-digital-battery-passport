"""Durable, bounded agent tasks. Models propose; the existing application owns every write."""

import json
import re
import uuid
from functools import partial

from . import connector, domain, lifecycle, prompt_shields, provenance
from . import carbon_verifier
from .agent_contracts import ACTIONS, CONTRACT, METHOD, POLICY, ROLES, SECTIONS, SOURCES
from .agent_contracts import Decision, fingerprint
from .service import COMMAND_ID, LOT_ID, MAX_JSON_EVIDENCE_BYTES, CommandError

PROVENANCE_ID = re.compile(r"^PV-[0-9a-f]{61}$")
MAX_VERIFY_CHARS = 20000
ATTEMPT_ID = re.compile(r"^[0-9a-f]{32}$")
SHIELD_ACTOR, SHIELD_REASON = prompt_shields.ACTOR, prompt_shields.REVIEW_REASON


def record_json(value):
    text = json.dumps(value, sort_keys=True, ensure_ascii=True)
    if len(text) > 8000:
        raise CommandError(413, "agent_record_limit", "The agent contract permits at most 8,000 ASCII JSON bytes.")
    return text


class AgentService:
    def __init__(self, service):
        self.service = service

    def _evidence(self, tx, lot):
        result = []
        for row in sorted(tx.select("Evidence", lot_id=lot), key=lambda r: (r["uploaded_utc"], r["evidence_id"])):
            data = self.service.blobs.get("evidence", row["blob_path"], row["blob_version_id"])
            if data is None or domain.sha256(data) != row["sha256"]:
                raise CommandError(409, "evidence_integrity", "Evidence bytes do not match the recorded hash.")
            if row["content_type"] != "application/json" or len(data) > MAX_JSON_EVIDENCE_BYTES:
                raise CommandError(422, "evidence_format", "Agents accept JSON evidence up to 16 KB.")
            result.append({"id": row["evidence_id"], "kind": row["kind"], "sha256": row["sha256"],
                           "content": json.loads(data)})
        return result

    @staticmethod
    def _fact(tx, kind, subject, input_hash):
        return [r for r in tx.select("AgentFacts", kind=kind, subject=subject) if r["input_hash"] == input_hash]

    @staticmethod
    def calculation_hash(inputs):
        batch = inputs["batch"] or {}
        return fingerprint({"input_hash": inputs["battery"]["input_hash"], "method": METHOD,
                            "factor_set_version": batch.get("factor_set_version")})

    @staticmethod
    def critical_lots(inputs):
        """Lots of critical raw materials (cobalt, graphite, lithium, nickel) that this battery uses."""
        return sorted({p["lot_id"] for p in inputs["composition"] if p["material"] in domain.CRITICAL})

    def gates(self, tx, state):
        """Supplier due diligence covers critical-material lots only. Every critical lot needs a current no_issue
        assessment of its evidence; the signed supplier claim arrives as evidence through the connector."""
        row = self.service.package_row(tx, state)
        inputs = self.service.inputs(tx, row)
        reasons = []
        if state["lifecycle_status"] in domain.CLOSED:
            reasons.append(domain.PASSPORT_CLOSED)
        eligibility, reason = domain.eligibility(inputs, set())
        if eligibility == domain.BLOCKED:
            reasons.append(reason)
        for lot in domain.split_ids(state["lot_ids"]):
            if tx.select("Reviews", lot_id=lot, status="open"):
                reasons.append(f"{domain.SOURCING_REVIEW_OPEN}:{lot}")
            if tx.select("ComplianceCases", lot_id=lot, status="open"):
                reasons.append(f"{domain.COMPLIANCE_REVIEW_OPEN}:{lot}")
        for lot in self.critical_lots(inputs):
            evidence = self._evidence(tx, lot)
            clear = self._fact(tx, "supplier", lot, fingerprint(evidence)) if evidence else []
            if not any(json.loads(f["data_json"])["action"] == "no_issue" for f in clear):
                reasons.append(f"supplier_assessment_missing:{lot}")
        if not self._fact(tx, "carbon", state["battery_id"], self.calculation_hash(inputs)):
            reasons.append("calculation_missing")
        else:
            carbon = self._fact(tx, "carbon", state["battery_id"], self.calculation_hash(inputs))[-1]
            verified = [fact for fact in self._fact(tx, "carbon_verifier", state["battery_id"],
                                                     self.calculation_hash(inputs))
                        if json.loads(fact["data_json"]).get("calculation_fact_id") == carbon["fact_id"]]
            if not verified:
                reasons.append("calculation_unverified")
        return {"ready": not reasons, "reasons": reasons, "policy": POLICY}

    @staticmethod
    def _calculator_section(inputs, available):
        battery, batch = inputs["battery"], inputs["batch"] or {}
        return {
            "method": METHOD, "available": available,
            "inputs": {
                "battery": {k: battery[k] for k in (
                    "battery_id", "input_hash", "chemistry", "rated_kwh", "plant_id", "batch_id", "allocated_kwh",
                    "grid_factor_id", "readiness")},
                "batch": {k: batch.get(k) for k in ("method_version", "factor_set_version", "allocation_rule")},
                "parts": [{"material": p["material"], "lot_id": p["lot_id"], "mass_kg": p["mass_kg"],
                           "recycled_share_pct": (inputs["lots"].get(p["lot_id"]) or {}).get("recycled_share_pct")}
                          for p in sorted(inputs["composition"], key=lambda p: (p["material"], p["lot_id"]))]}}

    def context(self, tx, role, subject, event=None):
        if role not in ROLES:
            raise CommandError(422, "unknown_role", "Choose one of the five agent roles.")
        active_policy = tx.get("PolicyState", "active")
        context = {"contract": CONTRACT, "role": role, "subject": subject,
                   "policy_version": f"synthetic-agent-policy-{active_policy['version'] if active_policy else 2}"}
        if active_policy:
            context["active_policy"] = {key: active_policy[key] for key in
                                        ("version", "active_candidate_id", "title", "policy_text")}
        if role == "watch":
            if subject != "battery-due-diligence":
                raise CommandError(422, "bad_subject", "Watch is scoped to battery-due-diligence.")
            policy_state = active_policy or {"version": 2, "title": "Baseline policy",
                                             "policy_text": "Operational gates remain code-owned."}
            return {**context, "sources": SOURCES, "policy": {
                "version": f"synthetic-agent-policy-{policy_state['version']}",
                "activation": "human_review_only", "active_title": policy_state["title"],
                "active_guidance": policy_state["policy_text"]}}
        if role == "supplier":
            if not LOT_ID.fullmatch(subject):
                raise CommandError(422, "bad_subject", "Supplier tasks use a lot ID.")
            lots = tx.select("PackageLots", lot_id=subject)
            if not lots:
                raise CommandError(404, "lot_not_found", "Import a package that delivers this lot first.")
            if lots[-1]["material"] not in domain.CRITICAL:
                raise CommandError(422, "not_critical_lot", "Supplier due diligence covers critical-material lots "
                                   "(cobalt, graphite, lithium, nickel); other lots get the package checks.")
            return {**context, "evidence": self._evidence(tx, subject),
                    "reviews": tx.select("Reviews", lot_id=subject)}
        state = tx.get("PassportState", subject)
        if not state:
            raise CommandError(404, "battery_not_found", "Import a battery package first.")
        row = self.service.package_row(tx, state)
        context["state"] = {"battery_id": subject, "build_id": state["build_id"],
                            "input_hash": state["input_hash"], "row_version": state["row_version"],
                            "committed_version": state["committed_version"], "readiness": row["readiness"]}
        if role == "carbon":
            inputs = self.service.inputs(tx, row)
            context["calculator"] = self._calculator_section(inputs, self.service.footprint(inputs) is not None)
        elif role == "issuance":
            context["gates"] = self.gates(tx, state)
        else:
            self._check_event(tx, state, event)
            readings = self.service.soh_readings(tx, subject)
            context["state"].update(lifecycle_status=state["lifecycle_status"], holder_id=state["holder_id"],
                                    latest_soh_pct=readings[-1]["soh_pct"] if readings else None)
            context["event"] = event
            prior = self.service.recorded_event(tx, event["event_id"])
            context["gate"] = {"allowed": False, "reason_code": "already_recorded"} if prior else \
                self.service.lifecycle_gate(tx, {k: event[k] for k in lifecycle.EVENT_KEYS})
        return context

    @staticmethod
    def _check_event(tx, state, event):
        keys = lifecycle.EVENT_KEYS | {"expected_passport_version"}
        if not isinstance(event, dict) or set(event) != keys or not isinstance(event["details"], dict):
            raise CommandError(422, "bad_event", "Use the synthetic partner event contract: event_id, battery_id, "
                                                 "event_type, issuer, issued_utc, details, expected_passport_version.")
        if not isinstance(event["event_id"], str) or not COMMAND_ID.fullmatch(event["event_id"]):
            raise CommandError(422, "bad_event_id", "Use an 8-64 character event ID.")
        if event["battery_id"] != state["battery_id"]:
            raise CommandError(403, "event_scope", "The event names another battery than the task.")
        routed = connector.routed_event(tx, event["event_id"])
        if routed is None or lifecycle.event_of(routed) != {k: event[k] for k in lifecycle.EVENT_KEYS}:
            raise CommandError(403, "event_scope", "Only a partner event verified and routed by the connector "
                                                   "for the requested battery is accepted.")
        if tx.select("LifecycleEvents", message_id=event["event_id"]):
            return
        if type(event["expected_passport_version"]) is not int or not state["committed_version"] or \
                event["expected_passport_version"] != state["committed_version"]:
            raise CommandError(409, "event_version", "An event must name the current committed passport version.")

    def prepare(self, role, subject, event, actor, task_id):
        if not isinstance(task_id, str) or not COMMAND_ID.fullmatch(task_id):
            raise CommandError(422, "bad_task_id", "Use an 8-64 character task ID.")
        request = {"role": role, "subject": subject, "event": event}
        self.service.require_schema()
        with self.service.store.transaction() as tx:
            prior = tx.get("AgentTasks", task_id)
            if prior:
                if json.loads(prior["request_json"]) != request or prior["actor"] != actor:
                    raise CommandError(409, "task_conflict", "Task ID belongs to another request or actor.")
                return prior
            context = self.context(tx, role, subject, event)
            task = {"task_id": task_id, "role": role, "subject": subject,
                    "context_hash": fingerprint(context), "context_json": record_json(context),
                    "request_json": record_json(request), "actor": actor,
                    "created_utc": self.service.clock()}
            tx.insert("AgentTasks", task)
            self.service._audit(tx, actor, "agent_task_prepared", subject, {"task_id": task_id, "role": role})
            return task

    def prepare_auto(self, role, subject, event, actor):
        """Use a context-derived identity so duplicate triggers share the same recorded agent task."""
        with self.service.store.read() as tx:
            context = self.context(tx, role, subject, event)
        task_id = "wf-" + fingerprint({"role": role, "subject": subject, "context": fingerprint(context)})[:61]
        return self.prepare(role, subject, event, actor, task_id)

    def result(self, task_id):
        with self.service.store.read() as tx:
            row = tx.get("AgentResults", task_id)
            return (row["status_code"], json.loads(row["result_json"])) if row else None

    def start_attempt(self, task_id, attempt_id):
        if not isinstance(attempt_id, str) or not ATTEMPT_ID.fullmatch(attempt_id):
            raise CommandError(422, "bad_attempt_id", "Use a 32-character lowercase hexadecimal attempt ID.")
        with self.service.store.transaction() as tx:
            if not tx.get("AgentTasks", task_id):
                raise CommandError(404, "agent_task_not_found", "No such agent task.")
            self.service._audit(tx, "agent-workflow", "agent_attempt_started", task_id,
                                {"attempt_id": attempt_id})

    def screen(self, task, runner):
        """Prompt Shields screens supplier evidence before any model reads it. A detected attack opens a sourcing
        review for a person and stops the run; a person who resolved such a review lets the same evidence through.
        A service failure propagates, so the run fails closed."""
        shield = getattr(runner, "shield", None)
        if task["role"] != "supplier" or not isinstance(shield, prompt_shields.Screener):
            return
        evidence = json.loads(task["context_json"])["evidence"]
        if not evidence:
            return
        result = shield.screen(evidence)
        flagged, lot = result["flagged"], task["subject"]
        with self.service.store.transaction() as tx:
            detail = {"task_id": task["task_id"], "flagged": flagged, "documents": result["documents"],
                      "api_version": result["api_version"]}
            if not flagged:
                self.service._audit(tx, SHIELD_ACTOR, "prompt_shield_passed", lot, detail)
                return
            cleared = {e for r in tx.select("Reviews", lot_id=lot, opened_by=SHIELD_ACTOR, status="resolved")
                       for e in domain.split_ids(r["opening_evidence"])}
            if set(flagged) <= cleared:
                self.service._audit(tx, SHIELD_ACTOR, "prompt_attack_cleared_by_reviewer", lot, detail)
                return
            if not tx.select("Reviews", lot_id=lot, status="open"):
                review_id = f"RV-{uuid.uuid4().hex[:8].upper()}"
                tx.insert("Reviews", {"review_id": review_id, "lot_id": lot, "status": "open",
                                      "reason": SHIELD_REASON, "opening_evidence": ",".join(flagged),
                                      "opened_by": SHIELD_ACTOR, "opened_utc": self.service.clock(),
                                      "reason_origin": "system", "provenance_id": None, "row_version": 1})
                self.service._reevaluate_lot(tx, lot, SHIELD_ACTOR, {"kind": "prompt_attack_detected"})
                self.service._audit(tx, SHIELD_ACTOR, "review_opened", review_id, {"task_id": task["task_id"]})
            self.service._audit(tx, SHIELD_ACTOR, "prompt_attack_detected", lot, detail)
        raise CommandError(409, "prompt_attack_detected", "Prompt Shields found instructions in supplier evidence "
                           f"({', '.join(flagged)}). No model read it; a person reviews the lot.")

    def invoke_attempt(self, task, runner):
        """Bind hosted callbacks and in-process tool reads to the same registered attempt."""
        self.screen(task, runner)
        attempt_id = uuid.uuid4().hex
        self.start_attempt(task["task_id"], attempt_id)
        decision, metadata = runner.run(task["role"], task["task_id"], json.loads(task["context_json"]),
                                        partial(self.section, attempt_id=attempt_id), attempt_id=attempt_id)
        return attempt_id, decision, metadata

    def section(self, task_id, role, section, attempt_id=None, *, require_attempt=False):
        with self.service.store.transaction() as tx:
            task = tx.get("AgentTasks", task_id)
            if not task or task["role"] != role or section not in SECTIONS[role]:
                raise CommandError(403, "tool_scope", "The task or section is outside this agent's role.")
            if (attempt_id is None and require_attempt) or (
                    attempt_id is not None and (
                        not isinstance(attempt_id, str) or not ATTEMPT_ID.fullmatch(attempt_id)
                    )
            ):
                raise CommandError(422, "bad_attempt_id", "Use a 32-character lowercase hexadecimal attempt ID.")
            started = tx.select("AuditEvents", action="agent_attempt_started")
            if attempt_id is not None and not any(
                    a["subject"] == task_id and json.loads(a["detail"]).get("attempt_id") == attempt_id
                    for a in started):
                raise CommandError(403, "unknown_attempt", "Start this agent attempt before reading task sections.")
            calls = [a for a in tx.select("AuditEvents", action="agent_tool_read")
                     if a["subject"] == task_id and json.loads(a["detail"]).get("attempt_id") == attempt_id]
            if len(calls) >= 8:
                raise CommandError(429, "tool_budget", "This agent attempt has used its eight permitted tool calls.")
            self.service._audit(tx, f"agent:{role}", "agent_tool_read", task_id,
                                {"section": section, "attempt_id": attempt_id})
            return json.loads(task["context_json"])[section]

    def finish(self, task, decision, metadata, attempt_id=None):
        role, subject = task["role"], task["subject"]
        parsed = Decision.model_validate(decision)
        if parsed.action not in ACTIONS[role]:
            raise CommandError(403, "action_scope", "This role is not permitted to request that action.")
        self.service.require_schema()
        with self.service.store.transaction() as tx:
            prior = tx.get("AgentResults", task["task_id"])
            if prior:
                return prior["status_code"], json.loads(prior["result_json"])
            reads = {json.loads(a["detail"])["section"] for a in tx.select("AuditEvents", action="agent_tool_read")
                     if a["subject"] == task["task_id"] and
                     json.loads(a["detail"]).get("attempt_id") == attempt_id}
            missing = sorted(set(SECTIONS[role]) - reads)
            if missing:
                raise CommandError(422, "grounding_required", "The agent did not retrieve all required task sections. "
                                   f"Missing on this attempt: {', '.join(missing)}.")
            event = json.loads(task["request_json"])["event"]
            context = self.context(tx, role, subject, event)
            if fingerprint(context) != task["context_hash"]:
                raise CommandError(409, "stale_task", "Inputs changed while the agent worked. Start a new task.")
            result = self._apply(tx, task, parsed, context, metadata)
            marks = self._mark(tx, task, parsed, metadata)
            body = {"task_id": task["task_id"], "role": role, "subject": subject,
                    "decision": parsed.model_dump(), "outcome": result, "execution": metadata,
                    "provenance": marks, "context_hash": task["context_hash"], "contract": CONTRACT}
            tx.insert("AgentResults", {"task_id": task["task_id"], "status_code": 200,
                                      "result_json": record_json(body), "recorded_utc": self.service.clock()})
            self.service._audit(tx, task["actor"], "agent_task_completed", subject,
                                {"task_id": task["task_id"], "action": parsed.action})
            return 200, body

    def _mark(self, tx, task, decision, metadata):
        """Write one provenance record per AI text field, naming every place the text is stored (Art. 50(2))."""
        task_id, accepted = task["task_id"], decision.action != "defer"
        places = {field: ["AgentResults.result_json"] for field in provenance.TEXT_FIELDS}
        if accepted and task["role"] in ("supplier", "watch"):
            for field in places:
                places[field].append("AgentFacts.data_json")
        if tx.select("Reviews", provenance_id=provenance.provenance_id(task_id, "reason")):
            places["reason"].append("Reviews.reason")
        if accepted and task["role"] == "watch":
            places["policy_title"].append("PolicyCandidates.title")
            places["policy_text"].append("PolicyCandidates.policy_text")
        rows = provenance.records(task, decision.model_dump(), metadata, places, self.service.clock())
        for row in rows:
            tx.insert("AiContentProvenance", row)
        return [{"field": r["field"], "provenance_id": r["provenance_id"], "ai_generated": bool(r["ai_generated"]),
                 "digital_source_type": r["digital_source_type"]} for r in rows]

    def _apply(self, tx, task, decision, context, metadata):
        role, subject, task_id = task["role"], task["subject"], task["task_id"]
        if role == "lifecycle":
            return self._apply_event(tx, task, decision, context)
        if decision.action == "defer":
            return {"status": "deferred", "reason": decision.reason}
        data = decision.model_dump()
        input_hash = task["context_hash"]
        if role == "supplier":
            known = {e["id"] for e in context["evidence"]}
            if not decision.evidence_ids or not set(decision.evidence_ids) <= known:
                raise CommandError(422, "citation_required", "Cite recorded evidence for this lot.")
            if decision.action == "no_issue" and tx.select("Reviews", lot_id=subject, status="open"):
                raise CommandError(409, "review_open", "Only a human Reviewer can resolve an open concern.")
            if decision.action == "no_issue" and tx.select("ComplianceCases", lot_id=subject, status="open"):
                raise CommandError(409, "compliance_case_open", "Only a Compliance user can decide an open case.")
            if decision.action == "escalate_compliance":
                data["case"] = self.service._open_case(tx, subject, list(decision.evidence_ids), "agent_escalation",
                                                       f"agent:{role}", agent_task_id=task_id)
            if decision.action == "request_review" and not tx.select("Reviews", lot_id=subject, status="open"):
                review_id = f"RV-{uuid.uuid4().hex[:8].upper()}"
                tx.insert("Reviews", {"review_id": review_id, "lot_id": subject, "status": "open",
                                      "reason": decision.reason, "opening_evidence": ",".join(decision.evidence_ids),
                                      "opened_by": f"agent:{role}", "opened_utc": self.service.clock(),
                                      "reason_origin": "ai" if provenance.ai_generated(metadata) else "scripted",
                                      "provenance_id": provenance.provenance_id(task_id, "reason"),
                                      "row_version": 1})
                self.service._reevaluate_lot(tx, subject, f"agent:{role}", {"kind": "agent_review"})
                self.service._audit(tx, f"agent:{role}", "review_opened", review_id, {"task_id": task_id})
            input_hash = fingerprint(context["evidence"])
        elif role == "carbon":
            inputs = self.service.inputs(tx, self.service.package_row(tx, tx.get("PassportState", subject)))
            data = self.service.footprint(inputs)
            if data is None:
                raise CommandError(409, "inputs_incomplete", "A model cannot replace missing measurements.")
            input_hash = self.calculation_hash(inputs)
        elif role == "issuance":
            if not self.service.agent_mode:
                raise CommandError(409, "agent_mode_required", "Enable the opt-in agent publication mode first.")
            if not context["gates"]["ready"]:
                raise CommandError(409, "publication_blocked", "Publication gates failed.", gates=context["gates"])
            row = self.service.package_row(tx, tx.get("PassportState", subject))
            calc_fact = self._fact(tx, "carbon", subject, self.calculation_hash(self.service.inputs(tx, row)))[-1]
            data = self.service._apply(tx, row, set(), f"agent:{role}",
                                       {"kind": "agent_publication", "task_id": task_id,
                                        "calculation_fact_id": calc_fact["fact_id"],
                                        "calculation": json.loads(calc_fact["data_json"]),
                                        "policy": POLICY, "assurance": "synthetic_unverified"})
        else:
            if set(decision.evidence_ids) != {s["id"] for s in SOURCES}:
                raise CommandError(422, "source_citations", "Cite both pinned source excerpts.")
            if not decision.policy_title or not decision.policy_text:
                raise CommandError(422, "policy_candidate_required",
                                   "A Watch recommendation needs a title and proposed policy guidance.")
            data = {**data, "status": "awaiting_policy_approval",
                    "active_policy_unchanged": context["policy"]["version"],
                    "policy_title": decision.policy_title, "policy_text": decision.policy_text}
        tx.insert("AgentFacts", {"fact_id": task_id, "kind": role, "subject": subject, "input_hash": input_hash,
                                "data_json": record_json(data), "recorded_utc": self.service.clock()})
        if role == "carbon":
            verifier = carbon_verifier.verify(inputs, data)
            verifier_fact_id = "CV-" + fingerprint({"task_id": task_id, "input_hash": input_hash})[:61]
            tx.insert("AgentFacts", {"fact_id": verifier_fact_id, "kind": "carbon_verifier",
                                     "subject": subject, "input_hash": input_hash,
                                     "data_json": record_json({**verifier, "calculation_fact_id": task_id}),
                                     "recorded_utc": self.service.clock()})
        elif role == "watch":
            origin = provenance.execution(metadata)
            tx.insert("PolicyCandidates", {
                "candidate_id": task_id, "task_id": task_id, "title": decision.policy_title,
                "policy_text": decision.policy_text, "source_ids": ",".join(decision.evidence_ids),
                "status": "pending", "recommended_by": f"agent:{role}", "requested_by": task["actor"],
                "model": origin["model"], "model_version": origin["model_version"],
                "provenance_id": provenance.provenance_id(task_id, "policy_text"),
                "recommended_utc": self.service.clock(), "row_version": 1})
        return data

    def policy_candidates(self):
        with self.service.store.read() as tx:
            rows = sorted(tx.select("PolicyCandidates"), key=lambda row: row["recommended_utc"], reverse=True)
            for row in rows:
                row["provenance"] = {field: provenance.mark(tx.get("AiContentProvenance",
                                                                   provenance.provenance_id(row["task_id"], field)))
                                     for field in ("policy_title", "policy_text")}
            return rows

    def active_policy(self):
        with self.service.store.read() as tx:
            return tx.get("PolicyState", "active")

    @staticmethod
    def _decision_reason(reason):
        reason = (reason or "").strip()
        if not 5 <= len(reason) <= 400:
            raise CommandError(422, "reason_required", "Give a reason of 5-400 characters.")
        return reason

    @staticmethod
    def _pending(candidate, expected_row_version):
        if not candidate:
            raise CommandError(404, "policy_candidate_not_found", "No such Watch policy candidate.")
        if candidate["status"] != "pending":
            raise CommandError(409, "policy_candidate_not_pending", "This candidate has already been decided.")
        if candidate["row_version"] != expected_row_version:
            raise CommandError(409, "version_conflict", "The candidate changed. Reload and retry.")

    def activate_policy(self, candidate_id, expected_row_version, reason, actor):
        reason = self._decision_reason(reason)
        with self.service.store.transaction() as tx:
            candidate = tx.get("PolicyCandidates", candidate_id)
            active = tx.get("PolicyState", "active")
            self._pending(candidate, expected_row_version)
            if not active:
                raise CommandError(503, "active_policy_missing", "The active policy record is missing.")
            now = self.service.clock()
            tx.update("PolicyCandidates", {"candidate_id": candidate_id, "status": "approved",
                                           "decided_by": actor, "decided_utc": now,
                                           "decision_reason": reason}, expected_row_version)
            tx.update("PolicyState", {
                "policy_slot": "active", "version": active["version"] + 1,
                "active_candidate_id": candidate_id, "title": candidate["title"],
                "policy_text": candidate["policy_text"], "ai_drafted": 1, "source_task_id": candidate["task_id"],
                "provenance_id": candidate["provenance_id"], "activated_by": actor, "activated_utc": now,
            }, active["row_version"])
            self.service._audit(tx, actor, "watch_policy_activated", candidate_id, {
                "version": active["version"] + 1, "source_ids": candidate["source_ids"], "reason": reason})
            return {"candidate_id": candidate_id, "version": active["version"] + 1,
                    "status": "approved", "activated_utc": now}

    def reject_policy(self, candidate_id, expected_row_version, reason, actor):
        """The Policy Approver declines an AI-drafted candidate; the active guidance stays unchanged."""
        reason = self._decision_reason(reason)
        with self.service.store.transaction() as tx:
            candidate = tx.get("PolicyCandidates", candidate_id)
            self._pending(candidate, expected_row_version)
            now = self.service.clock()
            tx.update("PolicyCandidates", {"candidate_id": candidate_id, "status": "rejected",
                                           "decided_by": actor, "decided_utc": now,
                                           "decision_reason": reason}, expected_row_version)
            self.service._audit(tx, actor, "watch_policy_rejected", candidate_id, {
                "source_ids": candidate["source_ids"], "reason": reason})
            return {"candidate_id": candidate_id, "status": "rejected", "decided_utc": now}

    def verify_provenance(self, text=None, provenance_id=None):
        """Detection for AI text this application generated: an exact match on the normalised text's hash, or a
        lookup by provenance ID. Paraphrased or edited copies do not match."""
        if (text is None) == (provenance_id is None):
            raise CommandError(422, "text_or_provenance_id", "Send either text or provenance_id.")
        if provenance_id is not None and not (isinstance(provenance_id, str) and PROVENANCE_ID.match(provenance_id)):
            raise CommandError(422, "bad_provenance_id", "A provenance_id looks like PV- and 61 hex characters.")
        if text is not None and not (isinstance(text, str) and 0 < len(text) <= MAX_VERIFY_CHARS
                                     and provenance.normalise(text)):
            raise CommandError(422, "bad_text", f"Send 1-{MAX_VERIFY_CHARS:,} characters of text.")
        with self.service.store.read() as tx:
            if provenance_id is not None:
                rows = [row for row in [tx.get("AiContentProvenance", provenance_id)] if row]
            else:
                rows = tx.select("AiContentProvenance", text_sha256=provenance.text_sha256(text))
        method = ("provenance_id" if provenance_id is not None
                  else "sha256 of NFC-normalised, whitespace-collapsed text")
        return {"match": bool(rows), "method": method, "records": [provenance.public(row) for row in rows]}

    def _apply_event(self, tx, task, decision, context):
        """The app's state machine decides; the agent's proposal must match the routed event or defer."""
        event = {k: context["event"][k] for k in lifecycle.EVENT_KEYS}
        if self.service.recorded_event(tx, event["event_id"]):
            return self.service.record_lifecycle_event(tx, event, f"agent:{task['role']}", task["task_id"])
        if decision.action == "defer":
            return {"status": "deferred", "reason": decision.reason, "event_id": event["event_id"]}
        if decision.action != event["event_type"]:
            raise CommandError(422, "action_mismatch", "The proposed action must match the routed event type.")
        if not self.service.agent_mode:
            raise CommandError(409, "agent_mode_required", "Enable the opt-in agent mode to apply lifecycle events.")
        return self.service.record_lifecycle_event(tx, event, f"agent:{task['role']}", task["task_id"],
                                                   context["gate"])

    def fail(self, task, code, message, status=422):
        body = {"task_id": task["task_id"], "error": code, "message": message}
        with self.service.store.transaction() as tx:
            prior = tx.get("AgentResults", task["task_id"])
            if prior:
                return prior["status_code"], json.loads(prior["result_json"])
            tx.insert("AgentResults", {"task_id": task["task_id"], "status_code": status,
                                      "result_json": record_json(body), "recorded_utc": self.service.clock()})
            self.service._audit(tx, task["actor"], "agent_task_failed", task["subject"], body)
        return status, body
