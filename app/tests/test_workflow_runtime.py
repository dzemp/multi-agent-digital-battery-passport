"""Local route and halt behavior for the durable Agent Framework workflow graph."""

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import helpers
from passport.agent_contracts import SECTIONS
from passport.agent_service import AgentService
from passport.events import Events
from passport.blobs import LocalBlobs
from passport.exec_views import series_snapshot
from passport.service import CommandError, PassportService
from passport.store import MemoryStore
from passport.telemetry import Telemetry
from passport.workflow_runtime import AgentWorkflowRuntime, create_workflow


class FakeAgentRuntime:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def run_agent(self, trigger, role):
        self.calls.append(role)
        return self.results[role]


def invoked_roles(events):
    return [event.executor_id.removesuffix("_agent") for event in events
            if event.type == "executor_invoked" and event.executor_id.endswith("_agent")]


class WorkflowRoutes(unittest.TestCase):
    def run_workflow(self, trigger, results):
        runtime = FakeAgentRuntime(results)
        events = asyncio.run(create_workflow(runtime).run(trigger))
        return runtime.calls, invoked_roles(events)

    def test_supplier_no_issue_continues_only_after_verified_carbon(self):
        results = {
            "supplier": {"role": "supplier", "status_code": 200,
                         "decision": {"action": "no_issue"}},
            "carbon": {"role": "carbon", "status_code": 200, "verified": True},
            "issuance": {"role": "issuance", "status_code": 200},
        }
        calls, routed = self.run_workflow({"kind": "supplier_lot"}, results)
        self.assertEqual(calls, ["supplier", "carbon", "issuance"])
        self.assertEqual(routed, calls)

    def test_supplier_hold_and_unverified_carbon_stop_before_issuance(self):
        held = {"supplier": {"role": "supplier", "status_code": 200,
                             "decision": {"action": "request_review"}}}
        calls, _ = self.run_workflow({"kind": "supplier_lot"}, held)
        self.assertEqual(calls, ["supplier"])

        unverified = {
            "supplier": {"role": "supplier", "status_code": 200,
                         "decision": {"action": "no_issue"}},
            "carbon": {"role": "carbon", "status_code": 200, "verified": False},
        }
        calls, _ = self.run_workflow({"kind": "supplier_lot"}, unverified)
        self.assertEqual(calls, ["supplier", "carbon"])

    def test_lifecycle_and_watch_triggers_run_only_their_own_agent(self):
        for kind, role in (("lifecycle_event", "lifecycle"), ("watch", "watch")):
            with self.subTest(kind=kind):
                calls, _ = self.run_workflow({"kind": kind}, {role: {"role": role, "status_code": 200}})
                self.assertEqual(calls, [role])

    def test_outbox_retry_recognizes_an_already_accepted_stable_instance(self):
        class WorkflowClient:
            def start_workflow(self, input, instance_id):
                raise RuntimeError("response lost after acceptance")

        class SchedulerClient:
            def __init__(self, state):
                self.state = state

            def get_orchestration_state(self, instance_id, fetch_payloads):
                return self.state

        runtime = AgentWorkflowRuntime(None, None)
        runtime.workflow_client = WorkflowClient()
        runtime.client = SchedulerClient(object())
        self.assertEqual(runtime.dispatch({"trigger_id": "wf-stable"}), "wf-stable")

        runtime.client.state = None
        with self.assertRaisesRegex(RuntimeError, "response lost"):
            runtime.dispatch({"trigger_id": "wf-not-accepted"})


class WorkflowExecutionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="dbp-workflow-test-")
        self.addCleanup(folder.cleanup)
        self.service = PassportService(MemoryStore(), LocalBlobs(folder.name))
        self.service.agent_mode = True
        files = helpers.make_package("BUILD-01", 1, [helpers.battery("BAT-101", "LOT-LI")])
        helpers.put_package(self.service.blobs, "workflow", files)
        self.assertEqual(self.service.import_package("workflow", helpers.OPERATOR, "import-flow-1")[0], 200)
        for lot in ("LOT-LI", "LOT-CO", "LOT-NI", "LOT-GR"):
            status, _ = self.service.upload_evidence(
                "supplier_claim", lot, "claim.json", "application/json",
                json.dumps({"origin_site": "Synthetic site"}).encode(), helpers.REVIEWER, f"claim-{lot}")
            self.assertEqual(status, 201)
        self.agents = AgentService(self.service)
        self.runner = mock.Mock()
        self.runner.run.side_effect = self.propose
        self.runtime = AgentWorkflowRuntime(self.service, None, self.runner)

    @staticmethod
    def propose(role, task_id, context, reader, *, attempt_id=None):
        sections = {name: reader(task_id, role, name) for name in SECTIONS[role]}
        evidence = []
        if role == "supplier":
            action = "no_issue"
            evidence = [e["id"] for e in sections["evidence"]]
        elif role == "carbon":
            action = "calculate"
        else:
            action = "publish" if sections["gates"]["ready"] else "defer"
        return {"action": action, "reason": "Scripted workflow check.", "evidence_ids": evidence}, {
            "hosting": "local_scripted_double"}

    def test_real_service_flow_preserves_battery_and_publishes_only_after_all_lots_clear(self):
        for index, lot in enumerate(("LOT-LI", "LOT-CO", "LOT-NI", "LOT-GR")):
            trigger = {"kind": "supplier_lot", "subject": lot, "lot_id": lot,
                       "battery_id": "BAT-101", "trigger_id": f"wf-test-{lot}"}
            events = asyncio.run(create_workflow(self.runtime).run(trigger))
            self.assertEqual(invoked_roles(events), ["supplier", "carbon", "issuance"])
            with self.service.store.read() as tx:
                state = tx.get("PassportState", "BAT-101")
                self.assertEqual(state["committed_version"], 1 if index == 3 else 0)
        with self.service.store.read() as tx:
            self.assertEqual(len(tx.select("AgentFacts", kind="supplier")), 4)
            self.assertTrue(tx.select("AgentFacts", kind="carbon_verifier", subject="BAT-101"))
            self.assertTrue(self.agents.gates(tx, state)["ready"])
            self.assertEqual({t["subject"] for t in tx.select("AgentTasks") if t["role"] != "supplier"},
                             {"BAT-101"})
        step = self.runtime.run_agent(trigger, "supplier")
        for key in ("kind", "subject", "lot_id", "battery_id", "trigger_id"):
            self.assertEqual(step[key], trigger[key])

    def test_preparation_failure_is_logged_and_fails_the_workflow_instead_of_disappearing(self):
        for failure in (CommandError(404, "battery_not_found", "Import a battery first."),
                        RuntimeError("store unavailable")):
            with self.subTest(failure=type(failure).__name__):
                with mock.patch.object(AgentService, "prepare_auto", side_effect=failure):
                    with self.assertLogs("passport.workflow", level="ERROR"):
                        with self.assertRaises(type(failure)):
                            self.runtime.run_agent({"battery_id": "BAT-101", "trigger_id": "wf-broken"}, "carbon")
        self.runner.run.assert_not_called()

    def test_execution_failure_keeps_task_result_and_error_details(self):
        self.runner.run.side_effect = RuntimeError("model unavailable")
        with self.assertLogs("passport.workflow", level="ERROR"):
            step = self.runtime.run_agent({"battery_id": "BAT-101", "trigger_id": "wf-failed"}, "carbon")
        self.assertEqual(step["status_code"], 502)
        self.assertEqual(step["error"], "agent_execution_failed")
        self.assertIn("model unavailable", step["message"])
        self.assertEqual(step["battery_id"], "BAT-101")
        status, body = self.agents.result(step["task_id"])
        self.assertEqual((status, body["error"]), (502, step["error"]))

    def test_tool_budget_is_scoped_to_each_attempt(self):
        task = self.agents.prepare("supplier", "LOT-LI", None, "workflow:application-event", "budget-task")
        first_attempt, second_attempt = "a" * 32, "b" * 32
        with self.assertRaisesRegex(CommandError, "Start this agent attempt"):
            self.agents.section(task["task_id"], "supplier", "evidence", first_attempt)
        with self.assertRaisesRegex(CommandError, "32-character lowercase hexadecimal attempt ID"):
            self.agents.section(task["task_id"], "supplier", "evidence", require_attempt=True)
        self.agents.start_attempt(task["task_id"], first_attempt)
        for _ in range(8):
            self.agents.section(task["task_id"], "supplier", "evidence", first_attempt)
        with self.assertRaisesRegex(CommandError, "eight permitted tool calls"):
            self.agents.section(task["task_id"], "supplier", "evidence", first_attempt)

        self.agents.start_attempt(task["task_id"], second_attempt)
        self.agents.section(task["task_id"], "supplier", "evidence", second_attempt)

    def test_workflow_emits_agent_run_telemetry(self):
        emitted = []
        telemetry = Telemetry(lambda name, dimensions, measurements: emitted.append(name))
        events = Events(self.service, telemetry)
        runtime = AgentWorkflowRuntime(self.service, None, self.runner, events)
        step = runtime.run_agent({"kind": "supplier_lot", "subject": "LOT-LI", "lot_id": "LOT-LI",
                                  "battery_id": "BAT-101", "trigger_id": "wf-telemetry"}, "supplier")
        self.assertEqual(step["status_code"], 200)
        self.assertEqual(emitted.count("agent.run"), 1)

    def test_series_snapshot_counts_pending_work_older_than_15_minutes(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.service.clock = lambda: (now - timedelta(minutes=16)).isoformat()
        self.agents.prepare("supplier", "LOT-LI", None, "workflow:application-event", "old-supplier-work")
        self.service.clock = lambda: now.isoformat()

        snapshot = series_snapshot(self.service, "SERIES-V3")
        self.assertEqual(snapshot["stuck_batteries"], 1)


if __name__ == "__main__":
    unittest.main()
