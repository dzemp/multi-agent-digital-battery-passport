"""Minimized business events for Application Insights (the monitoring event contract).

A no-op unless APPLICATIONINSIGHTS_CONNECTION_STRING is set. The Microsoft OpenTelemetry distro is an app
dependency. Only allowlisted event names and fields are sent: no evidence content, free-text reason,
prompt, user name, e-mail, token or secret. Strings land in customDimensions, numbers in customMeasurements.
"""

import json
import logging
import math
import os
import re
import uuid

log = logging.getLogger("passport")
EVENTS_LOGGER = "dbp.events"
SCHEMA_VERSION = "2"
COMMON = ("series_id", "run_id", "battery_id", "command_id")
# event name -> (string dimensions, numeric measurements) beyond the common fields
EVENTS = {
    "passport.series.snapshot": ((), ("total_batteries", "published_valid", "holds_open", "oldest_hold_minutes",
                                      "outbox_pending", "oldest_outbox_minutes", "stuck_batteries", "coverage_pct",
                                      "verified_footprint_batteries", "supplier_completeness_pct",
                                      "second_life_count", "second_life_value_eur_illustrative", "cases_open",
                                      "oldest_case_minutes")),
    "passport.plant.snapshot": (("plant_id",), ("produced", "published", "coverage_pct",
                                                "verified_footprint_batteries", "avg_footprint_per_kwh")),
    "view.served": (("field_set", "role_group"), ()),
    "passport.published": (("version",), ()),
    "hold.snapshot": (("hold_id", "state", "reason_code", "actor_role"), ("age_minutes",)),
    "hold.opened": (("hold_id", "reason_code", "actor_role"), ("age_minutes",)),
    "hold.resolved": (("hold_id", "reason_code", "actor_role"), ("age_minutes",)),
    "decision.recorded": (("proposal_action", "applied_action", "reason_code", "actor_role", "route_template"), ()),
    "gate.failed": (("proposal_action", "applied_action", "reason_code", "actor_role", "route_template"), ()),
    "agent.run": (("role", "model", "result", "price_version"),
                  ("input_tokens", "output_tokens", "latency_ms", "estimated_cost_usd")),
    "outbox.retry": (("reason_code",), ("attempt",)),
    "ledger.verified": (("result", "reason_code"), ()),
    "access.denied": (("route_template", "actor_role", "reason_code"), ()),
    "watch.recommendation": (("recommendation_id", "state", "reason_code"), ()),
    "connector.message": (("message_type", "result", "reason_code"), ()),
    "compliance.case.opened": (("case_id", "reason_code", "actor_role"), ()),
    "compliance.case.decided": (("case_id", "decision", "actor_role"), ("age_minutes",)),
    "compliance.case.reopened": (("case_id", "reason_code"), ()),
    "lifecycle.event": (("event_type", "result", "reason_code"), ()),
    "second_life.decided": (("request_id", "decision", "actor_role"), ()),
    "passport.closed": (("state",), ()),
    "passport.successor.created": (("successor_id",), ()),
}
CODE = re.compile(r"^[a-z0-9_.:-]{1,80}$")  # reason codes, states, roles and actions: never free text
VALUE = re.compile(r"^[A-Za-z0-9 _.:/{}()+-]{1,100}$")  # identifiers, model names and route templates
CODED = {"reason_code", "state", "result", "actor_role", "proposal_action", "applied_action", "role", "message_type",
         "decision", "event_type", "field_set", "role_group"}


def build(name, **fields):
    """Validate one event against the allowlist and split it into dimensions and measurements."""
    if name not in EVENTS:
        raise ValueError(f"Event {name!r} is not in the event contract.")
    strings, numbers = EVENTS[name]
    unknown = set(fields) - set(COMMON) - set(strings) - set(numbers)
    if unknown:
        raise ValueError(f"Fields {sorted(unknown)} are not allowlisted for {name}.")
    dims = {"dbp.schema_version": SCHEMA_VERSION, "event_id": str(uuid.uuid4())}
    measures = {}
    for key, value in fields.items():
        if value is None:
            continue  # unknown stays missing; never emitted as zero
        if key in numbers:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name}.{key} must be a finite number.")
            measures[key] = value
            continue
        text = str(value)
        if not (CODE if key in CODED else VALUE).fullmatch(text):
            raise ValueError(f"{name}.{key} is not an allowlisted code or identifier.")
        dims[key] = text
    return dims, measures


class Telemetry:
    """Sends allowlisted events to a sink. Without a sink, every call returns immediately."""

    def __init__(self, sink=None):
        self.sink = sink

    @property
    def enabled(self):
        return self.sink is not None

    def emit(self, name, **fields):
        if self.sink is None:
            return None
        try:
            dims, measures = build(name, **fields)
            self.sink(name, dims, measures)
            return dims["event_id"]
        except Exception as error:  # noqa: BLE001 - telemetry must never break a business operation
            log.warning("telemetry event %s dropped: %s", name, type(error).__name__)
            return None


def log_sink(name, dims, measures):
    logging.getLogger(EVENTS_LOGGER).info(name, extra={
        "microsoft.custom_event.name": name, **dims, "microsoft.custom_measurements": json.dumps(measures)})


def from_env(env=None):
    """Connect Azure Monitor once, only when a connection string is configured."""
    env = os.environ if env is None else env
    if not env.get("APPLICATIONINSIGHTS_CONNECTION_STRING"):
        return Telemetry()
    os.environ.setdefault("OTEL_SERVICE_NAME", "passport-app")
    from microsoft.opentelemetry import use_microsoft_opentelemetry

    use_microsoft_opentelemetry(
        enable_azure_monitor=True,
        azure_monitor_connection_string=env["APPLICATIONINSIGHTS_CONNECTION_STRING"],
        azure_monitor_enable_live_metrics=False,
        enable_trace_based_sampling_for_logs=False,
        enable_sensitive_data=False,
        logger_name=EVENTS_LOGGER,
    )
    logging.getLogger(EVENTS_LOGGER).setLevel(logging.INFO)
    return Telemetry(log_sink)
