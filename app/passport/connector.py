"""A mock data-space connector: recorded agreements and verified partner messages.

It is labelled "mock" in every response. It imitates the shape of a connector exchange (an agreement between sender
and receiver, then signed messages under it) without any connector protocol, catalogue, policy engine or conformance
with a data-space standard. Trust in a message's content comes from its issuer's signature in the party registry; the
caller only needs the Passport.Connector role to deliver it.

A verified supplier claim becomes evidence for its lot (the stored bytes are the canonical payload, so the evidence
hash is the claim hash a package lot references). A verified lifecycle event (state of health, ownership change,
repurpose request, end of life, recycling closure) that the lifecycle state machine allows now is routed to a Lifecycle
agent task; one it does not allow (closed or unpublished passport, wrong state, holder or role, out of order) is
quarantined with a reason code and never applied.
"""

import hashlib
import json
import re
import uuid

from . import domain, lifecycle, parties, signed_messages
from .service import COMMAND_ID, CommandError
from .signed_messages import CLAIM_TYPE, MESSAGE_TYPES, Rejected
from .store import parse_utc

LABEL = "mock"
CORRELATION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
AGREEMENT_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
ROUTES = {CLAIM_TYPE: "evidence", **{kind: "lifecycle" for kind in parties.EVENT_TYPES}}
COMPARED = ("name", "role", "key_id", "public_key", "lot_ids", "event_types", "revoked")


def message_command(message_id):
    return "connector-msg-" + hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:40]


lifecycle_event = lifecycle.event_of


def routed_event(tx, event_id):
    """The verified lifecycle event payload routed under this message ID, or None."""
    for row in tx.select("ConnectorMessages", message_id=event_id):
        if row["result"] == "routed" and row["message_type"] in parties.EVENT_TYPES:
            return json.loads(row["payload_json"])
    return None


def labelled(body):
    return {"connector": LABEL, **body}


class Connector:
    def __init__(self, service, registry=None):
        self.service = service
        self.registry = registry or {}
        parties.check_registry(self.registry)

    # ---- party registry ---------------------------------------------------------------------
    def sync(self, document=None, actor="system:party-registry"):
        """Copy a registry into Parties. New parties, changed keys or permissions and revocations are updates with
        history and an audit entry; a party missing from a later registry is revoked. Unchanged parties are left
        alone, so the same registry can be applied any number of times.

        The stored rows are compared on every call instead of remembering a finished sync in this process: the
        database may have been recreated while the app kept running (data reset). The comparison reads without the
        app lock, so only a registry that differs from the stored rows takes a write transaction."""
        document = self.registry if document is None else document
        entries = parties.check_registry(document)
        if not entries:
            return 0
        self.service.require_schema()
        digest = parties.registry_sha256(document)
        with self.service.store.read() as tx:
            if not self._plan(entries, digest, tx.select("Parties"), self.service.clock()):
                return 0
        with self.service.store.transaction() as tx:
            plan = self._plan(entries, digest, tx.select("Parties"), self.service.clock())
            for action, row, old, details in plan:
                if old is None:
                    tx.insert("Parties", {**row, "row_version": 1})
                else:
                    tx.update("Parties", row, old["row_version"])
                self.service._audit(tx, actor, action, row["party_id"], details)
        return len(plan)

    @staticmethod
    def _plan(entries, digest, rows, now):
        """The Parties writes that bring the stored rows in line with the registry: (action, row, old row, audit)."""
        current = {row["party_id"]: row for row in rows}
        plan = []
        for entry in entries:
            row = parties.party_row(entry, digest, now)
            old = current.pop(entry["party_id"], None)
            details = {"registry_sha256": digest, "revoked": entry["revoked"]}
            if old is None:
                plan.append(("party_registered", row, None, details))
            elif any(old[k] != row[k] for k in COMPARED):
                action = "party_revoked" if row["revoked"] and not old["revoked"] else "party_updated"
                plan.append((action, row, old, details))
        for old in current.values():
            if not old["revoked"]:
                plan.append(("party_revoked", {"party_id": old["party_id"], "revoked": 1, "registry_sha256": digest,
                                               "updated_utc": now}, old,
                             {"registry_sha256": digest, "revoked": True, "reason": "not_in_registry"}))
        return plan

    def parties(self):
        try:
            self.sync()
        except CommandError:
            pass  # a schema mismatch refuses writes; the rows already recorded can still be read
        with self.service.store.read() as tx:
            return [parties.public_view(row) for row in tx.select("Parties")]

    # ---- agreements -------------------------------------------------------------------------
    def record_agreement(self, body, actor):
        body = body if isinstance(body, dict) else {}
        types = body.get("message_types")
        payload = {k: body.get(k) for k in ("agreement_id", "sender", "purpose", "valid_from", "valid_until")}
        payload.update(message_types=sorted(set(types)) if isinstance(types, list) else types,
                       subject=str(body.get("agreement_id") or ""))
        try:
            self.sync()
        except CommandError as err:
            return err.status, labelled(err.body)

        def action(tx):
            agreement_id, sender, purpose = payload["agreement_id"], payload["sender"], payload["purpose"]
            if not isinstance(agreement_id, str) or not AGREEMENT_ID.fullmatch(agreement_id):
                raise CommandError(422, "bad_agreement_id", "Use an 8-64 character agreement ID.")
            if not isinstance(sender, str) or tx.get("Parties", sender) is None:
                raise CommandError(422, "unknown_party", "The sender must be a party in the registry.")
            if not isinstance(purpose, str) or not 5 <= len(purpose.strip()) <= 200:
                raise CommandError(422, "purpose_required", "Give a purpose of 5-200 characters.")
            types = payload["message_types"]
            if not isinstance(types, list) or not types or any(t not in MESSAGE_TYPES for t in types):
                raise CommandError(422, "bad_message_types", f"message_types must be from {list(MESSAGE_TYPES)}.")
            try:
                start, end = parse_utc(payload["valid_from"]), parse_utc(payload["valid_until"])
            except (ValueError, TypeError, AttributeError) as error:
                raise CommandError(422, "bad_validity", "valid_from and valid_until are UTC timestamps.") from error
            if start >= end:
                raise CommandError(422, "bad_validity", "valid_from must be before valid_until.")
            if tx.get("ConnectorAgreements", agreement_id):
                raise CommandError(409, "agreement_exists", f"{agreement_id} is already recorded.")
            row = {"agreement_id": agreement_id, "sender": sender, "receiver": parties.AUDIENCE,
                   "purpose": purpose.strip(), "message_types": ",".join(types), "valid_from": payload["valid_from"],
                   "valid_until": payload["valid_until"], "recorded_by": actor, "recorded_utc": self.service.clock()}
            tx.insert("ConnectorAgreements", row)
            self.service._audit(tx, actor, "connector_agreement_recorded", agreement_id,
                                {"sender": sender, "message_types": types})
            return 201, labelled({**row, "message_types": types})

        status, result = self.service._execute(body.get("command_id"), "connector_agreement", payload, actor, action)
        return status, labelled(result)

    @staticmethod
    def agreement(tx, sender, kind, now):
        """The latest agreement that lets this sender deliver this message type now, or None."""
        rows = [r for r in tx.select("ConnectorAgreements", sender=sender)
                if kind in domain.split_ids(r["message_types"])
                and parse_utc(r["valid_from"]) <= now <= parse_utc(r["valid_until"])]
        return max(rows, key=lambda r: r["recorded_utc"]) if rows else None

    # ---- messages ---------------------------------------------------------------------------
    def receive(self, body, actor):
        """Check the agreement and the signature, then store and route one partner message. The same message ID with
        the same payload returns the original result; with another payload it is refused as message_conflict. A
        refused message is logged but does not claim its message ID, so a forged copy cannot block the genuine one."""
        body = body if isinstance(body, dict) else {}
        envelope = body.get("envelope")
        payload = envelope.get("payload") if isinstance(envelope, dict) else None
        payload = payload if isinstance(payload, dict) else None
        message_id = payload.get("message_id") if payload else None
        message_id = message_id if isinstance(message_id, str) and signed_messages.MESSAGE_ID.fullmatch(message_id) \
            else None
        correlation_id = body.get("correlation_id", message_id)
        if correlation_id is not None and (not isinstance(correlation_id, str)
                                           or not CORRELATION_ID.fullmatch(correlation_id)):
            return 422, labelled({"error": "bad_correlation_id", "message": "Use a 1-64 character correlation ID."})
        info = {"message_id": message_id, "correlation_id": correlation_id,
                "payload_sha256": domain.sha256(domain.canonical(payload)) if payload else None}
        try:
            self.sync()
            if message_id:
                with self.service.store.read() as tx:
                    replay = self.service._replay(tx, message_command(message_id), "connector_message",
                                                  info["payload_sha256"])
                if replay:
                    return replay[0], labelled(replay[1])
            now = parse_utc(self.service.clock())
            with self.service.store.read() as tx:
                registry = [parties.entry_of(row) for row in tx.select("Parties")]
                payload, entry = signed_messages.verify(envelope, registry, now)
                kind = signed_messages.message_type(payload)
                agreement = self.agreement(tx, entry["party_id"], kind, now)
                if agreement is None:
                    raise Rejected("no_agreement", f"No valid agreement lets {entry['party_id']} send {kind}.")
                if kind != CLAIM_TYPE and tx.get("PassportState", payload["battery_id"]) is None:
                    raise Rejected("unknown_battery", "The event names a battery this app does not hold.")
        except CommandError as err:
            if err.body["error"] == "command_id_reused":
                return self._reject(Rejected("message_conflict", "This message ID was received with another "
                                                                 "payload."), payload, info, actor)
            return err.status, labelled(err.body)
        except Rejected as rejected:
            return self._reject(rejected, payload, info, actor)
        return self._accept(payload, agreement, info, actor)

    def _accept(self, payload, agreement, info, actor):
        kind, message_id = signed_messages.message_type(payload), payload["message_id"]
        result, reason = ("verified" if kind == CLAIM_TYPE else "routed"), "ok"
        body = {"message_id": message_id, "correlation_id": info["correlation_id"], "message_type": kind,
                "sender": payload["issuer"], "subject": signed_messages.subject(payload),
                "agreement_id": agreement["agreement_id"], "result": result, "reason_code": reason,
                "route": ROUTES.get(kind), "payload_sha256": info["payload_sha256"], "evidence_id": None}
        if kind == CLAIM_TYPE:
            evidence_kind = "supplier_claim" if payload["claim_version"] == 1 else "corrected_claim"
            status, uploaded = self.service.upload_evidence(
                evidence_kind, payload["lot_id"], f"{message_id}.json", "application/json",
                domain.canonical(payload), f"connector:{payload['issuer']}",
                "connector-ev-" + hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:40])
            if status >= 400:
                return status, labelled(uploaded)
            body["evidence_id"] = uploaded["evidence_id"]

        def action(tx):
            outcome, gate = dict(body), None
            if kind != CLAIM_TYPE:
                event = lifecycle.event_of(payload)
                gate = self.service.lifecycle_gate(tx, event)
                if gate["allowed"]:
                    outcome["lifecycle_event"] = event
                else:
                    outcome.update(result="quarantined", reason_code=gate["reason_code"], route=None)
            receipt = self._log(tx, payload, info, actor, outcome["result"], outcome["reason_code"],
                                agreement["agreement_id"], body["evidence_id"],
                                domain.canonical(payload).decode("utf-8"))
            self.service._audit(tx, actor, f"connector_message_{outcome['result']}", body["subject"],
                                {"message_id": message_id, "sender": body["sender"], "message_type": kind,
                                 "reason_code": outcome["reason_code"], "evidence_id": body["evidence_id"]})
            if gate and not gate["allowed"]:
                self.service.record_lifecycle_event(tx, lifecycle.event_of(payload), actor, gate=gate)
                return 202, {**outcome, "receipt_id": receipt}
            if gate:
                self.service.enqueue_agent_trigger(tx, {
                    "kind": "lifecycle_event", "subject": payload["battery_id"],
                    "battery_id": payload["battery_id"], "event": payload})
            return 201, {**outcome, "receipt_id": receipt}

        status, result_body = self.service._execute(message_command(message_id), "connector_message",
                                                    {"subject": body["subject"]}, actor, action,
                                                    payload_hash=info["payload_sha256"])
        if result_body.get("error") == "command_id_reused":
            return self._reject(Rejected("message_conflict", "This message ID was received with another payload."),
                                payload, info, actor)
        return status, labelled(result_body)

    def _log(self, tx, payload, info, actor, result, reason, agreement_id=None, evidence_id=None, text=None):
        receipt = f"CM-{uuid.uuid4().hex[:12].upper()}"
        payload = payload or {}
        kind, subject = signed_messages.message_type(payload), signed_messages.subject(payload)
        sender = payload.get("issuer")
        tx.insert("ConnectorMessages", {
            "receipt_id": receipt, "message_id": info["message_id"],
            "sender": sender if isinstance(sender, str) and parties.PARTY_ID.fullmatch(sender) else None,
            "message_type": kind if kind in MESSAGE_TYPES else None,
            "subject": subject if isinstance(subject, str) and signed_messages.SUBJECT.fullmatch(subject) else None,
            "correlation_id": info["correlation_id"], "payload_sha256": info["payload_sha256"],
            "payload_json": text, "agreement_id": agreement_id, "result": result, "reason_code": reason,
            "evidence_id": evidence_id, "received_by": actor, "received_utc": self.service.clock()})
        return receipt

    def _reject(self, rejected, payload, info, actor):
        kind = signed_messages.message_type(payload) if payload else None
        body = {"error": rejected.code, "message": rejected.message, "result": "rejected",
                "reason_code": rejected.code, "message_id": info["message_id"],
                "correlation_id": info["correlation_id"],
                "message_type": kind if kind in MESSAGE_TYPES else None}
        try:
            self.service.require_schema()
            with self.service.store.transaction() as tx:
                body["receipt_id"] = self._log(tx, payload, info, actor, "rejected", rejected.code)
                self.service._audit(tx, actor, "connector_message_rejected", info["message_id"] or "unknown",
                                    {"reason_code": rejected.code, "sender": (payload or {}).get("issuer")})
        except CommandError as err:
            return err.status, labelled(err.body)
        return rejected.status, labelled(body)

    def messages(self, limit=200):
        """The message log without payloads: who sent what, when, under which agreement and with what result."""
        with self.service.store.read() as tx:
            rows = sorted(tx.select("ConnectorMessages"), key=lambda r: (r["received_utc"], r["receipt_id"]))
        return [{k: v for k, v in r.items() if k != "payload_json"} for r in rows[-limit:]]

    def agreements(self):
        with self.service.store.read() as tx:
            return tx.select("ConnectorAgreements")


__all__ = ["COMMAND_ID", "Connector", "LABEL", "lifecycle_event", "routed_event"]
