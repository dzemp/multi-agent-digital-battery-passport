"""Lifecycle events, second-life decisions and passport links: the lifecycle state machine.

    active --record_soh / record_ownership_change--> active (new version)
    active --request_repurpose--> second_life_pending --approve--> repurposed (closed) + successor active
                                                      --reject--> active
    active --record_end_of_life--> end_of_life --record_recycling_closure--> recycled (closed)

Events arrive as signed partner messages through the connector, which routes them to Lifecycle agent tasks. The app,
not a model, checks every transition: an event the state machine does not allow is quarantined with a reason code and
never applied. Every recorded event commits one new passport version (superseding, never overwriting). Only a person
with the Reviewer role approves or rejects a second life; the illustrative value is not a sale price.
"""

import json
import re
import tomllib

from . import calculator, domain
from .store import ConflictError, format_utc, parse_utc

with open(calculator.FACTORS_FILE, "rb") as _handle:
    SECOND_LIFE = tomllib.load(_handle)["second_life"]
VALUE_LABEL = "illustrative, not a sale price"
DECISIONS = ("approve", "reject")
REQUEST_ID = re.compile(r"^SL-\d{4,8}$")
SUBJECT = re.compile(r"^[A-Z0-9-]{1,50}$")
ALLOWED_FROM = {"record_soh": domain.ACTIVE, "record_ownership_change": domain.ACTIVE,
                "request_repurpose": domain.ACTIVE, "record_end_of_life": domain.ACTIVE,
                "record_recycling_closure": domain.END_OF_LIFE}
NEXT = {"record_soh": domain.ACTIVE, "record_ownership_change": domain.ACTIVE,
        "request_repurpose": domain.SECOND_LIFE_PENDING, "record_end_of_life": domain.END_OF_LIFE,
        "record_recycling_closure": domain.RECYCLED}
INHERITED = ("chemistry", "rated_kwh", "model_id", "composition", "carbon_footprint", "recycled_content")
CLOSED_LABELS = {domain.REPURPOSED: "Closed - repurposed", domain.RECYCLED: "Closed - recycled"}
EVENT_KEYS = {"event_id", "battery_id", "event_type", "issuer", "issued_utc", "details"}


def _service():
    from . import service
    return service


def status_label(status):
    return CLOSED_LABELS.get(status, (status or domain.ACTIVE).replace("_", " ").capitalize())


def illustrative_value(rated_kwh):
    return round(float(rated_kwh) * SECOND_LIFE["value_eur_per_rated_kwh"], 2)


def event_of(payload):
    """The Lifecycle task's event for a verified partner event payload (the caller adds the expected version)."""
    skip = {"schema", "audience", "message_id", "issued_utc", "expires_utc", "data_label", "issuer", "event_type",
            "battery_id"}
    return {"event_id": payload["message_id"], "battery_id": payload["battery_id"],
            "event_type": payload["event_type"], "issuer": payload["issuer"], "issued_utc": payload["issued_utc"],
            "details": {k: v for k, v in payload.items() if k not in skip}}


def _number(value, low=None, high=None):
    ok = type(value) in (int, float) and value == value and value not in (float("inf"), float("-inf"))
    return ok and (low is None or low <= value <= high)


class LifecycleCommands:
    """Mixed into PassportService: lifecycle gates, recorded events, second-life decisions and links."""

    # ---- rows and views -----------------------------------------------------------------------
    @staticmethod
    def predecessor(tx, battery_id):
        links = tx.select("PassportLinks", battery_id=battery_id, link_type="predecessor")
        return links[0]["linked_id"] if links else None

    @staticmethod
    def successor(tx, battery_id):
        links = tx.select("PassportLinks", battery_id=battery_id, link_type="successor")
        return links[0]["linked_id"] if links else None

    def _inherited_row(self, tx, battery_id, build_id, source):
        row = tx.get("PackageBatteries", build_id, source)
        return {**row, "battery_id": battery_id, "inherited_from": source} if source != battery_id else row

    def package_row(self, tx, state):
        """The package row of a battery; a successor reads its predecessor's committed row (inherited data)."""
        row = tx.get("PackageBatteries", state["build_id"], state["battery_id"])
        if row is not None:
            return row
        source = self.predecessor(tx, state["battery_id"])
        return self._inherited_row(tx, state["battery_id"], state["build_id"], source)

    def committed_row(self, tx, state):
        """The package row behind the latest committed version: lifecycle versions never publish newer inputs."""
        version = tx.get("PassportVersions", state["battery_id"], state["committed_version"])
        source = self.predecessor(tx, state["battery_id"]) or state["battery_id"]
        return self._inherited_row(tx, state["battery_id"], version["build_id"], source)

    @staticmethod
    def soh_readings(tx, battery_id, before_version=None):
        rows = tx.select("LifecycleEvents", battery_id=battery_id, event_type="record_soh", result="recorded")
        readings = []
        for row in rows:
            if before_version is not None and row["passport_version"] >= before_version:
                continue
            details = json.loads(row["details_json"])
            readings.append({"soh_pct": details["soh_pct"], "measured_utc": details["measured_utc"],
                             "source": row["issuer"]})
        return sorted(readings, key=lambda r: parse_utc(r["measured_utc"]))

    def lifecycle_view(self, tx, battery_id, status=None, holder_id=None):
        """{status, holder_id, links, soh_readings, inherited_from} for the snapshot and the screens."""
        state = tx.get("PassportState", battery_id) or {}
        links = [{"battery_id": r["linked_id"], "link_type": r["link_type"]}
                 for r in tx.select("PassportLinks", battery_id=battery_id)]
        readings = self.soh_readings(tx, battery_id)
        source, inherited = self.predecessor(tx, battery_id), None
        if source:
            inherited = {"battery_id": source, "fields": list(INHERITED),
                         "label": "inherited from the predecessor passport, not re-measured"}
            earlier = self.soh_readings(tx, source)
            if earlier:
                readings = [{**earlier[-1], "source": f"inherited:{source}"}] + readings
        return {"status": status or state.get("lifecycle_status") or domain.ACTIVE,
                "holder_id": holder_id or state.get("holder_id"), "links": links, "soh_readings": readings,
                "inherited_from": inherited}

    # ---- the state machine --------------------------------------------------------------------
    @staticmethod
    def _last_recorded_utc(tx, battery_id):
        times = [parse_utc(r["event_utc"]) for r in tx.select("LifecycleEvents", battery_id=battery_id,
                                                                result="recorded") if r["event_utc"]]
        return max(times) if times else None

    def lifecycle_gate(self, tx, event):
        """Whether the state machine allows this event now: {allowed, reason_code, state_before, state_after}."""
        battery_id, kind, issuer, details = event["battery_id"], event["event_type"], event["issuer"], \
            event["details"]
        state = tx.get("PassportState", battery_id)
        before = state["lifecycle_status"] if state else None

        def refuse(code):
            return {"allowed": False, "reason_code": code, "state_before": before, "state_after": before}

        if state is None:
            return refuse("unknown_battery")
        if before in domain.CLOSED:
            return refuse(domain.PASSPORT_CLOSED)
        if not state["committed_version"]:
            return refuse("passport_not_published")
        if ALLOWED_FROM.get(kind) != before:
            return refuse("state_not_allowed")
        last = self._last_recorded_utc(tx, battery_id)
        if last and parse_utc(event["issued_utc"]) < last:
            return refuse("event_out_of_order")
        party = tx.get("Parties", issuer)
        role = party["role"] if party and not party["revoked"] else None
        if kind == "request_repurpose" and role != "second_life_operator" or \
                kind == "record_recycling_closure" and role != "recycler":
            return refuse("issuer_role_not_allowed")
        if issuer != state["holder_id"] and kind != "record_recycling_closure":
            return refuse("not_current_holder")
        holder = tx.get("Parties", state["holder_id"] or "")
        if kind == "record_recycling_closure" and holder and holder["role"] == "recycler" and \
                issuer != state["holder_id"]:
            return refuse("not_current_holder")
        if kind == "record_soh" and not _number(details.get("soh_pct"), 0, 100):
            return refuse("bad_soh")
        if kind == "record_ownership_change":
            new = tx.get("Parties", details.get("to_party_id") or "")
            if details.get("from_party_id") != state["holder_id"]:
                return refuse("not_current_holder")
            if new is None or new["revoked"] or new["party_id"] == state["holder_id"]:
                return refuse("unknown_new_holder")
        if kind == "record_end_of_life" and details.get("handed_to_party_id") is not None:
            new = tx.get("Parties", details["handed_to_party_id"]) if isinstance(
                details["handed_to_party_id"], str) else None
            if new is None or new["revoked"] or new["role"] != "recycler":
                return refuse("unknown_new_holder")
        if kind == "request_repurpose":
            gate = self._repurpose_gate(tx, state, details)
            if gate:
                return refuse(gate)
        return {"allowed": True, "reason_code": "ok", "state_before": before, "state_after": NEXT[kind]}

    def _repurpose_gate(self, tx, state, details):
        reference = details.get("safety_assessment_ref")
        if not isinstance(reference, str) or not reference.strip():
            return "safety_reference_missing"
        readings = self.soh_readings(tx, state["battery_id"])
        if not readings:
            return "soh_missing"
        if readings[-1]["soh_pct"] < SECOND_LIFE["minimum_soh_pct"]:
            return "soh_below_minimum"
        successor = details.get("successor_battery_id")
        if not isinstance(successor, str) or not SUBJECT.fullmatch(successor) or \
                tx.get("PassportState", successor) or tx.select("SecondLifeRequests", successor_id=successor):
            return "successor_unavailable"
        return None

    @staticmethod
    def recorded_event(tx, event_id):
        rows = tx.select("LifecycleEvents", message_id=event_id)
        return rows[0] if rows else None

    @staticmethod
    def _message_hash(tx, event_id):
        rows = [r for r in tx.select("ConnectorMessages", message_id=event_id) if r["result"] != "rejected"]
        return rows[0]["payload_sha256"] if rows else None

    def record_lifecycle_event(self, tx, event, actor, task_id=None, gate=None):
        """Record one routed event: applied with a new passport version, or quarantined with its reason."""
        prior = self.recorded_event(tx, event["event_id"])
        if prior:
            return {"status": "already_recorded", "event_id": event["event_id"], "result": prior["result"],
                    "reason_code": prior["reason_code"], "passport_version": prior["passport_version"]}
        gate = gate or self.lifecycle_gate(tx, event)
        state = tx.get("PassportState", event["battery_id"])
        recorded = gate["allowed"]
        row = {"lifecycle_event_id": f"LE-{len(tx.select('LifecycleEvents')) + 1:06d}",
               "message_id": event["event_id"], "battery_id": event["battery_id"], "event_type": event["event_type"],
               "issuer": event["issuer"], "event_utc": format_utc(parse_utc(event["issued_utc"])),
               "state_before": gate["state_before"], "state_after": gate["state_after"],
               "result": "recorded" if recorded else "quarantined", "reason_code": gate["reason_code"],
               "passport_version": state["committed_version"] + 1 if recorded else None,
               "payload_sha256": self._message_hash(tx, event["event_id"]),
               "details_json": json.dumps(event["details"], sort_keys=True)[:2000], "agent_task_id": task_id,
               "recorded_by": actor, "recorded_utc": self.clock()}
        tx.insert("LifecycleEvents", row)
        outcome = {"status": row["result"], "event_id": event["event_id"], "battery_id": event["battery_id"],
                   "event_type": event["event_type"], "reason_code": gate["reason_code"],
                   "state_before": gate["state_before"], "state_after": gate["state_after"]}
        if not recorded:
            self._audit(tx, actor, "lifecycle_event_quarantined", event["battery_id"],
                        {"event_id": event["event_id"], "event_type": event["event_type"],
                         "reason_code": gate["reason_code"]})
            return outcome
        details, changes = event["details"], {"lifecycle_status": gate["state_after"]}
        if event["event_type"] == "record_ownership_change":
            changes["holder_id"] = details["to_party_id"]
        if event["event_type"] == "record_end_of_life" and details.get("handed_to_party_id"):
            changes["holder_id"] = details["handed_to_party_id"]
        if event["event_type"] == "request_repurpose":
            outcome["request"] = self._open_request(tx, state, event)
        self._audit(tx, actor, "lifecycle_event_recorded", event["battery_id"],
                    {"event_id": event["event_id"], "event_type": event["event_type"],
                     "state_after": gate["state_after"]})
        decision = {"kind": "lifecycle_event", "event_type": event["event_type"], "event_id": event["event_id"],
                    "task_id": task_id}
        outcome["passport_version"] = self._commit_version(tx, event["battery_id"], decision, actor, changes)
        outcome["closed"] = gate["state_after"] in domain.CLOSED
        return outcome

    def _open_request(self, tx, state, event):
        details = event["details"]
        value = illustrative_value(state["rated_kwh"])
        request_id = f"SL-{len(tx.select('SecondLifeRequests')) + 1:04d}"
        tx.insert("SecondLifeRequests", {
            "request_id": request_id, "battery_id": state["battery_id"],
            "successor_id": details["successor_battery_id"], "requested_by": event["issuer"],
            "message_id": event["event_id"], "safety_assessment_ref": details["safety_assessment_ref"].strip(),
            "intended_use": str(details.get("intended_use") or "not stated")[:200],
            "soh_pct": self.soh_readings(tx, state["battery_id"])[-1]["soh_pct"], "illustrative_value_eur": value,
            "value_label": VALUE_LABEL, "status": "pending", "decision_reason": None, "decided_by": None,
            "decided_utc": None, "requested_utc": self.clock(), "row_version": 1})
        return {"request_id": request_id, "illustrative_value_eur": value,
                "event_value_matches": details.get("illustrative_value") == value}

    def _commit_version(self, tx, battery_id, decision, actor, changes):
        """Commit the next passport version from the last committed inputs plus the lifecycle changes."""
        svc = _service()
        current = tx.get("PassportState", battery_id)
        state = {**current, **changes}
        row = {**self.committed_row(tx, state), "lifecycle_status": state["lifecycle_status"],
               "holder_id": state["holder_id"]}
        inputs = self.inputs(tx, row)
        footprint = self.footprint(inputs)
        if footprint is None:
            raise svc.CommandError(409, "inputs_incomplete", "The committed inputs no longer calculate.")
        version = current["committed_version"] + 1
        state_change = {"battery_id": battery_id, **changes, "committed_version": version,
                        "updated_utc": self.clock()}
        if state["lifecycle_status"] in domain.CLOSED:
            state_change.update(eligibility=domain.BLOCKED, hold_reason=domain.PASSPORT_CLOSED)
        try:
            tx.update("PassportState", state_change, current["row_version"])
        except ConflictError as exc:
            raise svc.CommandError(409, "version_conflict", str(exc)) from exc
        self._write_version(tx, row, inputs, footprint, version, decision, actor,
                            self.lifecycle_view(tx, battery_id, state["lifecycle_status"], state["holder_id"]))
        return version

    # ---- second life --------------------------------------------------------------------------
    def decide_second_life(self, request_id, decision, expected_row_version, reason, actor, command_id):
        svc = _service()
        reason = (reason or "").strip() if isinstance(reason, str) else ""

        def action(tx):
            request = tx.get("SecondLifeRequests", request_id) if REQUEST_ID.match(request_id or "") else None
            if not request:
                raise svc.CommandError(404, "request_not_found", f"No second-life request {request_id}.")
            if decision not in DECISIONS:
                raise svc.CommandError(422, "bad_decision", f"decision must be one of {list(DECISIONS)}.")
            if request["status"] != "pending":
                raise svc.CommandError(409, "request_not_pending", f"{request_id} is {request['status']}.")
            if type(expected_row_version) is not int or request["row_version"] != expected_row_version:
                raise svc.CommandError(409, "version_conflict", "The request changed since you read it. Reload and "
                                       "retry.", current_row_version=request["row_version"])
            if not 5 <= len(reason) <= 400:
                raise svc.CommandError(422, "reason_required", "Give a reason of 5-400 characters.")
            state = tx.get("PassportState", request["battery_id"])
            if state["lifecycle_status"] != domain.SECOND_LIFE_PENDING:
                raise svc.CommandError(409, "state_not_allowed", "The passport is not awaiting a second-life decision.")
            now, approved = self.clock(), decision == "approve"
            try:
                tx.update("SecondLifeRequests", {
                    "request_id": request_id, "status": "approved" if approved else "rejected",
                    "decision_reason": reason, "decided_by": actor, "decided_utc": now}, expected_row_version)
            except ConflictError as exc:
                raise svc.CommandError(409, "version_conflict", str(exc)) from exc
            record = {"kind": "second_life_decision", "request_id": request_id, "decision": decision}
            body = {"request_id": request_id, "decision": decision, "battery_id": request["battery_id"],
                    "status": "approved" if approved else "rejected", "row_version": expected_row_version + 1,
                    "safety_assessment_ref": request["safety_assessment_ref"], "successor_id": None}
            if approved:
                successor = request["successor_id"]
                self._create_successor(tx, state, request, actor, record)
                body["passport_version"] = self._commit_version(
                    tx, request["battery_id"], record, actor, {"lifecycle_status": domain.REPURPOSED})
                body.update(successor_id=successor, lifecycle_status=domain.REPURPOSED,
                            illustrative_value_eur=request["illustrative_value_eur"], value_label=VALUE_LABEL)
            else:
                body["passport_version"] = self._commit_version(
                    tx, request["battery_id"], record, actor, {"lifecycle_status": domain.ACTIVE})
                body["lifecycle_status"] = domain.ACTIVE
            self._audit(tx, actor, "second_life_decided", request_id,
                        {"battery_id": request["battery_id"], "decision": decision,
                         "successor_id": body["successor_id"]})
            return 200, body

        payload = {"request_id": request_id, "decision": decision, "expected_row_version": expected_row_version,
                   "reason": reason, "subject": request_id or ""}
        return self._execute(command_id, "decide_second_life", payload, actor, action)

    def _create_successor(self, tx, state, request, actor, decision):
        """The successor passport: active, held by the operator, inheriting the predecessor's committed data."""
        predecessor, successor, now = state["battery_id"], request["successor_id"], self.clock()
        version = tx.get("PassportVersions", predecessor, state["committed_version"])
        for battery_id, linked_id, link_type in ((predecessor, successor, "successor"),
                                                 (successor, predecessor, "predecessor")):
            tx.insert("PassportLinks", {"battery_id": battery_id, "linked_id": linked_id, "link_type": link_type,
                                        "created_by": actor, "created_utc": now})
        row = {**self._inherited_row(tx, successor, version["build_id"], predecessor),
               "lifecycle_status": domain.ACTIVE, "holder_id": request["requested_by"]}
        inputs = self.inputs(tx, row)
        footprint = self.footprint(inputs)
        tx.insert("PassportState", {
            "battery_id": successor, "model_id": row["model_id"], "build_id": row["build_id"],
            "input_hash": row["input_hash"], "readiness": row["readiness"], "eligibility": domain.ELIGIBLE,
            "hold_reason": None, "lot_ids": row["lot_ids"], "chemistry": row["chemistry"],
            "rated_kwh": row["rated_kwh"], "plant_id": row["plant_id"], "batch_id": row["batch_id"],
            "lifecycle_status": domain.ACTIVE, "holder_id": request["requested_by"],
            "footprint_per_kwh": row["footprint_per_kwh"], "footprint_verified": row["footprint_verified"],
            "committed_version": 1, "committed_input_hash": row["input_hash"], "row_version": 1,
            "updated_utc": now})
        self._write_version(tx, row, inputs, footprint, 1, {**decision, "kind": "successor_created",
                                                            "predecessor_id": predecessor}, actor,
                            self.lifecycle_view(tx, successor))
        self._audit(tx, actor, "successor_created", successor, {"predecessor_id": predecessor})

    # ---- reads --------------------------------------------------------------------------------
    def list_second_life(self):
        with self.store.read() as tx:
            return sorted(tx.select("SecondLifeRequests"), key=lambda r: r["requested_utc"])

    def second_life_detail(self, request_id):
        if not REQUEST_ID.match(request_id or ""):
            return None
        with self.store.read() as tx:
            request = tx.get("SecondLifeRequests", request_id)
            if not request:
                return None
            life = self.lifecycle_view(tx, request["battery_id"])
            state = tx.get("PassportState", request["battery_id"])
            return {"request": request, "lifecycle": life, "state": state,
                    "minimum_soh_pct": SECOND_LIFE["minimum_soh_pct"],
                    "value_basis": f"{SECOND_LIFE['value_eur_per_rated_kwh']} {SECOND_LIFE['currency']} per rated kWh",
                    "events": tx.select("LifecycleEvents", battery_id=request["battery_id"])}

    def passport_links(self, battery_id):
        with self.store.read() as tx:
            state = tx.get("PassportState", battery_id)
            if not state:
                return None
            return {"battery_id": battery_id, "lifecycle_status": state["lifecycle_status"],
                    "status_label": status_label(state["lifecycle_status"]),
                    "links": [{"battery_id": r["linked_id"], "link_type": r["link_type"]}
                              for r in tx.select("PassportLinks", battery_id=battery_id)]}

    def lifecycle_events(self, battery_id=None, result=None):
        with self.store.read() as tx:
            rows = tx.select("LifecycleEvents")
        return [r for r in rows if (battery_id is None or r["battery_id"] == battery_id)
                and (result is None or r["result"] == result)]

    def quarantined_events(self):
        return self.lifecycle_events(result="quarantined")
