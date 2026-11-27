"""Run every act in memory, with a scripted double in place of the agents.

This is local evidence only: no Azure, no model and no AI. It proves that the runner, the gates and the human
hand-off behave as described, so a run against the deployed app fails only for deployment reasons. The series'
package and evidence come from the same synthetic data generator as the deployment.
"""

import itertools
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from data import simulator
from data.aftersales_flow import after_bytes
from data.package import read_folder
from dbp_mvp import aftersales
from passport.agent_contracts import EVENT_FROM, SECTIONS
from passport.auth import LOCAL_ROLES_HEADER, LOCAL_USER_HEADER
from passport.blobs import LocalBlobs
from passport.config import Settings
from passport.service import PassportService
from passport.store import MemoryStore, format_utc
from passport.web import create_app

from .acts import OUTPUT, PASS, Register, Runner, Stop, STOPPED, body_of, call, problem
from .series import Series

STAFF = "Passport.Operator,Passport.Reviewer,Passport.Connector,Passport.Compliance"
ACTS = (("prepare", {}), ("review", {"yes": True}), ("complete", {}), ("aftercare", {}))
INTAKE = "intake"
LOCAL_WORKBOOK = ("executive workbook: an Azure-only view of the app's Application Insights telemetry; not available "
                  "in a local run.")


class ScriptedDouble:
    """A fixed, content-based rule per role. It reads every permitted section like a grounded agent, but it is not
    AI, and every register says so."""

    METADATA = {"hosting": "local_scripted_double", "agent_name": "scripted-double", "agent_version": "1",
                "model": "none (not AI)", "model_version": None, "contract": None, "evidence_level": "local",
                "input_tokens": None, "output_tokens": None, "latency_ms": None}

    def __init__(self, defer_roles=(), agree_all=False):
        self.defer_roles, self.agree_all = set(defer_roles), agree_all
        self.foreign_citation = self.force_action = None

    def run(self, role, task_id, context, reader, *, attempt_id=None):
        sections = {name: reader(task_id, role, name) for name in SECTIONS[role]}
        ids = []
        if role in self.defer_roles:
            action, reason = "defer", "Scripted double was told to defer this role."
        elif role == "supplier":
            action, reason, ids = self.supplier(sections["evidence"])
        elif role == "carbon":
            ready = sections["calculator"]["available"]
            action, reason = ("calculate", "Factory inputs are complete; request the illustrative calculation.") \
                if ready else ("defer", "Factory inputs are incomplete; a calculation would invent data.")
        elif role == "issuance":
            gates = sections["gates"]
            action, reason = ("publish", "All publication gates pass.") if gates["ready"] or self.agree_all else \
                ("defer", f"Gates fail: {'; '.join(gates['reasons'])}"[:400])
        elif role == "lifecycle":
            action, reason = self.lifecycle(sections["state"], sections["event"])
            event_id = sections["event"].get("event_id")
            ids = [event_id] if event_id else []
        else:
            action, reason = "recommend", "Scripted double cites both pinned sources."
            ids = [s["id"] for s in sections["sources"]]
        if role == "supplier" and self.foreign_citation:
            ids = ids + list(self.foreign_citation)
        if role == "supplier" and self.force_action:
            action, reason = self.force_action, "Scripted double was told to propose this action."
        decision = {"action": action, "reason": reason, "evidence_ids": ids[:20]}
        if role == "watch" and action == "recommend":
            decision.update(policy_title="Review sourcing guidance against pinned sources",
                            policy_text="Keep adverse sourcing findings under human Compliance review. "
                                        "Confirm the applicable dates and scope before changing guidance. "
                                        "This recommendation does not change application gates.")
        return decision, dict(self.METADATA)

    @staticmethod
    def lifecycle(state, event):
        """Propose the routed event's action only where the state machine and the repurpose prerequisites allow it;
        text inside the event is data, so a request to approve a second life is never an action."""
        kind = event.get("event_type") if isinstance(event, dict) else None
        details = event.get("details") if isinstance(event, dict) else None
        if kind not in EVENT_FROM or not isinstance(details, dict):
            return "defer", "The routed event is incomplete or names no lifecycle action this agent may propose."
        status = state.get("lifecycle_status")
        if status != EVENT_FROM[kind]:
            return "defer", f"{kind} needs a {EVENT_FROM[kind]} passport; this one is {status}."
        if kind == "request_repurpose":
            reference = details.get("safety_assessment_ref")
            if not isinstance(reference, str) or not reference.strip():
                return "defer", "The repurpose request has no safety assessment reference."
            if state.get("latest_soh_pct") is None:
                return "defer", "No state-of-health reading is recorded for this battery."
        return kind, f"Record the routed {kind} event from {event.get('issuer')} for {event.get('battery_id')}."

    def supplier(self, evidence):
        ids = [e["id"] for e in evidence]
        claims = [e for e in evidence if e["kind"] == "corrected_claim"] or \
                 [e for e in evidence if e["kind"] == "supplier_claim"]
        transports = [e for e in evidence if e["kind"] == "transport_record"]
        if not claims:
            return "defer", "A signed supplier claim is needed before the lot can be assessed.", ids
        kinds = [e["kind"] for e in evidence]
        if "adverse_report" in kinds and (
                "audit_report" not in kinds or kinds[::-1].index("adverse_report") < kinds[::-1].index("audit_report")):
            report = [e for e in evidence if e["kind"] == "adverse_report"][-1]["content"]
            return "escalate_compliance", (f"An adverse report ({report.get('report_id')}) about "
                                           f"{report.get('subject_site') or 'the mine'} has no later audit; "
                                           "Compliance must decide."), ids
        origin = claims[-1]["content"].get("origin_site")
        if not transports:
            return "no_issue", (f"The verified signed claim names {origin or 'no mine (manufactured component)'}; "
                                "no evidence contradicts it."), ids
        loaded = transports[-1]["content"].get("loaded_at_site")
        if self.agree_all or (origin and origin == loaded):
            return "no_issue", f"Supplier claim origin {origin} matches the transport loading site {loaded}.", ids
        return "request_review", (f"Supplier claim names {origin}, but the transport record was loaded at "
                                  f"{loaded}. A human Reviewer must decide."), ids


class HeaderClient:
    """The local app called as one identity, the way a signed-in caller reaches the deployed app."""

    def __init__(self, client, headers):
        self.client, self.headers = client, headers

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)

    def request(self, method, path, **kwargs):
        return self.client.request(method, path, headers=self.headers, **kwargs)


class LocalWorkflowDispatcher:
    """Accept durable-workflow triggers without executing them; the scenario invokes its scripted double directly."""

    def __init__(self):
        self.accepted = []

    def dispatch(self, trigger):
        trigger_id = trigger.get("trigger_id")
        if not isinstance(trigger_id, str) or not trigger_id:
            raise ValueError("A local workflow trigger needs a stable ID.")
        self.accepted.append(trigger_id)
        return trigger_id


def identity(user, roles):
    return {LOCAL_USER_HEADER: user, LOCAL_ROLES_HEADER: roles}


class LocalApp:
    def __init__(self, folder, double, clock=None, settings_admin=None, prices=None):
        # A ticking clock gives every write a distinct timestamp, as SQL time does in Azure.
        start, ticks = datetime.now(timezone.utc) - timedelta(minutes=30), itertools.count()
        self.service = PassportService(MemoryStore(), LocalBlobs(Path(folder) / "blobs"), agent_mode=True,
                                       clock=clock or (lambda: format_utc(start + timedelta(seconds=next(ticks)))))
        self.workflow_dispatcher = LocalWorkflowDispatcher()
        self.service.workflow_dispatcher = self.workflow_dispatcher
        self.double = double
        settings = Settings(auth_mode="local", worker=False, agent_mode=True, prices=prices or {})
        self.client = TestClient(create_app(settings, self.service, double, settings_admin=settings_admin))
        self.staff = HeaderClient(self.client, identity("local-reviewer", STAFF))
        self.anonymous = HeaderClient(self.client, {})
        self.roleless = HeaderClient(self.client, identity("local-no-role", ""))
        self.agent = HeaderClient(self.client, identity("local-agent", "Passport.Agent.Supplier"))
        self.reviewer = HeaderClient(self.client, identity("local-reviewer-only", "Passport.Reviewer"))
        self.lifecycle_agent = HeaderClient(self.client, identity("local-lifecycle-agent", "Passport.Agent.Lifecycle"))
        self.viewers = {name: HeaderClient(self.client, identity(f"local-{name.lower()}", roles))
                        for name, roles in (("OEM", "Passport.OEM,Passport.Fleet.AdventureWorks"),
                                            ("Recycler", "Passport.Recycler"), ("Authority", "Passport.Authority"))}

    def seed(self, runner):
        """Import the series' package from the synthetic data generator, upload its transport records and send every
        lot's signed claim through the mock connector."""
        c = runner.series
        state = simulator.prepare(c.extra, c.tag, c.output)
        for attempt in (1, 2):
            for name, data in read_folder(simulator.build_paths(c.output, state)["package"]).items():
                self.service.blobs.put(INTAKE, f"{state['build_id']}/{name}", data, "application/json")
            response = call(runner.http, "POST", "/packages/import", f"Importing {state['build_id']}", json={
                "intake_folder": state["build_id"], "command_id": f"seed-import-{state['manifest_sha256'][:16]}"})
            body = body_of(response)
            if response.status_code < 400:
                break
            if response.status_code == 409 and body.get("error") in ("stale_package", "build_conflict") \
                    and attempt == 1:
                latest = body.get("latest_build_sequence")
                state = simulator.prepare(c.extra, c.tag, c.output, new_build=True,
                                          min_sequence=latest + 1 if isinstance(latest, int) else None)
                continue
            raise Stop(STOPPED, f"the package import of {state['build_id']} answered HTTP {response.status_code}: "
                                f"{problem(body)}.")
        runner.out(f"  package {state['build_id']}: {body.get('outcome')}")
        gold = aftersales.run_local(state, output=c.output)
        delivery = gold["delivery_id"]
        self.service.blobs.put(INTAKE, f"aftersales/{delivery}/aftersales.json", after_bytes(gold), "application/json")
        response = call(runner.http, "POST", f"/imports/aftersales/{delivery}", "Importing local aftersales Gold",
                        json={})
        receipt = body_of(response)
        if response.status_code not in (200, 201) or any(receipt.get(k) != gold[k] for k in
                                                         ("delivery_id", "series_id", "as_of_utc", "sha256")):
            raise Stop(STOPPED, f"Local aftersales import {delivery} did not return its expected receipt: {receipt}")
        runner.out(f"  warranty and recall: local Gold delivery {delivery} imported.")
        # The deployed app reads the registry from DBP_PARTY_REGISTRY_B64; the local app takes the series' copy.
        self.client.app.state.connector.sync(c.registry())
        for item in c.opening_evidence():
            runner.upload(item)
        runner.connect()

    def runner(self, series, register, out=print, sleep=lambda _: None, app_url=""):
        return Runner(self.staff, series, register, seed=self.seed, anonymous=self.anonymous,
                      confirm=lambda _: "RESOLVE", sleep=sleep, out=out, roleless=self.roleless,
                      agent_http=self.agent, double=self.double, reviewer_only=self.reviewer,
                      lifecycle_http=self.lifecycle_agent, viewers=self.viewers, workbook=LOCAL_WORKBOOK,
                      app_url=app_url)

    def close(self):
        self.client.close()


def play(tag=None, batteries=0, out=print, out_dir=OUTPUT, double=None, prices=None, acts=ACTS, app=None,
             series=None):
    """Run the acts in order against one in-memory app; stop at the first act that does not PASS."""
    with tempfile.TemporaryDirectory(prefix="dbp-local-", ignore_cleanup_errors=True) as tmp:
        series = series or Series(tag, batteries, output=Path(tmp) / "output")
        own = app is None
        app = app or LocalApp(tmp, double or ScriptedDouble(), prices=prices)
        try:
            results = {}
            for act, options in acts:
                out(f"\n=== {act} ===")
                register = Register(series, act, "local", prices=prices, out_dir=out_dir)
                runner = app.runner(series, register, out)
                results[act] = (runner.execute(act, **options), register)
                if results[act][0] != PASS:
                    break
            out("")
            app.runner(series, Register(series, "status", "local", out_dir=out_dir), out).show_status()
            return results
        finally:
            if own:
                app.close()
