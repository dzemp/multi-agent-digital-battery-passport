"""Storage contract shared by the in-memory store (tests, simulation) and the Azure SQL ledger store.

Every table has a mode:
  append    - insert only (an append-only ledger table in SQL)
  updatable - insert and update with an expected row_version (an updatable ledger table in SQL)
  ordinary  - working tables that are not evidence (commands, the outbox and the schema version)
"""

import copy
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

S, N, F, T = "s", "i", "f", "t"  # string, integer, decimal, UTC time

TABLES = {
    "AftersalesReceipts": ("append", ["delivery_id"], {
        "delivery_id": S, "series_id": S, "as_of_utc": T, "sha256": S, "imported_utc": T, "imported_by": S}),
    "OEMPhysicalPacks": ("append", ["battery_id"], {
        "battery_id": S, "series_id": S, "oem_id": S, "batch_id": S}),
    "WarrantyTerms": ("append", ["delivery_id", "battery_id"], {
        "delivery_id": S, "battery_id": S, "in_service_date": S, "term_version": S,
        "warranty_months": N, "warranty_cycles": N}),
    "ServiceReadings": ("append", ["reading_id"], {
        "reading_id": S, "battery_id": S, "delivery_id": S, "measured_utc": T,
        "actual_cycles": F, "cycle_basis": S, "soh_pct": F}),
    "ServiceCampaigns": ("append", ["delivery_id", "campaign_id"], {
        "delivery_id": S, "campaign_id": S, "kind": S, "status": S, "effective_date": S}),
    "CampaignPacks": ("append", ["delivery_id", "battery_id", "campaign_id"], {
        "delivery_id": S, "battery_id": S, "campaign_id": S}),
    "AgentTasks": ("append", ["task_id"], {
        "task_id": S, "role": S, "subject": S, "context_hash": S, "context_json": S,
        "request_json": S, "actor": S, "created_utc": T}),
    "AgentResults": ("append", ["task_id"], {
        "task_id": S, "status_code": N, "result_json": S, "recorded_utc": T}),
    "AgentFacts": ("append", ["fact_id"], {
        "fact_id": S, "kind": S, "subject": S, "input_hash": S, "data_json": S, "recorded_utc": T}),
    "AiContentProvenance": ("append", ["provenance_id"], {
        "provenance_id": S, "task_id": S, "role": S, "field": S, "stored_in": S, "ai_generated": N,
        "digital_source_type": S, "generator": S, "agent_name": S, "agent_version": S, "model": S, "model_version": S,
        "hosting": S, "text_sha256": S, "text_chars": N, "generated_utc": T, "human_modified": N}),
    "PolicyCandidates": ("updatable", ["candidate_id"], {
        "candidate_id": S, "task_id": S, "title": S, "policy_text": S, "source_ids": S, "status": S,
        "recommended_by": S, "requested_by": S, "model": S, "model_version": S, "provenance_id": S,
        "recommended_utc": T, "decided_by": S, "decided_utc": T, "decision_reason": S, "row_version": N}),
    "PolicyState": ("updatable", ["policy_slot"], {
        "policy_slot": S, "version": N, "active_candidate_id": S, "title": S, "policy_text": S,
        "ai_drafted": N, "source_task_id": S, "provenance_id": S,
        "activated_by": S, "activated_utc": T, "row_version": N}),
    "Commands": ("ordinary", ["command_id"], {
        "command_id": S, "command_type": S, "payload_sha256": S, "actor": S,
        "status_code": N, "result_json": S, "recorded_utc": T}),
    "SchemaInfo": ("ordinary", ["component"], {
        "component": S, "schema_version": N, "applied_utc": T}),
    "Packages": ("append", ["build_id"], {
        "build_id": S, "build_sequence": N, "manifest_sha256": S, "series_id": S, "battery_count": N,
        "rules_version": S, "method_version": S, "factor_set_version": S, "intake_folder": S, "imported_by": S,
        "imported_utc": T}),
    "Batches": ("append", ["build_id", "batch_id"], {
        "build_id": S, "batch_id": S, "plant_id": S, "version": N, "method_version": S, "factor_set_version": S,
        "allocation_rule": S, "energy_id": S, "energy_version": N, "batch_kwh": F, "battery_count": N,
        "grid_factor_id": S}),
    "PackageLots": ("append", ["build_id", "lot_id"], {
        "build_id": S, "lot_id": S, "version": N, "supplier_id": S, "material": S, "region": S, "origin_site": S,
        "mass_kg": F, "recycled_share_pct": F, "pcf_kgco2e_per_kg": F, "claim_id": S, "claim_sha256": S,
        "required_fields": S, "present_fields": S, "na_reasons": S}),
    "PackageComposition": ("append", ["build_id", "battery_id", "lot_id"], {
        "build_id": S, "battery_id": S, "material": S, "lot_id": S, "mass_kg": F}),
    "PackageBatteries": ("append", ["build_id", "battery_id"], {
        "build_id": S, "battery_id": S, "series_id": S, "model_id": S, "chemistry": S, "rated_kwh": F,
        "rated_capacity_ah": F, "expected_cycles": N, "plant_id": S, "batch_id": S, "readiness": S,
        "input_hash": S, "allocated_kwh": F, "grid_factor_id": S, "lot_ids": S, "reasons": S,
        "lifecycle_status": S, "holder_id": S, "footprint_per_kwh": F, "footprint_verified": N}),
    "PassportState": ("updatable", ["battery_id"], {
        "battery_id": S, "model_id": S, "build_id": S, "input_hash": S, "readiness": S, "eligibility": S,
        "hold_reason": S, "lot_ids": S, "chemistry": S, "rated_kwh": F, "plant_id": S, "batch_id": S,
        "lifecycle_status": S, "holder_id": S, "footprint_per_kwh": F, "footprint_verified": N,
        "committed_version": N, "committed_input_hash": S, "row_version": N, "updated_utc": T}),
    "PassportVersions": ("append", ["battery_id", "version"], {
        "battery_id": S, "version": N, "build_id": S, "input_hash": S, "snapshot_sha256": S,
        "snapshot_path": S, "decision": S, "review_id": S, "committed_by": S, "committed_utc": T}),
    "SnapshotReceipts": ("append", ["battery_id", "version"], {
        "battery_id": S, "version": N, "blob_version_id": S, "verified_sha256": S, "written_utc": T}),
    "Evidence": ("append", ["evidence_id"], {
        "evidence_id": S, "kind": S, "lot_id": S, "file_name": S, "content_type": S, "sha256": S,
        "size_bytes": N, "blob_path": S, "blob_version_id": S, "uploaded_by": S, "uploaded_utc": T}),
    "Reviews": ("updatable", ["review_id"], {
        "review_id": S, "lot_id": S, "status": S, "reason": S, "opening_evidence": S,
        "resolution_reason": S, "resolution_evidence": S, "opened_by": S, "opened_utc": T,
        "resolved_by": S, "resolved_utc": T, "reason_origin": S, "provenance_id": S, "row_version": N}),
    "ComplianceCases": ("updatable", ["case_id"], {
        "case_id": S, "lot_id": S, "status": S, "reason_code": S, "opening_evidence": S, "agent_task_id": S,
        "opened_by": S, "opened_utc": T, "decision": S, "action_owner": S, "due_date": S, "decided_by": S,
        "decided_utc": T, "reopen_count": N, "reopened_utc": T, "row_version": N}),
    "ComplianceDecisions": ("append", ["decision_id"], {
        "decision_id": S, "case_id": S, "lot_id": S, "decision": S, "action_owner": S, "due_date": S,
        "evidence_ids": S, "reason": S, "case_row_version": N, "decided_by": S, "decided_utc": T}),
    "AuditEvents": ("append", ["event_id"], {
        "event_id": N, "event_utc": T, "actor": S, "action": S, "subject": S, "detail": S}),
    "Parties": ("updatable", ["party_id"], {
        "party_id": S, "name": S, "role": S, "key_id": S, "public_key": S, "lot_ids": S, "event_types": S,
        "revoked": N, "registry_sha256": S, "updated_utc": T, "row_version": N}),
    "ConnectorAgreements": ("append", ["agreement_id"], {
        "agreement_id": S, "sender": S, "receiver": S, "purpose": S, "message_types": S, "valid_from": T,
        "valid_until": T, "recorded_by": S, "recorded_utc": T}),
    "ConnectorMessages": ("append", ["receipt_id"], {
        "receipt_id": S, "message_id": S, "sender": S, "message_type": S, "subject": S, "correlation_id": S,
        "payload_sha256": S, "payload_json": S, "agreement_id": S, "result": S, "reason_code": S, "evidence_id": S,
        "received_by": S, "received_utc": T}),
    "LifecycleEvents": ("append", ["lifecycle_event_id"], {
        "lifecycle_event_id": S, "message_id": S, "battery_id": S, "event_type": S, "issuer": S, "event_utc": T,
        "state_before": S, "state_after": S, "result": S, "reason_code": S, "passport_version": N,
        "payload_sha256": S, "details_json": S, "agent_task_id": S, "recorded_by": S, "recorded_utc": T}),
    "SecondLifeRequests": ("updatable", ["request_id"], {
        "request_id": S, "battery_id": S, "successor_id": S, "requested_by": S, "message_id": S,
        "safety_assessment_ref": S, "intended_use": S, "soh_pct": F, "illustrative_value_eur": F, "value_label": S,
        "status": S, "decision_reason": S, "decided_by": S, "decided_utc": T, "requested_utc": T, "row_version": N}),
    "PassportLinks": ("append", ["battery_id", "linked_id"], {
        "battery_id": S, "linked_id": S, "link_type": S, "created_by": S, "created_utc": T}),
    "Outbox": ("ordinary", ["outbox_id"], {
        "outbox_id": N, "kind": S, "subject": S, "payload": S, "status": S, "attempts": N,
        "lease_until": T, "last_error": S, "created_utc": T, "done_utc": T}),
}
IDENTITY = {"AuditEvents": "event_id", "Outbox": "outbox_id"}
MAX_ATTEMPTS = 5
SCHEMA_VERSION = 4
GRANTS = {"append": "SELECT, INSERT", "updatable": "SELECT, INSERT, UPDATE", "ordinary": "SELECT, INSERT, UPDATE"}


def grant_statements(identity):
    """Per-table grants from the table manifest: no DELETE, no DDL and nothing on schema report."""
    return [f"GRANT {GRANTS[mode]} ON OBJECT::dbp.{name} TO [{identity}];"
            for name, (mode, _keys, _columns) in sorted(TABLES.items())]


class StoreError(Exception):
    pass


class ConflictError(StoreError):
    """The row changed since it was read (expected row_version did not match) or the key exists."""


def key_of(table, row):
    return tuple(row[k] for k in TABLES[table][1])


def check_key(table, key):
    expected = len(TABLES[table][1])
    if len(key) != expected:
        raise StoreError(f"{table}: expected {expected} key values, got {len(key)}")


def check_row(table, row):
    columns = TABLES[table][2]
    unknown = set(row) - set(columns)
    if unknown:
        raise StoreError(f"{table}: unknown columns {sorted(unknown)}")


class MemoryTx:
    def __init__(self, tables, counters):
        self.tables, self.counters = tables, counters

    def insert(self, table, row):
        check_row(table, row)
        row = dict(row)
        if table in IDENTITY:
            self.counters[table] = self.counters.get(table, 0) + 1
            row[IDENTITY[table]] = self.counters[table]
        full = {c: row.get(c) for c in TABLES[table][2]}
        key = key_of(table, full)
        if key in self.tables[table]:
            raise ConflictError(f"{table}: key {key} exists")
        self.tables[table][key] = full

    def get(self, table, *key):
        check_key(table, key)
        row = self.tables[table].get(tuple(key))
        return dict(row) if row else None

    def select(self, table, **where):
        rows = [dict(r) for r in self.tables[table].values() if all(r.get(k) == v for k, v in where.items())]
        return sorted(rows, key=lambda r: key_of(table, r))

    def update(self, table, row, expected_row_version):
        mode = TABLES[table][0]
        if mode != "updatable":
            raise StoreError(f"{table} is {mode}; updates are not allowed")
        check_row(table, row)
        key = key_of(table, row)
        current = self.tables[table].get(key)
        if not current or current["row_version"] != expected_row_version:
            raise ConflictError(f"{table} {key}: expected row_version {expected_row_version}")
        self.tables[table][key] = {**current, **row, "row_version": expected_row_version + 1}


class MemoryStore:
    """Holds all tables in memory. A transaction works on a copy and replaces the tables on commit."""

    kind = "memory"

    def __init__(self, schema_version=SCHEMA_VERSION):
        self._tables = {name: {} for name in TABLES}
        self._tables["SchemaInfo"][("dbp",)] = {"component": "dbp", "schema_version": schema_version,
                                                 "applied_utc": format_utc(datetime.now(timezone.utc))}
        self._tables["PolicyState"][("active",)] = {
            "policy_slot": "active", "version": 2, "active_candidate_id": None, "title": "Baseline policy",
            "policy_text": "Operational gates remain code-owned. Watch recommendations are advisory until "
                           "approved by the designated Policy Approver.",
            "ai_drafted": 0, "source_task_id": None, "provenance_id": None,
            "activated_by": "system", "activated_utc": format_utc(datetime.now(timezone.utc)), "row_version": 1}
        self._counters = {}
        self._lock = threading.RLock()

    @contextmanager
    def transaction(self):
        with self._lock:
            tables, counters = copy.deepcopy(self._tables), dict(self._counters)
            yield MemoryTx(tables, counters)
            self._tables, self._counters = tables, counters

    @contextmanager
    def read(self):
        """Read-only view. Changes made through it are thrown away."""
        with self._lock:
            yield MemoryTx(copy.deepcopy(self._tables), dict(self._counters))

    def ping(self):
        return True

    def schema_version(self):
        row = self._tables["SchemaInfo"].get(("dbp",))
        return row["schema_version"] if row else None

    def claim_outbox(self, lease_seconds=60):
        now = datetime.now(timezone.utc)
        with self._lock:
            for _key, item in sorted(self._tables["Outbox"].items()):
                lease = item["lease_until"]
                expired = item["status"] == "processing" and lease and parse_utc(lease) < now
                if item["status"] == "pending" or expired:
                    item.update(status="processing", attempts=item["attempts"] + 1,
                                lease_until=format_utc(now + timedelta(seconds=lease_seconds)))
                    return dict(item)
        return None

    def finish_outbox(self, outbox_id, error=None):
        with self._lock:
            item = self._tables["Outbox"][(outbox_id,)]
            if error is None:
                item.update(status="done", done_utc=format_utc(datetime.now(timezone.utc)), last_error=None)
            else:
                item.update(status="failed" if item["attempts"] >= MAX_ATTEMPTS else "pending",
                            last_error=str(error)[:1000], lease_until=None)


def format_utc(dt):
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_utc(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))
