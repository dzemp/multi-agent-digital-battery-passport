"""Executive screens: server-rendered HTML with optional first-party navigation enhancements.

/                operations console: passports, reviews, evidence, queue, audit   anyone; controls by role
                 (the "Connector (mock)" section: Operator or Authority)
/exec            passport readiness and the four success measures per series   Operator or Reviewer
/p/{id}, /card   passport with QR code; the caller's field policy (access.py)   anyone (anonymous: public fields)
/holds/{id}      hold and review                    Reviewer decides; Operator sees document names only
/lots/{id}       supplier lot: documents, batteries, holds   Operator, Reviewer or Compliance;
                 Reviewer and Compliance read the documents
/trail/{id}      decision trail and integrity       Operator or Reviewer
/cases, /cases/{id}  compliance cases               Compliance decides; Reviewer and Authority read
/second-life, /second-life/{id}  second-life approvals   Reviewer decides; Operator reads (JSON for API callers)
/passports/{id}/links  predecessor and successor links  anyone (JSON)
/lifecycle/quarantine  quarantined lifecycle events    Operator or Authority (JSON)
/ledger/verify   ledger check as JSON               Operator or Reviewer
/settings        Azure connections and workbook links   Admin edits; Operator sees masked values
The sidebar lists the passports for OEM, Recycler, Authority and Compliance viewers; Authority also gets the
console's parties and connector log and the quarantine list.
A person never publishes here: resolving a hold re-runs the checks; publication stays with the gates and agents.
"""

import re
import uuid
from pathlib import Path as FilePath
from urllib.parse import quote

from fastapi import HTTPException, Path, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.concurrency import run_in_threadpool

from . import access, aftersales, exec_views, experience
from .auth import ADMIN, AUTHORITY, COMPLIANCE, OEM, OPERATOR, POLICY_APPROVER, RECYCLER, REVIEWER
from .config import WORKBOOKS
from .service import CommandError
from .settings_store import SettingsAdmin

ID = r"^[A-Za-z0-9-]{1,64}$"
TEMPLATES = FilePath(__file__).with_name("templates")
ERRORS = {
    "reason_required": "Write a reason of 5 to 400 characters.",
    "evidence_required": "Choose the corrected document that resolves the concern.",
    "too_much_evidence": "Choose one document.",
    "evidence_not_found": "That document is not recorded for this lot. Reload the page.",
    "new_evidence_required": "Choose a document that was not there when the hold opened.",
    "version_conflict": "Someone changed this hold while you were reading. Reload the page and try again.",
    "review_not_open": "This hold is already resolved.",
    "review_not_found": "This hold does not exist.",
    "command_id_required": "The form expired. Reload the page and try again.",
    "command_id_reused": "The form was already used. Reload the page and try again.",
    "row_version_required": "The form expired. Reload the page and try again.",
    "bad_decision": "Choose keep the hold or accept with an action plan.",
    "owner_required": "Name the action-plan owner (2 to 100 characters).",
    "due_date_required": "Give the action-plan due date (YYYY-MM-DD).",
    "case_not_open": "This case is already decided.",
    "case_not_found": "This compliance case does not exist.",
}
SECOND_LIFE_ERRORS = {**ERRORS, "bad_decision": "Choose approve or reject.",
                      "version_conflict": "Someone changed this request while you were reading. Reload the page and "
                                          "try again.",
                      "request_not_pending": "This request is already decided.",
                      "request_not_found": "This second-life request does not exist.",
                      "state_not_allowed": "The passport is no longer awaiting a second-life decision."}
SECOND_LIFE_READERS = (REVIEWER, OPERATOR)
QUARANTINE_READERS = (OPERATOR, AUTHORITY)
PASSPORT_VIEWERS = (OEM, RECYCLER, AUTHORITY, COMPLIANCE)
CASE_ERRORS = {**ERRORS, "evidence_required": "Choose at least one document that supports the action plan.",
               "new_evidence_required": "Choose a document that was not there when the case opened.",
               "version_conflict": "Someone changed this case while you were reading. Reload the page and try again."}
TITLES = {401: "Please sign in", 403: "You do not have access to this page", 404: "Not found"}


def environment():
    return Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(default=True),
                       trim_blocks=True, lstrip_blocks=True)


def install(app, *, service, settings, who, require, events, ledger, staff, admin=None, worker=None, connector=None,
            case_readers=(), deciders=(), agents=None, same_origin=None):
    env = environment()
    prices = settings.prices
    admin = admin or SettingsAdmin.fixed("Saving is off.").start(settings)
    workbooks = [{"label": short, "title": title, "url": settings.workbooks[name]}
                 for name, _, short, title in WORKBOOKS if name in settings.workbooks]

    def scope_for(request, battery=None, lot=None):
        return experience.production_scope(request, settings, service, battery, lot)

    def record_scope(request, table, key):
        with service.store.read() as tx:
            record = tx.get(table, key) or {}
        return scope_for(request, record.get("battery_id"), record.get("lot_id"))

    def outside_scope(request, table, key):
        scope = record_scope(request, table, key)
        with service.store.read() as tx:
            row = tx.get(table, key)
        if row and (not scope.valid or not scope.includes(
                row.get("battery_id") or row.get("lot_id"), "batteries" if "battery_id" in row else "lots")):
            return problem(request, 404, message="This record is outside the selected production series. "
                           "Choose its production series before recording a decision.")
        return None

    def render(request, template, status=200, place=None, **values):
        """`place` marks a page that shows the sidebar: page name plus the batch, battery, lot or hold.
        Staff see the batch; an administrator without a staff role sees only Monitoring and Settings;
        case readers also see the compliance cases."""
        person = who(request)
        is_staff = bool(person and person.has_any(staff))
        is_admin = bool(person and person.has_any((ADMIN,)))
        is_reader = bool(person and case_readers and person.has_any(case_readers))
        is_viewer = bool(person and person.has_any(PASSPORT_VIEWERS) and not is_staff)
        is_policy = bool(person and person.has_any((POLICY_APPROVER,)))
        second_life = bool(person and person.has_any(SECOND_LIFE_READERS))
        place = place or {}
        scope = scope_for(request, place.get("battery"), place.get("lot"))
        if template != "error.html" and (not scope.valid
                            or place.get("battery") and not scope.includes(place["battery"])
                            or place.get("lot") and not scope.includes(place["lot"], "lots")):
            template, status, place = "error.html", 404, {"page": "error"}
            values = {"title": "Not in the selected production series",
                      "text": "This series is unavailable or this record is outside the selected production series. "
                              "Choose another production series or All production series above.", "login": None}
        nav = None
        if (is_staff or is_admin or is_reader or is_viewer or is_policy) and place:
            nav = exec_views.nav_view(service, scope=scope) if is_staff else {}
            if is_reader:
                nav.update(exec_views.case_nav(service, scope))
            if is_viewer:
                nav.update(passports=exec_views.viewer_passports(service, scope))
            if second_life:
                rows = exec_views.second_life_rows(service, scope)
                nav.update(second_life=rows[:6], open_second_life=sum(1 for r in rows if r["open"]))
            nav = {**nav, "staff": is_staff, "admin": is_admin, "workbooks": workbooks, "case_reader": is_reader,
                   "second_life_reader": second_life, "viewer": is_viewer,
                   "authority": bool(person.has_any((AUTHORITY,))),
                   "oem_fleet": bool(aftersales.organization(person)),
                   "policy": is_policy, "show_settings": bool(person.has_any((ADMIN, OPERATOR)))}
        guide = experience.guide(request, settings, service)
        series_reset = getattr(request.state, "series_reset", False)
        changed_series = ((series_reset or "series" in request.query_params)
                          and guide and guide["series"] != scope.series_id)
        if changed_series:
            guide = None
        if guide:
            values["refresh"] = None  # never interrupt a decision form or reset the guide's focus
        selection_target = request.url.path
        if selection_target not in ("/", "/exec", "/cases", "/second-life", "/journey"):
            selection_target = "/exec" if is_staff else "/cases" if is_reader else "/"
        page = env.get_template(template).render(
            banner=exec_views.SYNTHETIC, person=person, is_staff=is_staff, nav=nav, place=place,
            scope=scope, series_reset=series_reset,
            series_options=[{"id": s, "name": exec_views.series_label(s)} for s in scope.options]
            if person and person.has_any(experience.SERIES_READERS) else None,
            selection_target=selection_target, live_enabled=experience.live_mode(request),
            persona=experience.switcher(request, settings), guide=guide, **values)
        response = HTMLResponse(page, status_code=status)
        if scope.valid and not request.headers.get("authorization") and (
                person or experience.SERIES_COOKIE in request.cookies):
            experience.cookie(response, experience.SERIES_COOKIE, scope.series_id, settings)
        if request.query_params.get("live") in ("0", "1"):
            experience.cookie(response, experience.LIVE_COOKIE, request.query_params["live"], settings)
        if changed_series:
            response.delete_cookie(experience.JOURNEY_COOKIE, path="/")
        return response

    def problem(request, status, roles=(), message=""):
        texts = {401: "This page is for signed-in staff. Public passport pages stay open to everyone.",
                 403: message or f"This needs the {' or '.join(roles)} app role. Switch to an assigned persona "
                                 "or ask the app owner to assign it.",
                 404: message or "We could not find that page."}
        login = None
        if status == 401 and settings.auth_mode == "easyauth":
            login = "/.auth/login/aad?post_login_redirect_uri=" + quote(request.url.path, safe="/")
        return render(request, "error.html", status=status, title=TITLES.get(status, "Error"),
                      text=texts.get(status, message), login=login, place={"page": "error"})

    experience.install(app, settings=settings, service=service, render=render, problem=problem,
                       same_origin=same_origin)

    def guard(request, *roles):
        try:
            return require(request, *roles), None
        except HTTPException as error:
            return None, problem(request, error.status_code, roles)

    @app.get("/policy", response_class=HTMLResponse)
    def policy_page(request: Request, done: str = "", error: str = ""):
        person, denied = guard(request, POLICY_APPROVER)
        if denied:
            return denied
        if agents is None or not settings.agent_mode:
            return problem(request, 404, message="Policy review is available only when agent mode is enabled.")
        return render(request, "policy.html", title="Watch policy approval", view=None,
                      active=agents.active_policy(), candidates=agents.policy_candidates(),
                      done=done if done in ("activated", "rejected") else "", error=error, place={"page": "policy"})

    @app.post("/policy/{candidate_id}/activate", response_class=HTMLResponse)
    async def activate_policy_page(request: Request, candidate_id: str = Path(pattern=ID)):
        return await decide_policy_page(request, candidate_id, agents.activate_policy if agents else None, "activated")

    @app.post("/policy/{candidate_id}/reject", response_class=HTMLResponse)
    async def reject_policy_page(request: Request, candidate_id: str = Path(pattern=ID)):
        return await decide_policy_page(request, candidate_id, agents.reject_policy if agents else None, "rejected")

    async def decide_policy_page(request, candidate_id, decide, done):
        person, denied = guard(request, POLICY_APPROVER)
        if denied:
            return denied
        if agents is None or not settings.agent_mode:
            return problem(request, 404, message="Policy review is available only when agent mode is enabled.")
        async with request.form() as form:
            data = {key: value for key, value in form.items() if isinstance(value, str)}
        try:
            expected = int(data.get("expected_row_version"))
            await run_in_threadpool(decide, candidate_id, expected, data.get("reason"), person.actor())
        except (TypeError, ValueError):
            return RedirectResponse("/policy?error=row_version_required", status_code=303)
        except CommandError as exc:
            return RedirectResponse(f"/policy?error={quote(exc.body['message'])}", status_code=303)
        return RedirectResponse(f"/policy?done={done}", status_code=303)

    def public_url(request, battery_id):
        base = settings.public_base_url or str(request.base_url).rstrip("/")
        return f"{base}/p/{battery_id}"

    @app.get("/", response_class=HTMLResponse)
    def console_page(request: Request, msg: str = ""):
        person = who(request)
        scope = scope_for(request)
        is_staff = bool(person and person.has_any(staff))
        view = exec_views.console_view(
            service, worker, staff=is_staff, reviewer=bool(person and person.has_any((REVIEWER,))),
            operator=bool(person and person.has_any((OPERATOR,))), msg=msg[:400], scope=scope)
        if connector is not None and person and person.has_any((OPERATOR, AUTHORITY)):
            view["connector"] = exec_views.connector_view(connector, scope=scope)
        login = "/.auth/login/aad?post_login_redirect_uri=/" if person is None and settings.auth_mode == "easyauth" \
            else None
        return render(request, "console.html", title="Operations console" if is_staff else "Battery passports",
                      view=view, login=login, admin_only=bool(person and not is_staff and person.has_any((ADMIN,))),
                      command_ids=[str(uuid.uuid4()) for _ in range(4)], place={"page": "console"})

    @app.get("/exec", response_class=HTMLResponse)
    def exec_page(request: Request):
        _, denied = guard(request, *staff)
        if denied:
            return denied
        view = exec_views.series_view(service, prices=prices, scope=scope_for(request))
        live = experience.live_mode(request)
        return render(request, "exec.html", title="Passport readiness", view=view, live=live,
                      refresh=5 if live else None,
                      place={"page": "readiness", "series": view.get("series_id")})

    def passport_page(request, battery_id, template, scale, place=None):
        scope = scope_for(request, battery=battery_id)
        if not scope.includes(battery_id):
            return problem(request, 404, message="This battery is outside the selected production series. "
                           "Choose its production series or All production series above.")
        person = who(request)
        policy = access.project(service, battery_id, person if template == "passport.html" else None)
        view = exec_views.public_page(service, battery_id, ledger, policy) if policy else None
        if view is None:
            return problem(request, 404, message="No passport exists for this battery identifier.")
        if template == "passport.html":
            events.view_served(policy)
        url = public_url(request, battery_id)
        return render(request, template, title=f"Battery {battery_id}", view=view, url=url,
                      qr=exec_views.qr_svg(url, scale), place=place, policy=policy,
                      sections=exec_views.policy_sections(policy))

    @app.get("/p/{battery_id}", response_class=HTMLResponse)
    def public_passport_page(request: Request, battery_id: str = Path(pattern=ID)):
        return passport_page(request, battery_id, "passport.html", 4, {"page": "passport", "battery": battery_id})

    @app.get("/p/{battery_id}/card", response_class=HTMLResponse)
    def public_card(request: Request, battery_id: str = Path(pattern=ID)):
        return passport_page(request, battery_id, "card.html", 6)

    def fleet_view(request, series, holder, campaign_id):
        person = require(request, OEM)
        result = aftersales.fleet(service, person, series, holder, campaign_id)
        if result is None:
            raise HTTPException(403, "This principal has no fleet entitlement.")
        return result

    @app.get("/oem/fleet/view")
    def fleet_json(request: Request, series_id: str = None, holder_id: str = None, campaign_id: str = None):
        return fleet_view(request, series_id, holder_id, campaign_id)

    @app.get("/oem/fleet", response_class=HTMLResponse)
    def fleet_page(request: Request, series_id: str = None, holder_id: str = None, campaign_id: str = None):
        view = fleet_view(request, series_id, holder_id, campaign_id)
        # The general passport sidebar/series picker is a public catalog, not an entitled fleet list.
        return env.get_template("fleet.html").render(
            title="OEM campaign exposure", banner=exec_views.SYNTHETIC, view=view,
            person=who(request), persona=experience.switcher(request, settings), series_options=None,
            guide=experience.guide(request, settings, service))

    def show_hold(request, person, review_id, status=200, error=None, reason="", chosen=None, done=False):
        scope = record_scope(request, "Reviews", review_id)
        view = exec_views.hold_view(service, review_id, person.has_any((REVIEWER,)), scope)
        if view is None:
            return problem(request, 404, message="This hold does not exist.")
        return render(request, "hold.html", status=status, title=f"Hold {review_id}", view=view, error=error,
                      reason=reason, chosen=chosen or view.get("preselected"), command_id=str(uuid.uuid4()),
                      done=done, place={"page": "hold", "review": review_id, "lot": view["lot_id"]})

    @app.get("/holds/{review_id}", response_class=HTMLResponse)
    def hold_page(request: Request, review_id: str = Path(pattern=ID), done: str = ""):
        person, denied = guard(request, *staff)
        if denied:
            return denied
        return show_hold(request, person, review_id, done=done == "1")

    @app.post("/holds/{review_id}/resolve", response_class=HTMLResponse)
    async def hold_resolve(request: Request, review_id: str = Path(pattern=ID)):
        person, denied = guard(request, REVIEWER)
        if denied:
            return denied
        denied = outside_scope(request, "Reviews", review_id)
        if denied:
            return denied
        async with request.form() as form:
            data = {k: v for k, v in form.items() if isinstance(v, str)}
        reason, evidence_id = data.get("reason", ""), (data.get("evidence_id") or "").strip()
        try:
            expected = int(data.get("expected_row_version"))
        except (TypeError, ValueError):
            return await run_in_threadpool(show_hold, request, person, review_id, 422,
                                           ERRORS["row_version_required"], reason, evidence_id)
        status, body = await run_in_threadpool(
            service.resolve_review, review_id, expected, reason, [evidence_id] if evidence_id else [],
            person.actor(), data.get("command_id"))
        if status != 200:
            message = ERRORS.get(body.get("error"), body.get("message") or "The decision was not recorded.")
            return await run_in_threadpool(show_hold, request, person, review_id, status, message, reason,
                                           evidence_id)
        await run_in_threadpool(events.hold_resolved, review_id, status, body)
        return RedirectResponse(f"/holds/{review_id}?done=1", status_code=303)

    @app.get("/lots/{lot_id}", response_class=HTMLResponse)
    def lot_page(request: Request, lot_id: str = Path(pattern=ID)):
        person, denied = guard(request, *staff, COMPLIANCE)
        if denied:
            return denied
        view = exec_views.lot_view(service, lot_id, person.has_any((REVIEWER, COMPLIANCE)),
                                   scope_for(request, lot=lot_id))
        if view is None:
            return problem(request, 404, message="This supplier lot does not exist.")
        back = ("/exec", "passport readiness") if person.has_any(staff) else ("/cases", "compliance cases")
        return render(request, "lot.html", title=f"Lot {lot_id}", view=view, back=back,
                      can_open_cases=person.has_any(case_readers), place={"page": "lot", "lot": lot_id})

    @app.get("/cases", response_class=HTMLResponse)
    def cases_page(request: Request):
        _, denied = guard(request, *case_readers)
        if denied:
            return denied
        return render(request, "cases.html", title="Compliance cases",
                      rows=exec_views.case_rows(service, scope_for(request)),
                      labels=["Synthetic data", "Illustrative"], place={"page": "cases"})

    def show_case(request, person, case_id, status=200, error=None, form=None, done=False, msg=""):
        view = exec_views.case_view(service, case_id, person.has_any(deciders),
                                   record_scope(request, "ComplianceCases", case_id))
        if view is None:
            return problem(request, 404, message="This compliance case does not exist.")
        return render(request, "compliance.html", status=status, title=f"Compliance case {case_id}", view=view,
                      error=error, form=form or {}, command_id=str(uuid.uuid4()), done=done,
                      upload_command_id=str(uuid.uuid4()), upload_result=exec_views.console_result(msg[:400]),
                      place={"page": "case", "case": case_id, "lot": view["lot_id"]})

    @app.get("/cases/{case_id}", response_class=HTMLResponse)
    def case_page(request: Request, case_id: str = Path(pattern=ID), done: str = "", msg: str = ""):
        person, denied = guard(request, *case_readers)
        if denied:
            return denied
        return show_case(request, person, case_id, done=done == "1", msg=msg)

    @app.post("/cases/{case_id}/decide", response_class=HTMLResponse)
    async def case_decide(request: Request, case_id: str = Path(pattern=ID)):
        person, denied = guard(request, *deciders)
        if denied:
            return denied
        denied = outside_scope(request, "ComplianceCases", case_id)
        if denied:
            return denied
        async with request.form() as form:
            data = {k: v for k, v in form.items() if isinstance(v, str)}
            evidence_ids = [v for v in form.getlist("evidence_id") if isinstance(v, str) and v]
        try:
            expected = int(data.get("expected_row_version"))
        except (TypeError, ValueError):
            return await run_in_threadpool(show_case, request, person, case_id, 422, ERRORS["row_version_required"],
                                           data)
        status, body = await run_in_threadpool(
            service.decide_case, case_id, data.get("decision"), expected, data.get("owner"), data.get("due_date"),
            evidence_ids, data.get("reason"), person.actor(), data.get("command_id"))
        if status != 200:
            message = CASE_ERRORS.get(body.get("error"), body.get("message") or "The decision was not recorded.")
            return await run_in_threadpool(show_case, request, person, case_id, status, message, data)
        await run_in_threadpool(events.case_decided, status, body)
        return RedirectResponse(f"/cases/{case_id}?done=1", status_code=303)

    # ---- second life: the Reviewer decides; JSON for API callers, HTML for browsers ----------------------
    def wants_html(request):
        return "text/html" in request.headers.get("accept", "")

    @app.get("/second-life")
    def second_life_list(request: Request):
        if not wants_html(request):
            require(request, *SECOND_LIFE_READERS)
            return {"requests": service.list_second_life(), "labels": ["synthetic", "illustrative"]}
        _, denied = guard(request, *SECOND_LIFE_READERS)
        if denied:
            return denied
        return render(request, "second_life.html", title="Second-life approvals",
                      rows=exec_views.second_life_rows(service, scope_for(request)), view=None,
                      labels=["Synthetic data", "Illustrative value"], place={"page": "second_life"})

    def show_request(request, person, request_id, status=200, error=None, form=None, done=False):
        record_scope(request, "SecondLifeRequests", request_id)
        view = exec_views.second_life_view(service, request_id, person.has_any((REVIEWER,)))
        if view is None:
            return problem(request, 404, message="This second-life request does not exist.")
        return render(request, "second_life.html", status=status, title=f"Second-life request {request_id}",
                      view=view, rows=None, error=error, form=form or {}, command_id=str(uuid.uuid4()), done=done,
                      place={"page": "second_life_request", "request": request_id, "battery": view["battery_id"]})

    @app.get("/second-life/{request_id}")
    def second_life_request(request: Request, request_id: str = Path(pattern=ID), done: str = ""):
        if not wants_html(request):
            require(request, *SECOND_LIFE_READERS)
            detail = service.second_life_detail(request_id)
            if detail is None:
                raise HTTPException(404, "No such second-life request.")
            return detail
        person, denied = guard(request, *SECOND_LIFE_READERS)
        if denied:
            return denied
        return show_request(request, person, request_id, done=done == "1")

    @app.post("/second-life/{request_id}/decision")
    async def second_life_decision(request: Request, request_id: str = Path(pattern=ID)):
        is_json = request.headers.get("content-type", "").startswith("application/json")
        if is_json:
            person = require(request, REVIEWER)
            try:
                data = await request.json()
            except ValueError:
                raise HTTPException(400, "Body is not valid JSON.") from None
            data = data if isinstance(data, dict) else {}
        else:
            person, denied = guard(request, REVIEWER)
            if denied:
                return denied
            denied = outside_scope(request, "SecondLifeRequests", request_id)
            if denied:
                return denied
            async with request.form() as form:
                data = {k: v for k, v in form.items() if isinstance(v, str)}
        expected = data.get("expected_row_version")
        if not is_json or isinstance(expected, str):
            try:
                expected = int(expected)
            except (TypeError, ValueError):
                expected = None
        if type(expected) is not int:
            if is_json:
                return JSONResponse({"error": "row_version_required",
                                     "message": "Send expected_row_version (the request's row_version you read)."},
                                    422)
            return await run_in_threadpool(show_request, request, person, request_id, 422,
                                           ERRORS["row_version_required"], data)
        status, body = await run_in_threadpool(
            service.decide_second_life, request_id, data.get("decision"), expected, data.get("reason"),
            person.actor(), data.get("command_id"))
        await run_in_threadpool(events.second_life_decided, status, body)
        if is_json:
            return JSONResponse(body, status)
        if status != 200:
            message = SECOND_LIFE_ERRORS.get(body.get("error"), body.get("message") or "The decision was not "
                                                                                        "recorded.")
            return await run_in_threadpool(show_request, request, person, request_id, status, message, data)
        return RedirectResponse(f"/second-life/{request_id}?done=1", status_code=303)

    @app.get("/passports/{battery_id}/links")
    def passport_links(battery_id: str = Path(pattern=ID)):
        links = service.passport_links(battery_id)
        if links is None:
            raise HTTPException(404, "No such passport.")
        return links

    @app.get("/lifecycle/quarantine")
    def lifecycle_quarantine(request: Request):
        require(request, *QUARANTINE_READERS)
        scope = scope_for(request) if wants_html(request) else None
        return {"events": [e for e in service.quarantined_events()
                           if scope is None or scope.includes(e["battery_id"])], "labels": ["synthetic"]}

    @app.get("/trail/{battery_id}", response_class=HTMLResponse)
    def trail_page(request: Request, battery_id: str = Path(pattern=ID)):
        _, denied = guard(request, *staff)
        if denied:
            return denied
        scope_for(request, battery=battery_id)
        view = exec_views.trail_view(service, battery_id, prices, ledger)
        if view is None:
            return problem(request, 404, message="No battery with this identifier has been imported.")
        if view["integrity"]:
            events.ledger_checked(view["integrity"]["ledger"], battery_id)
        return render(request, "trail.html", title=f"Decision trail {battery_id}", view=view,
                      place={"page": "trail", "battery": battery_id})

    @app.get("/ledger/verify")
    def ledger_verify(request: Request, battery: str = ""):
        require(request, *staff)
        version = None
        if battery:
            if not re.fullmatch(ID, battery):
                raise HTTPException(422, "battery must be a battery identifier.")
            passport = service.public_passport(battery)
            if passport is None:
                raise HTTPException(404, "No such passport.")
            version = passport["passport_version"] or None
        result = ledger.check(battery or None, version)
        events.ledger_checked(result, battery or None)
        return {**result, "battery_id": battery or None, "version": version}

    def settings_page(request, person, status=200, values=None, errors=None, form_error=None, results=None,
                      saved=False):
        view = admin.view(settings, values, errors)
        editable = person.has_any((ADMIN,)) and not view["save_off"]
        if not person.has_any((ADMIN,)):
            state = ("View only", "info")
        elif view["save_off"]:
            state = ("Saving is off", "warn")
        elif view["pending"]:
            state = ("Restart required", "warn")
        else:
            state = ("Up to date", "good")
        return render(request, "settings.html", status=status, title="Settings", view=view, editable=editable,
                      state=state, form_error=form_error, results=results, saved=saved,
                      field_errors=bool(errors), place={"page": "settings"})

    @app.get("/settings", response_class=HTMLResponse)
    def settings_get(request: Request, saved: str = ""):
        person, denied = guard(request, ADMIN, OPERATOR)
        if denied:
            return denied
        return settings_page(request, person, saved=saved == "1")

    @app.post("/settings", response_class=HTMLResponse)
    async def settings_post(request: Request):
        person, denied = guard(request, ADMIN)
        if denied:
            return denied
        async with request.form() as form:
            data = {k: v for k, v in form.items() if isinstance(v, str)}
        action = data.get("action", "save")
        if admin.file is None:
            return settings_page(request, person, 409)
        if action == "reset":
            await run_in_threadpool(admin.reset, person.actor())
            return RedirectResponse("/settings?saved=1", status_code=303)
        if action not in ("save", "test"):
            return settings_page(request, person, 422, form_error="Unknown action. Reload the page and try again.")
        values = admin.submitted(data)
        errors, form_error = admin.validate(values)
        if errors or form_error:
            return settings_page(request, person, 422, values, errors, form_error)
        results = None
        if action == "test" or not data.get("skip_test"):
            results = await run_in_threadpool(admin.tester, values, settings.client_id)
        if action == "test":
            return settings_page(request, person, 200, values, results=results)
        if results and not all(r["ok"] for r in results):
            return settings_page(request, person, 422, values, results=results,
                                 form_error="Not saved: a connection test failed. Correct the value, or tick "
                                            "\"Save without testing\" if the service is not reachable from here yet.")
        await run_in_threadpool(admin.save, values, person.actor())
        return RedirectResponse("/settings?saved=1", status_code=303)
