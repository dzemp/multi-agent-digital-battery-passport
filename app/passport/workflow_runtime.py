"""Durable Microsoft Agent Framework workflow for application-triggered agent tasks."""

import asyncio
import json
import logging
from typing import Any

from agent_framework import Case, Default, Executor, WorkflowBuilder, WorkflowContext, handler
from agent_framework_durabletask import DurableAIAgentWorker, DurableWorkflowClient
from azure.identity import ManagedIdentityCredential
from durabletask.azuremanaged.client import DurableTaskSchedulerClient
from durabletask.azuremanaged.worker import DurableTaskSchedulerWorker
from typing_extensions import Never

from .service import CommandError

log = logging.getLogger("passport.workflow")
WORKFLOW_NAME = "battery_passport_agent_flow"


def _continue_supplier(step):
    return step.get("role") == "supplier" and step.get("status_code") == 200 and \
        (step.get("decision") or {}).get("action") == "no_issue"


def _verified_carbon(step):
    return step.get("role") == "carbon" and step.get("status_code") == 200 and step.get("verified") is True


class TriggerRouter(Executor):
    @handler
    async def route(self, trigger: dict[str, Any], ctx: WorkflowContext[Never, dict[str, Any]]) -> None:
        await ctx.send_message(trigger)


class AgentStepExecutor(Executor):
    def __init__(self, runtime, role, executor_id):
        super().__init__(id=executor_id)
        self.runtime, self.role = runtime, role

    @handler
    async def run(self, trigger: dict[str, Any], ctx: WorkflowContext[Never, dict[str, Any]]) -> None:
        step = await asyncio.to_thread(self.runtime.run_agent, trigger, self.role)
        await ctx.send_message(step)


class WorkflowStop(Executor):
    @handler
    async def stop(self, step: dict[str, Any], ctx: WorkflowContext[Never, dict[str, Any]]) -> None:
        await ctx.yield_output(step)


def create_workflow(runtime):
    router = TriggerRouter(id="application_event_router")
    supplier = AgentStepExecutor(runtime, "supplier", "supplier_agent")
    carbon = AgentStepExecutor(runtime, "carbon", "carbon_agent")
    issuance = AgentStepExecutor(runtime, "issuance", "issuance_agent")
    lifecycle = AgentStepExecutor(runtime, "lifecycle", "lifecycle_agent")
    watch = AgentStepExecutor(runtime, "watch", "watch_agent")
    stop = WorkflowStop(id="workflow_complete")

    return (
        WorkflowBuilder(name=WORKFLOW_NAME, start_executor=router)
        .add_switch_case_edge_group(
            router,
            [
                Case(condition=lambda item: item.get("kind") == "supplier_lot", target=supplier),
                Case(condition=lambda item: item.get("kind") == "lifecycle_event", target=lifecycle),
                Case(condition=lambda item: item.get("kind") == "watch", target=watch),
                Default(target=stop),
            ],
        )
        .add_switch_case_edge_group(
            supplier,
            [Case(condition=_continue_supplier, target=carbon), Default(target=stop)],
        )
        .add_switch_case_edge_group(
            carbon,
            [Case(condition=_verified_carbon, target=issuance), Default(target=stop)],
        )
        .add_edge(issuance, stop)
        .add_edge(lifecycle, stop)
        .add_edge(watch, stop)
        .build()
    )


class AgentWorkflowRuntime:
    def __init__(self, service, settings, runner=None, events=None):
        self.service, self.settings, self.runner, self.events = service, settings, runner, events
        self.agent_service = None
        self.worker = None
        self.client = None
        self.credential = None
        self.workflow_client = None
        self.last_error = None

    def _runner(self):
        if self.runner is None:
            from .foundry_runtime import FoundryRunner

            self.runner = FoundryRunner(self.settings.foundry_config)
        return self.runner

    def run_agent(self, trigger, role):
        from .agent_service import AgentService

        agents = self.agent_service or AgentService(self.service)
        subject = trigger.get("lot_id") if role == "supplier" else (
            "battery-due-diligence" if role == "watch" else trigger.get("battery_id"))
        event = trigger.get("event") if role == "lifecycle" else None
        task = None
        replay = False
        try:
            task = agents.prepare_auto(role, subject, event, "workflow:application-event")
            prior = agents.result(task["task_id"])
            if prior:
                replay = True
                status, body = prior
                decision = body.get("decision")
            else:
                attempt_id, decision, metadata = agents.invoke_attempt(task, self._runner())
                status, body = agents.finish(task, decision, metadata, attempt_id=attempt_id)
        except CommandError as error:
            if task is None:
                log.exception("Durable %s task preparation failed (trigger %s, subject %s)",
                              role, trigger.get("trigger_id"), subject)
                raise
            status, body, decision = error.status, error.body, None
            status, body = agents.fail(task, error.body["error"], str(error), error.status)
        except Exception as error:  # Keep a durable failure visible; never substitute a test or permissive result.
            log.exception("Durable %s agent task failed (%s)", role, type(error).__name__)
            if task is None:
                raise
            status, body, decision = 502, {"error": "agent_execution_failed",
                                           "message": f"{type(error).__name__}: {str(error)[:400]}"}, None
            status, body = agents.fail(task, body["error"], body["message"], status)
        if self.events is not None and not replay and task is not None:
            self.events.agent_run(role, task, status, body, decision)
        verified = False
        if role == "carbon" and status == 200:
            with self.service.store.read() as tx:
                facts = [fact for fact in tx.select("AgentFacts", kind="carbon_verifier", subject=subject)
                         if fact["input_hash"] == task["context_hash"] or
                         json.loads(fact["data_json"]).get("calculation_fact_id") == task["task_id"]]
            verified = bool(facts)
        return {**trigger, "role": role, "status_code": status, "decision": decision,
                "outcome": body.get("outcome"), "verified": verified,
                "error": body.get("error"), "message": body.get("message"),
                "task_id": body.get("task_id") or task["task_id"]}

    def start(self):
        credential = self.credential = ManagedIdentityCredential(client_id=self.settings.client_id)
        self.worker = DurableTaskSchedulerWorker(
            host_address=self.settings.dts_endpoint,
            secure_channel=not self.settings.dts_endpoint.startswith("http://"),
            taskhub=self.settings.dts_taskhub,
            token_credential=credential,
        )
        self.agent_service = __import__("passport.agent_service", fromlist=["AgentService"]).AgentService(self.service)
        agent_worker = DurableAIAgentWorker(self.worker, deployment_mode="isolated_v2")
        agent_worker.configure_workflow(create_workflow(self))
        self.client = DurableTaskSchedulerClient(
            host_address=self.settings.dts_endpoint,
            secure_channel=not self.settings.dts_endpoint.startswith("http://"),
            taskhub=self.settings.dts_taskhub,
            token_credential=credential,
        )
        self.workflow_client = DurableWorkflowClient(self.client, workflow_name=WORKFLOW_NAME)
        self.worker.start()

    def stop(self):
        if self.worker is not None:
            stop = getattr(self.worker, "stop", None)
            if stop is not None:
                stop()
            self.worker = None
        if self.client is not None:
            self.client.close()
            self.client = None
        if self.credential is not None:
            self.credential.close()
            self.credential = None
        self.workflow_client = None

    def dispatch(self, trigger):
        if self.workflow_client is None:
            raise RuntimeError("The durable workflow worker is not running.")
        instance_id = trigger["trigger_id"]
        try:
            return self.workflow_client.start_workflow(input=trigger, instance_id=instance_id)
        except Exception:
            try:
                if self.client.get_orchestration_state(instance_id, fetch_payloads=False) is not None:
                    return instance_id
            except Exception:
                log.exception("Could not confirm whether workflow %s was already accepted", instance_id)
            raise
