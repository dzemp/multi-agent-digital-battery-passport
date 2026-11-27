"""HTTP layer: a small FastAPI app with a JSON API; the HTML screens live in screens.py and templates/.

Run in Azure:  uvicorn passport.web:create_app --factory   (Easy Auth in front, managed identity behind)
Run in tests:  create_app(settings, service) with DBP_AUTH_MODE=local and the in-memory store.
"""

import json
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from pathlib import Path as FilePath
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Path, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import APP_VERSION, access, aftersales_import, experience, screens
from . import telemetry as tele
from .auth import AUTHORITY, COMPLIANCE, CONNECTOR, OPERATOR, POLICY_APPROVER, REVIEWER
from .blobs import AzureBlobs, LocalBlobs
from .config import Settings
from .connector import Connector
from .events import Events
from .ledger import build_ledger
from .service import EVIDENCE_KINDS, PassportService
from .settings_store import SettingsAdmin
from .store import SCHEMA_VERSION, MemoryStore

log = logging.getLogger("passport")
ID = r"^[A-Za-z0-9-]{1,64}$"
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
STAFF = (OPERATOR, REVIEWER)
CASE_READERS = (COMPLIANCE, REVIEWER, AUTHORITY)
EVIDENCE_UPLOADERS = {  # role -> evidence kinds it may upload
    REVIEWER: {"supplier_claim", "transport_record", "corrected_claim", "other", "adverse_report"},
    COMPLIANCE: {"adverse_report", "audit_report"},
}
MAX_MESSAGE_BYTES = 64 * 1024
CSP = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; script-src-attr 'none'; "
       "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")


def build_service(settings):
    if settings.store == "sql":
        from .sql_store import SqlStore

        store = SqlStore(settings.sql_server, settings.sql_database, settings.client_id)
    else:
        store = MemoryStore()
    blobs = AzureBlobs(settings.blob_account_url) if settings.blobs == "azure" else LocalBlobs(settings.blob_root)
    return PassportService(store, blobs, max_upload_bytes=settings.max_upload_bytes, agent_mode=settings.agent_mode)


class OutboxWorker:
    """Writes committed passport snapshots to Blob in the background. Pausing lives in memory only,
    so a restart resumes the work: the queue itself is in the database, not in this process."""

    def __init__(self, service, interval, events=None):
        self.service, self.interval, self.events = service, interval, events
        self.paused, self.last_error, self.running = False, None, False
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._loop, name="outbox-worker", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        self.running = False

    def _loop(self):
        while not self._stop.wait(self.interval):
            if not self.paused:
                try:
                    self.service.schedule_due_watch()
                    results = self.service.drain_outbox()
                    self.last_error = None
                except Exception as exc:  # noqa: BLE001 - keep the worker alive and show the error
                    results = []
                    self.last_error = f"{type(exc).__name__}: {exc}"[:500]
                    log.warning("outbox worker: %s", self.last_error)
                if self.events:
                    self.events.outbox_cycle(results)
            if self.events:
                self.events.snapshots()  # state snapshots continue while paused: a paused queue is news

    def status(self):
        return {"running": self.running, "paused": self.paused, "last_error": self.last_error}


FIXED = "Saving is off: a test or the local run script started this app with fixed settings."


def load_settings(admin):
    """Deployment values with the saved overrides on top. Saved values that do not combine fall back to the
    deployment values, so a bad save can never stop the app; a broken deployment still fails as before."""
    env = admin.env()
    try:
        return Settings.from_env(env), env
    except ValueError as error:
        if not admin.saved:
            raise
        admin.rejected(str(error))
        return Settings.from_env(admin.base_env), admin.base_env


def create_app(settings=None, service=None, agent_runner=None, telemetry=None, ledger=None, settings_admin=None,
               workflow_runtime=None):
    env = None
    if settings is None:
        admin = settings_admin or SettingsAdmin.load(os.environ)
        settings, env = load_settings(admin)
        if admin.load_error:
            log.warning("settings: %s", admin.load_error)
    else:
        admin = settings_admin or SettingsAdmin.fixed(FIXED)
    service = service or build_service(settings)
    connector = Connector(service, settings.party_registry)
    telemetry = telemetry or tele.from_env(env)
    admin.start(settings, env)
    ledger = ledger or build_ledger(settings, service.store)
    events = Events(service, telemetry, settings.prices)
    if settings.agent_mode and settings.dts_endpoint:
        if workflow_runtime is None:
            from .workflow_runtime import AgentWorkflowRuntime

            workflow_runtime = AgentWorkflowRuntime(service, settings, agent_runner, events)
        service.workflow_dispatcher = workflow_runtime
    worker = OutboxWorker(service, settings.worker_interval_seconds, events)

    @asynccontextmanager
    async def lifespan(_app):
        if workflow_runtime is not None:
            workflow_runtime.start()
        if settings.worker:
            worker.start()
        yield
        worker.stop()
        if workflow_runtime is not None:
            workflow_runtime.stop()

    app = FastAPI(title="Digital battery passport (synthetic MVP)", version=APP_VERSION, lifespan=lifespan,
                  docs_url="/docs", redoc_url=None)
    app.mount("/static", StaticFiles(directory=FilePath(__file__).with_name("static")), name="static")
    app.state.settings, app.state.service, app.state.worker = settings, service, worker
    app.state.telemetry, app.state.ledger, app.state.events = telemetry, ledger, events
    app.state.settings_admin, app.state.connector = admin, connector

    @app.middleware("http")
    async def protect(request: Request, call_next):
        if request.method in UNSAFE and settings.auth_mode == "easyauth" and not same_origin(request):
            return JSONResponse({"error": "cross_site_request", "message": "Form posted from another site."}, 403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = CSP
        response.headers["Referrer-Policy"] = "same-origin"
        if (response.headers.get("content-type", "").startswith("text/html") or request.url.path == "/me"
                or request.url.path.endswith("/view")):
            response.headers["Cache-Control"] = "no-store"
        return response

    def who(request):
        return experience.effective(request, settings)

    def require(request, *roles):
        person = who(request)
        if person is None:
            events.access_denied(request, None)
            raise HTTPException(401, "Sign in first.")
        if not person.has_any(roles):
            events.access_denied(request, person)
            raise HTTPException(403, f"This needs the app role {' or '.join(roles)}.")
        return person

    def reply(status, body, data):
        target = data.get("return_to")
        if target == "/" or (isinstance(target, str) and re.fullmatch(r"/cases/[A-Za-z0-9-]{1,64}", target)):
            text = f"{status} " + json.dumps(body, sort_keys=True)
            return RedirectResponse(f"{target}?msg={quote(text[:400])}", status_code=303)
        return JSONResponse(body, status_code=status)

    def found(value, what):
        if value is None:
            raise HTTPException(404, f"No such {what}.")
        return value

    @app.get("/health/live")
    def live():
        return {"status": "alive", "version": APP_VERSION}

    @app.get("/health/ready")
    def ready():
        try:
            service.store.ping()
            service.blobs.ping()
            schema_ok = service.schema_ok()
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"status": "not_ready", "error": f"{type(exc).__name__}"}, 503)
        if not schema_ok:
            return JSONResponse({"status": "not_ready", "error": "schema_mismatch",
                                 "expected_schema_version": SCHEMA_VERSION}, 503)
        return {"status": "ready", "store": service.store.kind, "blobs": service.blobs.kind,
                "schema_version": SCHEMA_VERSION}

    @app.get("/me")
    def me(request: Request):
        person = who(request)
        if person is None:
            return {"signed_in": False}
        return {"signed_in": True, "name": person.name, "user_id": person.user_id,
                "roles": sorted(person.roles), "source": person.source}

    # ---- passports -----------------------------------------------------------------------------
    @app.get("/passports")
    def passports():
        return {"passports": service.list_passports(), "board": service.board()}

    @app.get("/passports/{battery_id}")
    def passport(battery_id: str = Path(pattern=ID)):
        return found(service.public_passport(battery_id), "passport")

    @app.get("/passports/{battery_id}/restricted")
    def restricted(request: Request, battery_id: str = Path(pattern=ID)):
        require(request, *STAFF)
        return found(service.restricted_passport(battery_id), "passport")

    @app.get("/passports/{battery_id}/view")
    def policy_view(request: Request, battery_id: str = Path(pattern=ID)):
        person = who(request)
        view = found(access.project(service, battery_id, person), "passport")
        events.view_served(view)
        return view

    @app.get("/passports/{battery_id}/versions/{version}/verify")
    def verify_snapshot(request: Request, version: int, battery_id: str = Path(pattern=ID)):
        require(request, *STAFF)
        return found(service.verify_snapshot(battery_id, version), "passport version")

    # ---- commands ------------------------------------------------------------------------------
    async def read_input(request):
        if request.headers.get("content-type", "").startswith("application/json"):
            try:
                data = await request.json()
            except ValueError:
                raise HTTPException(400, "Body is not valid JSON.") from None
            if not isinstance(data, dict):
                raise HTTPException(400, "Send a JSON object.")
            return data
        async with request.form() as form:
            return {k: v for k, v in form.items() if isinstance(v, str)}

    from .agent_service import AgentService
    from .agent_contracts import ROLES
    from .service import CommandError

    agents = AgentService(service)

    def agent_enabled():
        if not settings.agent_mode:
            raise HTTPException(409, "Agent mode is not enabled.")

    @app.get("/agents/runs")
    def agent_runs(request: Request):
        agent_enabled()
        require(request, *STAFF)
        with service.store.read() as tx:
            return {"tasks": [{k: t[k] for k in ("task_id", "role", "subject", "context_hash", "created_utc")}
                              for t in tx.select("AgentTasks")],
                    "results": [json.loads(r["result_json"]) for r in tx.select("AgentResults")],
                    "facts": tx.select("AgentFacts")}

    @app.get("/policy/candidates")
    def policy_candidates(request: Request):
        agent_enabled()
        require(request, POLICY_APPROVER)
        return {"active": agents.active_policy(), "candidates": agents.policy_candidates()}

    async def decide_policy(request, candidate_id, decide):
        agent_enabled()
        person = require(request, POLICY_APPROVER)
        data = await read_input(request)
        try:
            expected = int(data.get("expected_row_version"))
        except (TypeError, ValueError):
            return JSONResponse({"error": "row_version_required", "message": "Send the candidate row_version."},
                                422)
        try:
            result = await run_in_threadpool(decide, candidate_id, expected, data.get("reason"), person.actor())
        except CommandError as error:
            return JSONResponse(error.body, error.status)
        return JSONResponse(result)

    @app.post("/policy/candidates/{candidate_id}/activate")
    async def activate_policy(request: Request, candidate_id: str = Path(pattern=ID)):
        return await decide_policy(request, candidate_id, agents.activate_policy)

    @app.post("/policy/candidates/{candidate_id}/reject")
    async def reject_policy(request: Request, candidate_id: str = Path(pattern=ID)):
        return await decide_policy(request, candidate_id, agents.reject_policy)

    @app.post("/provenance/verify")
    async def verify_provenance(request: Request):
        """Detection of AI text this application generated (EU AI Act Art. 50(2)); restricted to expert roles."""
        require(request, AUTHORITY, COMPLIANCE, OPERATOR)
        data = await read_input(request)
        if set(data) - {"text", "provenance_id"}:
            return JSONResponse({"error": "unknown_fields", "message": "Send text or provenance_id only."}, 422)
        try:
            return await run_in_threadpool(agents.verify_provenance, data.get("text"), data.get("provenance_id"))
        except CommandError as error:
            return JSONResponse(error.body, error.status)

    @app.get("/agents/tasks/{task_id}/{section}")
    def agent_section(request: Request, task_id: str, section: str, attempt_id: str | None = None):
        agent_enabled()
        person = require(request, "Passport.Agent.Supplier", "Passport.Agent.Lifecycle")
        role = "supplier" if person.has_any(("Passport.Agent.Supplier",)) else "lifecycle"
        try:
            return agents.section(task_id, role, section, attempt_id, require_attempt=True)
        except CommandError as error:
            if error.status == 403:
                events.agent_denied(request, person, error.body.get("error") or "section_denied")
            return JSONResponse(error.body, error.status)

    @app.post("/agents/{role}/run")
    async def agent_run(request: Request, role: str):
        agent_enabled()
        person = require(request, OPERATOR)
        data = await read_input(request)
        if role not in ROLES or set(data) - {"task_id", "subject", "event"} or \
                not isinstance(data.get("subject"), str):
            raise HTTPException(422, "Send task_id, subject and (Lifecycle only) event for a known role.")
        try:
            task = await run_in_threadpool(agents.prepare, role, data["subject"], data.get("event"),
                                           person.actor(), data.get("task_id"))
        except CommandError as error:
            return JSONResponse(error.body, error.status)
        prior = await run_in_threadpool(agents.result, task["task_id"])
        if prior:
            return JSONResponse(prior[1], prior[0])
        decision = None
        try:
            runner = agent_runner
            if runner is None:
                from .foundry_runtime import FoundryRunner
                runner = FoundryRunner(settings.foundry_config)
            attempt_id, decision, metadata = await run_in_threadpool(agents.invoke_attempt, task, runner)
            status, body = await run_in_threadpool(agents.finish, task, decision, metadata, attempt_id=attempt_id)
        except CommandError as error:
            status, body = await run_in_threadpool(agents.fail, task, error.body["error"], str(error), error.status)
        except Exception as error:  # Record failure, never substitute a mocked or permissive answer.
            log.exception("Agent task %s failed (%s)", task["task_id"], type(error).__name__)
            status, body = await run_in_threadpool(
                agents.fail, task, "agent_execution_failed", f"{type(error).__name__}: {str(error)[:400]}", 502)
        await run_in_threadpool(events.agent_run, role, task, status, body, decision)
        return JSONResponse(body, status)

    @app.post("/imports/aftersales/{delivery_id}")
    async def import_aftersales(request: Request, delivery_id: str = Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")):
        person = require(request, OPERATOR)
        if (await request.body()).strip() not in (b"", b"{}"):
            raise HTTPException(422, "Send no body. The app reads the named delivery from Gold intake.")
        status, result = await run_in_threadpool(aftersales_import.import_delivery, service, delivery_id,
                                                 person.actor())
        return JSONResponse(result, status_code=status)

    @app.post("/packages/import")
    async def import_package(request: Request):
        person = require(request, OPERATOR)
        data = await read_input(request)
        status, body = await run_in_threadpool(service.import_package, data.get("intake_folder"),
                                               person.actor(), data.get("command_id"))
        return reply(status, body, data)

    @app.post("/evidence")
    async def upload_evidence(request: Request):
        person = require(request, REVIEWER, COMPLIANCE)
        if (request.headers.get("content-length") or "0").isdigit() and \
                int(request.headers.get("content-length") or 0) > settings.max_upload_bytes + 65536:
            return JSONResponse({"error": "too_large", "message": "Files are limited to 5 MB."}, 413)
        async with request.form() as form:
            upload = form.get("file")
            if upload is None or isinstance(upload, str):
                return JSONResponse({"error": "file_required", "message": "Attach a file in the 'file' field."}, 422)
            content = await upload.read(settings.max_upload_bytes + 1)
            fields = {k: v for k, v in form.items() if isinstance(v, str)}
            file_name, content_type = upload.filename, upload.content_type
        allowed = set().union(*(kinds for role, kinds in EVIDENCE_UPLOADERS.items() if person.has_any((role,))))
        if fields.get("kind") in EVIDENCE_KINDS and fields.get("kind") not in allowed:
            events.agent_denied(request, person, "evidence_kind_not_allowed")
            return reply(403, {"error": "evidence_kind_not_allowed",
                               "message": f"Your roles may upload these kinds: {sorted(allowed)}."}, fields)
        status, body = await run_in_threadpool(
            service.upload_evidence, fields.get("kind"), fields.get("lot_id"), file_name,
            content_type, content, person.actor(), fields.get("command_id"))
        await run_in_threadpool(events.evidence_recorded, status, body)
        return reply(status, body, fields)

    # ---- mock connector and party registry -------------------------------------------------------
    async def read_json(request):
        if not request.headers.get("content-type", "").startswith("application/json"):
            raise HTTPException(415, "Send JSON.")
        raw = await request.body()
        if len(raw) > MAX_MESSAGE_BYTES:
            return None
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "Body is not valid JSON.") from None
        if not isinstance(data, dict):
            raise HTTPException(400, "Send a JSON object.")
        return data

    def too_large():
        return JSONResponse({"connector": "mock", "error": "too_large",
                             "message": f"A connector request may have at most {MAX_MESSAGE_BYTES} bytes."}, 413)

    @app.post("/connector/v1/agreements")
    async def connector_agreement(request: Request):
        person = require(request, CONNECTOR)
        data = await read_json(request)
        if data is None:
            return too_large()
        status, body = await run_in_threadpool(connector.record_agreement, data, person.actor())
        return JSONResponse(body, status)

    @app.post("/connector/v1/messages")
    async def connector_message(request: Request):
        person = require(request, CONNECTOR)
        data = await read_json(request)
        if data is None:
            return too_large()
        if set(data) - {"envelope", "correlation_id"}:
            return JSONResponse({"connector": "mock", "error": "bad_request",
                                 "message": "Send an envelope and optionally a correlation_id."}, 422)
        status, body = await run_in_threadpool(connector.receive, data, person.actor())
        await run_in_threadpool(events.connector_message, status, body)
        return JSONResponse(body, status)

    @app.get("/connector/v1/messages")
    def connector_messages(request: Request, limit: int = 200):
        require(request, OPERATOR, AUTHORITY)
        return {"connector": "mock", "messages": connector.messages(max(1, min(limit, 500))),
                "agreements": connector.agreements()}

    @app.get("/parties")
    def registered_parties(request: Request):
        require(request, OPERATOR, AUTHORITY)
        return {"connector": "mock", "parties": connector.parties()}

    @app.post("/reviews")
    async def open_review(request: Request):
        person = require(request, REVIEWER)
        data = await read_input(request)
        status, body = await run_in_threadpool(service.open_review, data.get("lot_id"), data.get("reason"),
                                               data.get("evidence_ids"), person.actor(), data.get("command_id"))
        await run_in_threadpool(events.hold_opened, status, body)
        return reply(status, body, data)

    async def resolve(request, review_id):
        person = require(request, REVIEWER)
        data = await read_input(request)
        review_id = review_id or str(data.get("review_id") or "").strip()
        try:
            expected = int(data.get("expected_row_version"))
        except (TypeError, ValueError):
            return reply(422, {"error": "row_version_required",
                               "message": "Send expected_row_version (the review's row_version you read)."}, data)
        status, body = await run_in_threadpool(service.resolve_review, review_id, expected, data.get("reason"),
                                               data.get("evidence_ids"), person.actor(), data.get("command_id"))
        await run_in_threadpool(events.hold_resolved, review_id, status, body)
        return reply(status, body, data)

    @app.post("/reviews/resolve")
    async def resolve_review_form(request: Request):
        return await resolve(request, None)

    @app.post("/reviews/{review_id}/resolve")
    async def resolve_review(request: Request, review_id: str = Path(pattern=ID)):
        return await resolve(request, review_id)

    @app.get("/compliance/cases")
    def compliance_cases(request: Request):
        require(request, *CASE_READERS)
        return {"cases": service.list_cases(), "labels": ["synthetic", "illustrative"]}

    @app.get("/compliance/cases/{case_id}")
    def compliance_case(request: Request, case_id: str = Path(pattern=ID)):
        require(request, *CASE_READERS)
        return found(service.case_detail(case_id), "compliance case")

    @app.post("/compliance/cases")
    async def open_compliance_case(request: Request):
        person = require(request, COMPLIANCE)
        data = await read_input(request)
        status, body = await run_in_threadpool(service.open_case, data.get("lot_id"), data.get("reason"),
                                               data.get("evidence_ids"), person.actor(), data.get("command_id"))
        await run_in_threadpool(events.case_opened, status, body)
        return reply(status, body, data)

    @app.post("/compliance/cases/{case_id}/decisions")
    async def decide_compliance_case(request: Request, case_id: str = Path(pattern=ID)):
        person = require(request, COMPLIANCE)
        data = await read_input(request)
        try:
            expected = int(data.get("expected_row_version"))
        except (TypeError, ValueError):
            return reply(422, {"error": "row_version_required",
                               "message": "Send expected_row_version (the case's row_version you read)."}, data)
        status, body = await run_in_threadpool(
            service.decide_case, case_id, data.get("decision"), expected, data.get("owner"), data.get("due_date"),
            data.get("evidence_ids"), data.get("reason"), person.actor(), data.get("command_id"))
        await run_in_threadpool(events.case_decided, status, body)
        return reply(status, body, data)

    # ---- evidence, reviews, audit ---------------------------------------------------------------
    @app.get("/evidence")
    def evidence(request: Request):
        require(request, *STAFF, COMPLIANCE)
        return {"evidence": service.list_evidence()}

    @app.get("/evidence/{evidence_id}/content")
    def evidence_content(request: Request, evidence_id: str = Path(pattern=ID)):
        require(request, REVIEWER, COMPLIANCE)
        row, data = service.evidence_content(evidence_id)
        found(row, "evidence")
        found(data, "evidence file version")
        return Response(data, media_type=row["content_type"],
                        headers={"Content-Disposition": f'attachment; filename="{row["file_name"]}"'})

    @app.get("/evidence/{evidence_id}/verify")
    def verify_evidence(request: Request, evidence_id: str = Path(pattern=ID)):
        require(request, *STAFF)
        return found(service.verify_evidence(evidence_id), "evidence")

    @app.get("/reviews")
    def reviews(request: Request):
        require(request, *STAFF)
        return {"reviews": service.list_reviews()}

    @app.get("/audit")
    def audit(request: Request, limit: int = 50):
        require(request, *STAFF)
        return {"events": service.audit(max(1, min(limit, 500)))}

    @app.get("/report/status")
    def report(request: Request):
        require(request, OPERATOR)
        connector.sync()
        return service.report_export()

    # ---- operator controls ----------------------------------------------------------------------
    @app.get("/admin/outbox")
    def outbox(request: Request):
        require(request, OPERATOR)
        return {"items": service.outbox(), "worker": worker.status()}

    @app.post("/admin/outbox/drain")
    async def drain(request: Request):
        require(request, OPERATOR)
        data = await read_input(request)
        processed = await run_in_threadpool(service.drain_outbox)
        await run_in_threadpool(events.outbox_cycle, processed)
        return reply(200, {"processed": processed}, data)

    @app.post("/admin/worker")
    async def pause_worker(request: Request):
        require(request, OPERATOR)
        data = await read_input(request)
        worker.paused = str(data.get("paused")).lower() in ("true", "1", "on", "yes")
        return reply(200, {"worker": worker.status()}, data)

    @app.get("/admin/reconcile")
    def reconcile(request: Request):
        require(request, OPERATOR)
        return service.reconcile()

    # ---- server-rendered screens: operations console, executive screens and settings -------------
    screens.install(app, service=service, settings=settings, who=who, require=require, events=events,
                    ledger=ledger, staff=STAFF, admin=admin, worker=worker, connector=connector,
                    case_readers=CASE_READERS, deciders=(COMPLIANCE,), agents=agents, same_origin=same_origin)

    return app


def same_origin(request):
    """Blocks forms posted from another web site. Scripts that send no browser headers are allowed;
    they still need a valid token, which a foreign site cannot attach."""
    hosts = {h for h in (request.headers.get("host"), request.headers.get("x-forwarded-host")) if h}
    origin = request.headers.get("origin")
    if origin:
        return urlsplit(origin).netloc in hosts
    site = request.headers.get("sec-fetch-site")
    return site is None or site in ("same-origin", "none")
