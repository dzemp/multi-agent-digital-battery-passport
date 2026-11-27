"""Business events for Application Insights, derived from authoritative state (the monitoring event contract).

Every hook runs after the database has committed, never emits for a rejected or replayed command, and swallows its
own errors: telemetry must never change or break a business operation. Field values are codes and identifiers only.
"""

import logging
import time

from . import domain, exec_views, telemetry
from .auth import ADMIN, AUTHORITY, COMPLIANCE, CONNECTOR, OEM, OPERATOR, RECYCLER, REVIEWER
from .prompt_shields import ACTOR as SHIELD_ACTOR
from .signed_messages import MESSAGE_TYPES

log = logging.getLogger("passport")
AGENT_ROUTE = "/agents/{role}/run"
SNAPSHOT_MEASURES = ("total_batteries", "published_valid", "holds_open", "oldest_hold_minutes", "outbox_pending",
                     "oldest_outbox_minutes", "stuck_batteries", "coverage_pct", "verified_footprint_batteries",
                     "supplier_completeness_pct", "second_life_count", "second_life_value_eur_illustrative",
                     "cases_open", "oldest_case_minutes")


def safe(method):
    def wrapper(self, *args, **kwargs):
        if not self.telemetry.enabled:
            return None
        try:
            return method(self, *args, **kwargs)
        except Exception as error:  # noqa: BLE001
            log.warning("business event hook %s skipped: %s", method.__name__, type(error).__name__)
            return None
    return wrapper


def action_of(decision):
    if isinstance(decision, dict):
        return decision.get("action")
    return getattr(decision, "action", None)


def requester_role(person):
    if person is None:
        return "anonymous"
    if person.has_any((REVIEWER,)):
        return "reviewer"
    if person.has_any((COMPLIANCE,)):
        return "compliance"
    if person.has_any((OPERATOR,)):
        return "operator"
    if person.has_any((CONNECTOR,)):
        return "connector"
    if any(role.startswith("Passport.Agent.") for role in person.roles):
        return "agent"
    for role, code in ((AUTHORITY, "authority"), (RECYCLER, "recycler"), (OEM, "oem"), (ADMIN, "admin")):
        if person.has_any((role,)):
            return code
    return "no_role"


class Events:
    def __init__(self, service, telemetry, prices=None, snapshot_seconds=300, monotonic=time.monotonic):
        self.service, self.telemetry, self.prices = service, telemetry, prices
        self.snapshot_seconds, self.monotonic = snapshot_seconds, monotonic
        self._last_snapshot = None

    def context(self, subject):
        """series_id and run_id (the package build) for a battery or a lot; battery_id only for a battery."""
        with self.service.store.read() as tx:
            state = tx.get("PassportState", subject)
            battery_id = subject if state else None
            if state is None:
                state = next((s for s in sorted(tx.select("PassportState"), key=lambda s: s["updated_utc"],
                                                reverse=True) if subject in domain.split_ids(s["lot_ids"])), None)
            if state is None:
                return {}
            package = tx.get("Packages", state["build_id"])
        return {"series_id": package["series_id"] if package else None, "run_id": state["build_id"],
                "battery_id": battery_id}

    @safe
    def agent_run(self, role, task, status, body, decision):
        ctx = self.context(task["subject"])
        execution = body.get("execution") or {}
        code = exec_views.outcome_code(status, body)
        tokens_in, tokens_out = execution.get("input_tokens"), execution.get("output_tokens")
        cost = exec_views.price_of(self.prices, execution.get("model"), tokens_in, tokens_out)
        usd = cost if cost is not None and (self.prices or {}).get("currency") == "USD" else None
        source = (self.prices or {}).get("source")
        price_version = ((source if source and telemetry.VALUE.fullmatch(source) else "custom")
                         if usd is not None else None)
        self.telemetry.emit("agent.run", role=role, model=execution.get("model"), result=code,
                            price_version=price_version,
                            input_tokens=tokens_in, output_tokens=tokens_out, latency_ms=execution.get("latency_ms"),
                            estimated_cost_usd=usd, **ctx)
        common = {"proposal_action": action_of(decision), "actor_role": f"agent:{role}",
                  "route_template": AGENT_ROUTE, **ctx}
        if status < 400:
            self.telemetry.emit("decision.recorded", applied_action=code, reason_code="accepted", **common)
        else:
            self.telemetry.emit("gate.failed", applied_action="refused", reason_code=code, **common)
            if role == "supplier" and code == "prompt_attack_detected":
                with self.service.store.read() as tx:
                    opened = [r for r in tx.select("Reviews", lot_id=task["subject"], status="open",
                                                   opened_by=SHIELD_ACTOR) if r["opened_utc"] >= task["created_utc"]]
                for review in opened:
                    self.telemetry.emit("hold.opened", hold_id=review["review_id"], reason_code=code,
                                        actor_role=SHIELD_ACTOR, age_minutes=0, **ctx)
            return
        outcome = body.get("outcome") or {}
        if role == "issuance" and outcome.get("committed_now"):
            self.telemetry.emit("passport.published", version=str(outcome["committed_version"]), **ctx)
        if role == "supplier" and code == "review_requested":
            with self.service.store.read() as tx:
                opened = [r for r in tx.select("Reviews", lot_id=task["subject"], status="open")
                          if r["opened_by"] == "agent:supplier" and r["opened_utc"] >= task["created_utc"]]
            for review in opened:
                self.telemetry.emit("hold.opened", hold_id=review["review_id"], reason_code="supplier_review_requested",
                                    actor_role="agent:supplier", age_minutes=0, **ctx)
        if role == "supplier" and code == "compliance_escalated":
            self._case_event(outcome.get("case") or {}, "agent:supplier", ctx)
        if role == "lifecycle" and outcome.get("status") in ("recorded", "quarantined", "already_recorded"):
            self._lifecycle_event(outcome, ctx)

    def _lifecycle_event(self, outcome, ctx):
        self.telemetry.emit("lifecycle.event", event_type=outcome.get("event_type") or "unknown",
                            result=outcome["status"], reason_code=outcome.get("reason_code") or "unknown", **ctx)
        if outcome["status"] == "recorded" and outcome.get("closed"):
            self.telemetry.emit("passport.closed", state=outcome["state_after"], **ctx)

    @safe
    def second_life_decided(self, status, body):
        if status != 200 or body.get("replayed"):
            return
        ctx = self.context(body["battery_id"])
        self.telemetry.emit("second_life.decided", request_id=body["request_id"], decision=body["decision"],
                            actor_role="reviewer", **ctx)
        if body["decision"] == "approve":
            self.telemetry.emit("passport.closed", state=body["lifecycle_status"], **ctx)
            self.telemetry.emit("passport.successor.created", successor_id=body["successor_id"],
                                **self.context(body["successor_id"]))

    def _case_event(self, case, actor_role, ctx):
        if case.get("outcome") == "opened":
            self.telemetry.emit("compliance.case.opened", case_id=case["case_id"], reason_code=case["reason_code"],
                                actor_role=actor_role, **ctx)
        elif case.get("outcome") == "reopened":
            self.telemetry.emit("compliance.case.reopened", case_id=case["case_id"],
                                reason_code=case["reason_code"], **ctx)

    @safe
    def case_opened(self, status, body):
        if status != 201 or body.get("replayed"):
            return
        self._case_event(body, "compliance", self.context(body["lot_id"]))

    @safe
    def case_decided(self, status, body):
        if status != 200 or body.get("replayed"):
            return
        ctx = self.context(body["lot_id"])
        self.telemetry.emit("compliance.case.decided", case_id=body["case_id"], decision=body["decision"],
                            actor_role="compliance", age_minutes=body.get("age_minutes"), **ctx)
        for battery in body.get("batteries") or []:
            if isinstance(battery, dict) and battery.get("committed_now"):
                self.telemetry.emit("passport.published", version=str(battery["committed_version"]),
                                    **self.context(battery["battery_id"]))

    @safe
    def evidence_recorded(self, status, body):
        """New adverse evidence may reopen an accepted case."""
        if status != 201 or body.get("replayed") or not body.get("reopened_case"):
            return
        self._case_event(body["reopened_case"], "compliance", self.context(body["lot_id"]))

    @safe
    def hold_opened(self, status, body):
        if status != 201 or body.get("replayed"):
            return
        self.telemetry.emit("hold.opened", hold_id=body["review_id"], reason_code="reviewer_opened",
                            actor_role="reviewer", age_minutes=0, **self.context(body["lot_id"]))

    @safe
    def hold_resolved(self, review_id, status, body):
        if status != 200 or body.get("replayed"):
            return
        with self.service.store.read() as tx:
            review = tx.get("Reviews", review_id)
        ctx = self.context(review["lot_id"])
        age = exec_views.minutes_between(review["opened_utc"], review["resolved_utc"])
        self.telemetry.emit("hold.resolved", hold_id=review_id, reason_code="resolved_with_new_evidence",
                            actor_role="reviewer", age_minutes=age, **ctx)
        self.telemetry.emit("hold.snapshot", hold_id=review_id, state="resolved", reason_code="sourcing_review",
                            actor_role=exec_views.actor_role(review["opened_by"]), age_minutes=age, **ctx)
        for battery in body.get("batteries") or []:
            if isinstance(battery, dict) and battery.get("committed_now"):
                self.telemetry.emit("passport.published", version=str(battery["committed_version"]),
                                    **self.context(battery["battery_id"]))

    @safe
    def connector_message(self, status, body):
        """One event per message the mock connector received or refused (not for replays); codes only."""
        if body.get("replayed"):
            return
        kind = body.get("message_type")
        result = body.get("result") or ("rejected" if status >= 400 else "unknown")
        reason = body.get("reason_code") or body.get("error") or "unknown"
        ctx = self.context(body["subject"]) if body.get("subject") else {}
        self.telemetry.emit("connector.message", message_type=kind if kind in MESSAGE_TYPES else "unknown",
                            result=result, reason_code=reason, **ctx)
        if result == "quarantined":
            self._lifecycle_event({"status": "quarantined", "event_type": kind, "reason_code": reason}, ctx)

    @safe
    def access_denied(self, request, person):
        route = request.scope.get("route")
        self.telemetry.emit("access.denied", route_template=getattr(route, "path", None) or "unmatched",
                            actor_role=requester_role(person),
                            reason_code="not_signed_in" if person is None else "role_missing")

    @safe
    def agent_denied(self, request, person, code):
        route = request.scope.get("route")
        self.telemetry.emit("access.denied", route_template=getattr(route, "path", None) or "unmatched",
                            actor_role=requester_role(person), reason_code=code)

    @safe
    def view_served(self, view):
        """Which field set a role group received: codes only, never who asked."""
        self.telemetry.emit("view.served", field_set=view["field_set"], role_group=view["role_group"],
                            **self.context(view["battery_id"]))

    @safe
    def ledger_checked(self, result, battery_id=None):
        if not result or not result.get("performed"):
            return  # a cached or impossible check is not a ledger verification
        outcome = "success" if result["status"] in ("verified", "chain_verified_anchor_pending") else "failure"
        ctx = self.context(battery_id) if battery_id else {}
        self.telemetry.emit("ledger.verified", result=outcome, reason_code=result.get("reason_code") or "unknown",
                            **ctx)

    @safe
    def outbox_cycle(self, results):
        for item in results or []:
            if item.get("outcome") != "retry_later":
                continue
            error = item.get("error") or ""
            reason = ("ledger_hash_mismatch" if "ledger hash" in error else
                      "blob_conflict" if "different content" in error else "snapshot_write_failed")
            with self.service.store.read() as tx:
                row = tx.get("Outbox", item["outbox_id"])
            battery_id = (item.get("subject") or "").split("/v")[0]
            self.telemetry.emit("outbox.retry", reason_code=reason, attempt=row["attempts"] if row else None,
                                **self.context(battery_id))

    @safe
    def snapshots(self, force=False):
        """Periodic complete state from the database, so dashboards do not depend on historical events."""
        now = self.monotonic()
        if not force and self._last_snapshot is not None and now - self._last_snapshot < self.snapshot_seconds:
            return
        self._last_snapshot = now
        with self.service.store.read() as tx:
            ordered, _ = exec_views.all_series(tx)
        for series_id in ordered[:5]:
            snap = exec_views.series_snapshot(self.service, series_id)
            if not snap:
                continue
            self.telemetry.emit("passport.series.snapshot", series_id=series_id,
                                **{k: snap[k] for k in SNAPSHOT_MEASURES})
            for plant in snap["plants"]:
                self.telemetry.emit("passport.plant.snapshot", series_id=series_id, plant_id=plant["plant_id"],
                                    produced=plant["produced"], published=plant["published"],
                                    coverage_pct=plant["coverage_pct"],
                                    verified_footprint_batteries=plant["verified_footprint_batteries"],
                                    avg_footprint_per_kwh=plant["avg_footprint_per_kwh"])
            for hold in snap["holds"]:
                self.telemetry.emit("hold.snapshot", hold_id=hold["review_id"], state="open",
                                    reason_code="sourcing_review", actor_role=hold["actor_role"],
                                    age_minutes=hold["age_minutes"], series_id=series_id)
