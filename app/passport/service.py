"""Passport commands. Every change runs in one database transaction and is recorded as evidence.

Rules that matter for the story:
  * A command ID is used once. Repeating it with the same request returns the stored result.
  * The ledger row (with the snapshot's SHA-256) is committed first; the Blob snapshot is written
    afterwards by the outbox worker, so a crash between the two can be finished later.
  * A battery is published automatically only when its inputs are ready, its batch pins the calculation method and
    factor set this app has, its composition is valid and no sourcing review or compliance case is open for a lot it
    uses. Otherwise a person must resolve the review (or Compliance accept the case) with new evidence.
  * Writes are refused while the database reports another schema version than this app (schema_mismatch).
"""

import json
import re
import uuid
from datetime import datetime, timezone

from . import calculator, domain
from .blobs import BlobExists
from .compliance import ComplianceCommands
from .lifecycle import LifecycleCommands
from .lifecycle import status_label as lifecycle_status_label
from .package import check_package
from .store import SCHEMA_VERSION, ConflictError, format_utc, parse_utc

COMMAND_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
REPORT_SCHEMA_VERSION = "2"
FOLDER = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
LOT_ID = re.compile(r"^[A-Z0-9-]{1,50}$")
EVIDENCE_KINDS = {"supplier_claim", "transport_record", "corrected_claim", "other", "adverse_report", "audit_report"}
JSON_EVIDENCE_KINDS = {"adverse_report", "audit_report"}
CLAIM_KINDS = {"supplier_claim", "corrected_claim"}
MAX_JSON_EVIDENCE_BYTES = 16 * 1024
CONTENT_TYPES = {"application/json", "application/pdf", "text/plain", "image/png", "image/jpeg"}


class CommandError(Exception):
    def __init__(self, status, code, message, **detail):
        super().__init__(message)
        self.status, self.body = status, {"error": code, "message": message, **detail}


def utc_now():
    return format_utc(datetime.now(timezone.utc))


def safe_file_name(name):
    base = re.sub(r"[^A-Za-z0-9._-]", "_", (name or "file").replace("\\", "/").split("/")[-1])[:100]
    return base.strip("._") or "file"


def as_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


def json_evidence_problem(content_type, data):
    """Adverse and audit reports are read by agents: JSON objects of at most 16 KB. None when acceptable."""
    if len(data) > MAX_JSON_EVIDENCE_BYTES:
        return {"error": "evidence_too_large", "message": f"This kind allows at most {MAX_JSON_EVIDENCE_BYTES} bytes."}
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        parsed = None
    if content_type != "application/json" or not isinstance(parsed, dict):
        return {"error": "evidence_not_json", "message": "This kind must be a JSON object (application/json)."}
    return None


class PassportService(ComplianceCommands, LifecycleCommands):
    def __init__(self, store, blobs, clock=utc_now, max_upload_bytes=5 * 1024 * 1024, agent_mode=False):
        self.store, self.blobs, self.clock = store, blobs, clock
        self.max_upload_bytes = max_upload_bytes
        self.agent_mode = agent_mode
        self.workflow_dispatcher = None

    # ---- command plumbing -------------------------------------------------------------------
    def schema_ok(self):
        return self.store.schema_version() == SCHEMA_VERSION

    def require_schema(self):
        if not self.schema_ok():
            raise CommandError(503, "schema_mismatch",
                               f"The database does not report schema version {SCHEMA_VERSION}; writes are refused. "
                               "Use the matching app version or explicitly provision a fresh database.")

    @staticmethod
    def _replay(tx, command_id, command_type, payload_hash):
        prior = tx.get("Commands", command_id)
        if not prior:
            return None
        if prior["command_type"] != command_type or prior["payload_sha256"] != payload_hash:
            raise CommandError(409, "command_id_reused", "This command ID was already used for a different request.")
        return prior["status_code"], dict(json.loads(prior["result_json"]), replayed=True)

    def _execute(self, command_id, command_type, payload, actor, action, payload_hash=None):
        if not COMMAND_ID.match(command_id or ""):
            return 422, {"error": "command_id_required", "message": "Send a command_id (8-64 letters, digits, dashes)."}
        try:
            self.require_schema()
        except CommandError as err:
            return err.status, err.body
        payload_hash = payload_hash or domain.sha256(domain.canonical({"type": command_type, **payload}))
        try:
            with self.store.transaction() as tx:
                replay = self._replay(tx, command_id, command_type, payload_hash)
                if replay:
                    return replay
                status, result = action(tx)
                self._record(tx, command_id, command_type, payload_hash, actor, status, result)
                return status, result
        except CommandError as err:
            if err.body["error"] == "command_id_reused":
                return err.status, err.body
            with self.store.transaction() as tx:
                replay = self._replay(tx, command_id, command_type, payload_hash)
                if replay:
                    return replay
                self._audit(tx, actor, f"{command_type}_rejected", payload.get("subject", ""), err.body)
                self._record(tx, command_id, command_type, payload_hash, actor, err.status, err.body)
            return err.status, err.body

    def _record(self, tx, command_id, command_type, payload_hash, actor, status, result):
        tx.insert("Commands", {
            "command_id": command_id, "command_type": command_type, "payload_sha256": payload_hash,
            "actor": actor, "status_code": status, "result_json": json.dumps(result, sort_keys=True),
            "recorded_utc": self.clock()})

    def _audit(self, tx, actor, action, subject, detail):
        tx.insert("AuditEvents", {"event_utc": self.clock(), "actor": actor, "action": action,
                                  "subject": subject[:100], "detail": json.dumps(detail, sort_keys=True)[:2000]})

    # ---- passport state ---------------------------------------------------------------------
    @staticmethod
    def inputs(tx, row):
        """Everything the rules and the calculator need for one package battery row, read from the ledger."""
        batch = tx.get("Batches", row["build_id"], row["batch_id"]) if row["batch_id"] else None
        composition = tx.select("PackageComposition", build_id=row["build_id"],
                                battery_id=row.get("inherited_from") or row["battery_id"])
        lots = {lot_id: tx.get("PackageLots", row["build_id"], lot_id)
                for lot_id in {p["lot_id"] for p in composition}}
        supported = bool(batch) and calculator.supported(batch["method_version"], batch["factor_set_version"])
        return {"battery": row, "batch": batch, "composition": composition,
                "lots": {k: v for k, v in lots.items() if v}, "method_supported": supported}

    @staticmethod
    def footprint(inputs):
        """The calculator's result, or None when the battery cannot be calculated (the gates say why)."""
        battery = inputs["battery"]
        if battery["readiness"] != domain.READY or not inputs["method_supported"] or \
                domain.composition_problems(battery, inputs["composition"], inputs["lots"]):
            return None
        try:
            return calculator.calculate(battery, inputs["batch"], inputs["composition"], inputs["lots"])
        except calculator.MethodMismatch:
            return None

    def _apply(self, tx, row, open_lots, actor, decision):
        """Recalculate one battery and commit a new passport version when it is eligible and new."""
        inputs = self.inputs(tx, row)
        case_lots = {c["lot_id"] for c in tx.select("ComplianceCases", status="open")}
        eligibility, hold_reason = domain.eligibility(inputs, open_lots, case_lots)
        footprint = self.footprint(inputs)
        if eligibility == domain.ELIGIBLE and footprint is None:
            eligibility, hold_reason = domain.BLOCKED, domain.METHOD_UNPINNED
        state = tx.get("PassportState", row["battery_id"])
        status = state["lifecycle_status"] if state else row["lifecycle_status"]
        holder = state["holder_id"] if state else row["holder_id"]
        if status in domain.CLOSED:
            eligibility, hold_reason = domain.BLOCKED, domain.PASSPORT_CLOSED
        committed_version = state["committed_version"] if state else 0
        committed_hash = state["committed_input_hash"] if state else None
        publication_requested = not self.agent_mode or decision["kind"] == "agent_publication"
        committed_now = publication_requested and eligibility == domain.ELIGIBLE and committed_hash != row["input_hash"]
        if committed_now:
            committed_version += 1
            committed_hash = row["input_hash"]
            self._write_version(tx, {**row, "lifecycle_status": status, "holder_id": holder}, inputs, footprint,
                                committed_version, decision, actor,
                                self.lifecycle_view(tx, row["battery_id"], status, holder))
        new_state = {
            "battery_id": row["battery_id"], "model_id": row["model_id"], "build_id": row["build_id"],
            "input_hash": row["input_hash"], "readiness": row["readiness"], "eligibility": eligibility,
            "hold_reason": hold_reason, "lot_ids": row["lot_ids"], "chemistry": row["chemistry"],
            "rated_kwh": row["rated_kwh"], "plant_id": row["plant_id"], "batch_id": row["batch_id"],
            "lifecycle_status": status, "holder_id": holder,
            "footprint_per_kwh": row["footprint_per_kwh"], "footprint_verified": row["footprint_verified"],
            "committed_version": committed_version, "committed_input_hash": committed_hash}
        if not state:
            tx.insert("PassportState", {**new_state, "row_version": 1, "updated_utc": self.clock()})
        elif any(state[k] != v for k, v in new_state.items()):
            tx.update("PassportState", {**new_state, "updated_utc": self.clock()}, state["row_version"])
        return {"battery_id": row["battery_id"], "eligibility": eligibility, "hold_reason": hold_reason,
                "committed_version": committed_version, "committed_now": committed_now}

    def _write_version(self, tx, row, inputs, footprint, version, decision, actor, lifecycle):
        """Ledger row first (with the snapshot hash), then the outbox entry that writes the Blob snapshot."""
        committed_utc = self.clock()
        snap = domain.snapshot({**inputs, "battery": row}, footprint, version, decision, actor, committed_utc,
                               lifecycle)
        data = domain.canonical(snap)
        path = domain.snapshot_path(row["battery_id"], version)
        tx.insert("PassportVersions", {
            "battery_id": row["battery_id"], "version": version, "build_id": row["build_id"],
            "input_hash": row["input_hash"], "snapshot_sha256": domain.sha256(data), "snapshot_path": path,
            "decision": decision["kind"], "review_id": decision.get("review_id"),
            "committed_by": actor, "committed_utc": committed_utc})
        tx.insert("Outbox", {
            "kind": "write_snapshot", "subject": f"{row['battery_id']}/v{version}",
            "payload": json.dumps({"battery_id": row["battery_id"], "version": version,
                                   "snapshot": data.decode("utf-8")}),
            "status": "pending", "attempts": 0, "created_utc": self.clock()})
        self._audit(tx, actor, "passport_committed", row["battery_id"], {"version": version, "decision": decision})

    def _reevaluate_lot(self, tx, lot_id, actor, decision):
        open_lots = {r["lot_id"] for r in tx.select("Reviews", status="open")}
        results = []
        for state in tx.select("PassportState"):
            if lot_id in domain.split_ids(state["lot_ids"]):
                row = tx.get("PackageBatteries", state["build_id"], state["battery_id"])
                if row is not None:
                    results.append(self._apply(tx, row, open_lots, actor, decision))
        return results

    # ---- commands -----------------------------------------------------------------------------
    def import_package(self, intake_folder, actor, command_id):
        """Read a Fabric gold package from Blob intake, check it, and apply it to the passports."""
        if not FOLDER.match(intake_folder or ""):
            return 422, {"error": "bad_folder", "message": "intake_folder must be one folder name"}
        prefix = f"{intake_folder}/"
        files = {name[len(prefix):]: self.blobs.get("intake", name) for name in self.blobs.list("intake", prefix)}
        check = check_package(files)

        def action(tx):
            if not files:
                raise CommandError(404, "package_not_found", f"Nothing found in intake/{intake_folder}/")
            if not check.ok:
                raise CommandError(422, "package_rejected", "The package is incomplete or changed.",
                                   problems=check.errors)
            m = check.manifest
            existing = tx.get("Packages", m["build_id"])
            if existing:
                if existing["manifest_sha256"] == check.manifest_sha256:
                    return 200, {"outcome": "already_imported", "build_id": m["build_id"]}
                raise CommandError(409, "build_conflict", "This build ID was imported with a different manifest.")
            imported = tx.select("Packages")
            latest = max(imported, key=lambda p: p["build_sequence"]) if imported else None
            if latest and m["build_sequence"] <= latest["build_sequence"]:
                raise CommandError(409, "stale_package", "A newer package was already imported.",
                                   latest_build_id=latest["build_id"], latest_build_sequence=latest["build_sequence"])
            tx.insert("Packages", {
                "build_id": m["build_id"], "build_sequence": m["build_sequence"],
                "manifest_sha256": check.manifest_sha256, "series_id": m.get("series_id"),
                "battery_count": len(check.batteries), "rules_version": m.get("rules_version"),
                "method_version": m.get("method_version"), "factor_set_version": m.get("factor_set_version"),
                "intake_folder": intake_folder, "imported_by": actor, "imported_utc": self.clock()})
            for batch in check.batches:
                tx.insert("Batches", domain.batch_row(m["build_id"], batch))
            for lot in check.lots:
                tx.insert("PackageLots", domain.lot_row(m["build_id"], lot))
            parts = {}
            for part in check.composition:
                tx.insert("PackageComposition", domain.composition_row(m["build_id"], part))
                parts.setdefault(part["battery_id"], []).append(part)
            open_lots = {r["lot_id"] for r in tx.select("Reviews", status="open")}
            decision = {"kind": "clean_case_rule", "review_id": None, "evidence_ids": []}
            results = []
            for battery in check.batteries:
                composition = parts.get(battery["battery_id"], [])
                draft = domain.package_battery_row(m["build_id"], battery, composition, None)
                row = domain.package_battery_row(m["build_id"], battery, composition,
                                                 self.footprint(self.inputs(tx, draft)))
                tx.insert("PackageBatteries", row)
                results.append(self._apply(tx, row, open_lots, actor, decision))
                for lot_id in self._critical_lots_for_battery(tx, m["build_id"], battery["battery_id"]):
                    self._enqueue_supplier_trigger(tx, lot_id, battery["battery_id"])
            self._audit(tx, actor, "package_imported", m["build_id"],
                        {"build_sequence": m["build_sequence"], "manifest_sha256": check.manifest_sha256})
            return 200, {"outcome": "imported", "build_id": m["build_id"],
                         "build_sequence": m["build_sequence"], "batteries": results}

        return self._execute(command_id, "import_package",
                             {"intake_folder": intake_folder, "subject": intake_folder,
                              "files": {n: domain.sha256(b or b"") for n, b in files.items()}},
                             actor, action)

    def upload_evidence(self, kind, lot_id, file_name, content_type, data, actor, command_id):
        """Store a file in Blob, prove the stored bytes, then record it in the ledger."""
        if kind not in EVIDENCE_KINDS:
            return 422, {"error": "bad_kind", "message": f"kind must be one of {sorted(EVIDENCE_KINDS)}"}
        if not LOT_ID.match(lot_id or ""):
            return 422, {"error": "bad_lot", "message": "lot_id must look like LOT-A"}
        content_type = (content_type or "").split(";")[0].strip()
        if content_type not in CONTENT_TYPES:
            return 422, {"error": "bad_type", "message": f"content type must be one of {sorted(CONTENT_TYPES)}"}
        if not data or len(data) > self.max_upload_bytes:
            return 422, {"error": "bad_size", "message": f"file must have 1-{self.max_upload_bytes} bytes"}
        if kind in JSON_EVIDENCE_KINDS:
            problem = json_evidence_problem(content_type, data)
            if problem:
                return 422, problem
        file_name = safe_file_name(file_name)
        digest = domain.sha256(data)
        payload = {"kind": kind, "lot_id": lot_id, "file_name": file_name, "sha256": digest, "subject": lot_id}
        payload_hash = domain.sha256(domain.canonical({"type": "upload_evidence", **payload}))
        if not COMMAND_ID.match(command_id or ""):
            return self._execute(command_id, "upload_evidence", payload, actor, None)
        try:
            self.require_schema()
            with self.store.transaction() as tx:
                replay = self._replay(tx, command_id, "upload_evidence", payload_hash)
        except CommandError as err:
            return err.status, err.body
        if replay:
            return replay
        evidence_id = f"EV-{uuid.uuid4().hex[:12].upper()}"
        path = f"{evidence_id}/{file_name}"
        version_id = self.blobs.put("evidence", path, data, content_type, overwrite=False)
        stored = self.blobs.get("evidence", path, version_id)
        if stored is None or domain.sha256(stored) != digest:
            return 502, {"error": "blob_verify_failed",
                         "message": "Stored bytes differ from the upload; nothing recorded."}

        def action(tx):
            row = {"evidence_id": evidence_id, "kind": kind, "lot_id": lot_id, "file_name": file_name,
                   "content_type": content_type, "sha256": digest, "size_bytes": len(data),
                   "blob_path": path, "blob_version_id": version_id, "uploaded_by": actor,
                   "uploaded_utc": self.clock()}
            tx.insert("Evidence", row)
            self._audit(tx, actor, "evidence_recorded", evidence_id, {"lot_id": lot_id, "sha256": digest})
            reopened = self._reopen_on_adverse(tx, lot_id, evidence_id, actor) if kind == "adverse_report" else None
            self._enqueue_lot_triggers(tx, lot_id)
            return 201, {"evidence_id": evidence_id, "sha256": digest, "blob_version_id": version_id,
                         "lot_id": lot_id, "kind": kind, "reopened_case": reopened}

        return self._execute(command_id, "upload_evidence", payload, actor, action)

    def _evidence_for_lot(self, tx, evidence_ids, lot_id):
        if not evidence_ids:
            raise CommandError(422, "evidence_required", "Attach at least one evidence item.")
        if len(evidence_ids) > 20:
            raise CommandError(422, "too_much_evidence", "Attach at most 20 evidence items.")
        for evidence_id in evidence_ids:
            row = tx.get("Evidence", evidence_id)
            if not row or row["lot_id"] != lot_id:
                raise CommandError(422, "evidence_not_found", f"{evidence_id} is not recorded for {lot_id}.")

    def open_review(self, lot_id, reason, evidence_ids, actor, command_id):
        evidence_ids = sorted(set(as_list(evidence_ids)))
        reason = (reason or "").strip()

        def action(tx):
            if not LOT_ID.match(lot_id or ""):
                raise CommandError(422, "bad_lot", "lot_id must look like LOT-A")
            if not 5 <= len(reason) <= 400:
                raise CommandError(422, "reason_required", "Give a reason of 5-400 characters.")
            self._evidence_for_lot(tx, evidence_ids, lot_id)
            if tx.select("Reviews", lot_id=lot_id, status="open"):
                raise CommandError(409, "review_already_open", f"{lot_id} already has an open review.")
            review_id = f"RV-{uuid.uuid4().hex[:8].upper()}"
            tx.insert("Reviews", {
                "review_id": review_id, "lot_id": lot_id, "status": "open", "reason": reason,
                "opening_evidence": ",".join(evidence_ids), "opened_by": actor, "opened_utc": self.clock(),
                "reason_origin": "person", "provenance_id": None, "row_version": 1})
            self._audit(tx, actor, "review_opened", review_id, {"lot_id": lot_id, "evidence_ids": evidence_ids})
            affected = self._reevaluate_lot(tx, lot_id, actor, {"kind": "clean_case_rule", "review_id": None,
                                                                "evidence_ids": []})
            return 201, {"review_id": review_id, "row_version": 1, "lot_id": lot_id, "batteries": affected}

        payload = {"lot_id": lot_id, "reason": reason, "evidence_ids": evidence_ids, "subject": lot_id or ""}
        return self._execute(command_id, "open_review", payload, actor, action)

    def resolve_review(self, review_id, expected_row_version, reason, evidence_ids, actor, command_id):
        evidence_ids = sorted(set(as_list(evidence_ids)))
        reason = (reason or "").strip()

        def action(tx):
            review = tx.get("Reviews", review_id)
            if not review:
                raise CommandError(404, "review_not_found", f"No review {review_id}.")
            if review["status"] != "open":
                raise CommandError(409, "review_not_open", f"{review_id} is {review['status']}.")
            if review["row_version"] != expected_row_version:
                raise CommandError(409, "version_conflict", "The review changed since you read it. Reload and retry.",
                                   current_row_version=review["row_version"])
            if not 5 <= len(reason) <= 400:
                raise CommandError(422, "reason_required", "Give a reason of 5-400 characters.")
            self._evidence_for_lot(tx, evidence_ids, review["lot_id"])
            opening = set(domain.split_ids(review["opening_evidence"]))
            if not set(evidence_ids) - opening:
                raise CommandError(422, "new_evidence_required",
                                   "Resolving needs at least one evidence item that was not there when the review "
                                   "opened.")
            try:
                tx.update("Reviews", {
                    "review_id": review_id, "status": "resolved", "resolution_reason": reason,
                    "resolution_evidence": ",".join(evidence_ids), "resolved_by": actor,
                    "resolved_utc": self.clock()}, expected_row_version)
            except ConflictError as exc:
                raise CommandError(409, "version_conflict", str(exc)) from exc
            self._audit(tx, actor, "review_resolved", review_id,
                        {"lot_id": review["lot_id"], "evidence_ids": evidence_ids})
            decision = {"kind": "review_resolved", "review_id": review_id,
                        "evidence_ids": sorted(opening | set(evidence_ids))}
            affected = self._reevaluate_lot(tx, review["lot_id"], actor, decision)
            self._enqueue_lot_triggers(tx, review["lot_id"])
            return 200, {"review_id": review_id, "row_version": expected_row_version + 1, "batteries": affected}

        payload = {"review_id": review_id, "expected_row_version": expected_row_version, "reason": reason,
                   "evidence_ids": evidence_ids, "subject": review_id or ""}
        return self._execute(command_id, "resolve_review", payload, actor, action)

    # ---- outbox -------------------------------------------------------------------------------
    def process_outbox_once(self):
        item = self.store.claim_outbox()
        if not item:
            return None
        try:
            if item["kind"] == "agent_trigger":
                if self.workflow_dispatcher is None:
                    raise RuntimeError("Agent workflow dispatch is not configured.")
                outcome = self.workflow_dispatcher.dispatch(json.loads(item["payload"]))
            elif item["kind"] == "write_snapshot":
                outcome = self._write_snapshot(json.loads(item["payload"]))
            else:
                raise RuntimeError(f"Unknown outbox kind {item['kind']!r}.")
        except Exception as exc:  # noqa: BLE001 - every failure is recorded and retried
            self.store.finish_outbox(item["outbox_id"], error=f"{type(exc).__name__}: {exc}")
            return {"outbox_id": item["outbox_id"], "subject": item["subject"], "outcome": "retry_later",
                    "error": str(exc)}
        self.store.finish_outbox(item["outbox_id"])
        return {"outbox_id": item["outbox_id"], "subject": item["subject"], "outcome": outcome}

    def _write_snapshot(self, payload):
        battery_id, version = payload["battery_id"], payload["version"]
        data = payload["snapshot"].encode("utf-8")
        with self.store.read() as tx:
            recorded = tx.get("PassportVersions", battery_id, version)
            if tx.get("SnapshotReceipts", battery_id, version):
                return "already_written"
        if not recorded or domain.sha256(data) != recorded["snapshot_sha256"]:
            raise RuntimeError("outbox payload does not match the ledger hash")
        path = recorded["snapshot_path"]
        try:
            version_id = self.blobs.put("passports", path, data, "application/json", overwrite=False)
        except BlobExists:
            current = self.blobs.get("passports", path)
            if current is None or domain.sha256(current) != recorded["snapshot_sha256"]:
                raise RuntimeError(f"passports/{path} exists with different content") from None
            version_id = self.blobs.version_of("passports", path)
        stored = self.blobs.get("passports", path, version_id)
        if stored is None or domain.sha256(stored) != recorded["snapshot_sha256"]:
            raise RuntimeError("stored snapshot differs from the ledger hash")
        with self.store.transaction() as tx:
            if not tx.get("SnapshotReceipts", battery_id, version):
                tx.insert("SnapshotReceipts", {"battery_id": battery_id, "version": version,
                                               "blob_version_id": version_id,
                                               "verified_sha256": recorded["snapshot_sha256"],
                                               "written_utc": self.clock()})
                self._audit(tx, "outbox-worker", "snapshot_written", battery_id,
                            {"version": version, "blob_version_id": version_id})
        return "written"

    def drain_outbox(self, limit=50):
        results = []
        for _ in range(limit):
            result = self.process_outbox_once()
            if not result:
                break
            results.append(result)
        return results

    def enqueue_agent_trigger(self, tx, trigger, trigger_id=None):
        if not self.agent_mode:
            return None
        trigger = dict(trigger)
        trigger_id = trigger_id or f"wf-{domain.sha256(domain.canonical(trigger))[:56]}"
        trigger["trigger_id"] = trigger_id
        tx.insert("Outbox", {
            "kind": "agent_trigger", "subject": str(trigger.get("subject") or trigger["kind"]),
            "payload": json.dumps(trigger, sort_keys=True), "status": "pending", "attempts": 0,
            "created_utc": self.clock()})
        return trigger_id

    @staticmethod
    def _critical_lots_for_battery(tx, build_id, battery_id):
        from . import domain

        materials = {row["lot_id"]: row["material"] for row in tx.select("PackageLots", build_id=build_id)}
        return sorted({part["lot_id"] for part in tx.select("PackageComposition", build_id=build_id,
                                                           battery_id=battery_id)
                       if materials.get(part["lot_id"]) in domain.CRITICAL})

    def _enqueue_supplier_trigger(self, tx, lot_id, battery_id):
        evidence = sorted(tx.select("Evidence", lot_id=lot_id), key=lambda row: row["evidence_id"])
        # Due diligence starts with the supplier's claim; a transport record or adverse report that arrives
        # first is assessed together with it, never alone (which would open a review for the missing claim).
        if not any(row["kind"] in CLAIM_KINDS for row in evidence):
            return
        evidence_hash = domain.sha256(domain.canonical([{"evidence_id": row["evidence_id"], "sha256": row["sha256"]}
                                                        for row in evidence]))
        trigger = {"kind": "supplier_lot", "subject": lot_id, "lot_id": lot_id, "battery_id": battery_id}
        trigger_id = f"wf-{domain.sha256(domain.canonical({**trigger, 'evidence_hash': evidence_hash}))[:56]}"
        self.enqueue_agent_trigger(tx, trigger, trigger_id)

    def _enqueue_lot_triggers(self, tx, lot_id):
        if not self.agent_mode:
            return
        for state in tx.select("PassportState"):
            if lot_id in domain.split_ids(state["lot_ids"]):
                self._enqueue_supplier_trigger(tx, lot_id, state["battery_id"])

    def schedule_due_watch(self, now=None):
        """Persist one Watch trigger per ISO week; the SQL outbox survives app restarts."""
        if not self.agent_mode:
            return False
        moment = now or parse_utc(self.clock())
        iso = moment.isocalendar()
        trigger_id = f"watch-{iso.year}-w{iso.week:02d}"
        with self.store.transaction() as tx:
            if any(json.loads(row["payload"]).get("trigger_id") == trigger_id for row in
                   tx.select("Outbox", kind="agent_trigger")):
                return False
            self.enqueue_agent_trigger(tx, {"kind": "watch", "subject": "battery-due-diligence"}, trigger_id)
        return True

    # ---- reads --------------------------------------------------------------------------------
    def _views(self, tx, state):
        version = tx.get("PassportVersions", state["battery_id"], state["committed_version"]) \
            if state["committed_version"] else None
        receipt = tx.get("SnapshotReceipts", state["battery_id"], state["committed_version"]) if version else None
        return version, receipt

    def _links(self, tx, battery_id):
        return [{"battery_id": r["linked_id"], "link_type": r["link_type"]}
                for r in tx.select("PassportLinks", battery_id=battery_id)]

    def list_passports(self):
        with self.store.read() as tx:
            return [domain.public_view(s, *self._views(tx, s), self._links(tx, s["battery_id"]))
                    for s in tx.select("PassportState")]

    def public_passport(self, battery_id):
        with self.store.read() as tx:
            state = tx.get("PassportState", battery_id)
            return domain.public_view(state, *self._views(tx, state), self._links(tx, battery_id)) if state else None

    def restricted_passport(self, battery_id):
        with self.store.read() as tx:
            state = tx.get("PassportState", battery_id)
            if not state:
                return None
            version, receipt = self._views(tx, state)
            package_row = self.package_row(tx, state)
            lots = domain.split_ids(state["lot_ids"])
            reviews = [r for r in tx.select("Reviews") if r["lot_id"] in lots]
            inputs = self.inputs(tx, package_row)
            life = self.lifecycle_view(tx, battery_id)
            result = {"public": domain.public_view(state, version, receipt, self._links(tx, battery_id)),
                      "state": state,
                      "current_package_row": package_row, "batch": inputs["batch"],
                      "composition": inputs["composition"], "calculation": self.footprint(inputs),
                      "versions": tx.select("PassportVersions", battery_id=battery_id),
                      "receipts": tx.select("SnapshotReceipts", battery_id=battery_id), "reviews": reviews,
                      "lifecycle": {**life, "status_label": lifecycle_status_label(life["status"]),
                                    "latest_soh_pct": life["soh_readings"][-1]["soh_pct"]
                                    if life["soh_readings"] else None,
                                    "events": tx.select("LifecycleEvents", battery_id=battery_id),
                                    "requests": tx.select("SecondLifeRequests", battery_id=battery_id)}}
            return result

    @staticmethod
    def _claims(tx):
        claims = [r for r in tx.select("ConnectorMessages") if r["message_type"] == "supplier_claim"]
        return claims, {r["party_id"] for r in tx.select("Parties") if r["revoked"]}

    def report_rows(self):
        with self.store.read() as tx:
            claims, revoked = self._claims(tx)
            rows = []
            for state in tx.select("PassportState"):
                inputs = self.inputs(tx, self.package_row(tx, state))
                shares = domain.recycled_shares(inputs["composition"], inputs["lots"])
                approved = [r for r in tx.select("SecondLifeRequests", battery_id=state["battery_id"])
                            if r["status"] == "approved"]
                lots = {k: tx.get("PackageLots", state["build_id"], k) for k in domain.split_ids(state["lot_ids"])}
                lots = {k: v for k, v in lots.items() if v}
                statuses = {k: domain.claim_status(v, claims, revoked) for k, v in lots.items()}
                rows.append(domain.report_row(state, *self._views(tx, state), shares,
                                              self.successor(tx, state["battery_id"]),
                                              approved[-1]["illustrative_value_eur"] if approved else None,
                                              domain.footprint_verified(state, lots, statuses)))
            return rows

    def lot_claim_rows(self):
        """report.v_lot_claim_status: the latest delivered version of every lot."""
        with self.store.read() as tx:
            sequence = {p["build_id"]: p["build_sequence"] for p in tx.select("Packages")}
            latest = {}
            for lot in tx.select("PackageLots"):
                current = latest.get(lot["lot_id"])
                if current is None or sequence[lot["build_id"]] > sequence[current["build_id"]]:
                    latest[lot["lot_id"]] = lot
            claims, revoked = self._claims(tx)
            cases = {c["lot_id"]: c["status"] for c in tx.select("ComplianceCases")}
            return [domain.lot_claim_row(latest[k], domain.claim_status(latest[k], claims, revoked),
                                         cases.get(k, "none"))
                    for k in sorted(latest)]

    def report_export(self, now=None):
        """The v2 reporting export the operator copies to Fabric: passport rows and lot-claim rows, one export ID."""
        moment = parse_utc(now or self.clock())
        return {"schema_version": REPORT_SCHEMA_VERSION, "export_id": moment.strftime("EXP-%Y%m%dT%H%M%SZ"),
                "generated_utc": moment.strftime("%Y-%m-%dT%H:%M:%SZ"), "rows": self.report_rows(),
                "lot_claims": self.lot_claim_rows(), "data_label": "synthetic"}

    def board(self, battery_ids=None):
        """Coverage over produced batteries; successor passports from a second life are counted separately."""
        rows = [r for r in self.report_rows() if battery_ids is None or r["battery_id"] in battery_ids]
        with self.store.read() as tx:
            successors = {r["battery_id"] for r in tx.select("PassportLinks", link_type="predecessor")
                          if battery_ids is None or r["battery_id"] in battery_ids}
        produced = [r for r in rows if r["battery_id"] not in successors]
        committed = sum(1 for r in produced if r["committed_version"])
        text = f"{committed} of {len(produced)} passports committed"
        if successors:
            text += f"; {len(successors)} successor passport{'s' if len(successors) > 1 else ''}"
        return {"committed": committed, "total": len(produced), "successors": len(successors), "text": text}

    def list_reviews(self):
        with self.store.read() as tx:
            return tx.select("Reviews")

    def list_evidence(self):
        with self.store.read() as tx:
            return tx.select("Evidence")

    def audit(self, limit=50):
        with self.store.read() as tx:
            return tx.select("AuditEvents")[-limit:]

    def outbox(self):
        with self.store.read() as tx:
            return [{k: v for k, v in r.items() if k != "payload"} for r in tx.select("Outbox")]

    def evidence_content(self, evidence_id):
        with self.store.read() as tx:
            row = tx.get("Evidence", evidence_id)
        if not row:
            return None, None
        return row, self.blobs.get("evidence", row["blob_path"], row["blob_version_id"])

    def _compare(self, container, path, version_id, expected):
        current = self.blobs.get(container, path)
        recorded = self.blobs.get(container, path, version_id) if version_id else None
        return {
            "current_blob": "missing" if current is None else (
                "match" if domain.sha256(current) == expected else "mismatch"),
            "recorded_version": "unavailable" if recorded is None else (
                "match" if domain.sha256(recorded) == expected else "mismatch"),
        }

    def verify_evidence(self, evidence_id):
        with self.store.read() as tx:
            row = tx.get("Evidence", evidence_id)
        if not row:
            return None
        return {"evidence_id": evidence_id, "ledger_sha256": row["sha256"],
                **self._compare("evidence", row["blob_path"], row["blob_version_id"], row["sha256"])}

    def verify_snapshot(self, battery_id, version):
        with self.store.read() as tx:
            row = tx.get("PassportVersions", battery_id, version)
            receipt = tx.get("SnapshotReceipts", battery_id, version)
        if not row:
            return None
        return {"battery_id": battery_id, "version": version, "ledger_sha256": row["snapshot_sha256"],
                **self._compare("passports", row["snapshot_path"], receipt and receipt["blob_version_id"],
                                row["snapshot_sha256"])}

    def reconcile(self):
        """Compare Blob with the ledger: files without a ledger row, and ledger rows without a file."""
        with self.store.read() as tx:
            evidence = {r["blob_path"] for r in tx.select("Evidence")}
            snapshots = {r["snapshot_path"] for r in tx.select("PassportVersions")}
        found = {"evidence": set(self.blobs.list("evidence")), "passports": set(self.blobs.list("passports"))}
        return {
            "orphan_blobs": sorted(f"evidence/{p}" for p in found["evidence"] - evidence)
            + sorted(f"passports/{p}" for p in found["passports"] - snapshots),
            "missing_blobs": sorted(f"evidence/{p}" for p in evidence - found["evidence"])
            + sorted(f"passports/{p}" for p in snapshots - found["passports"]),
        }
