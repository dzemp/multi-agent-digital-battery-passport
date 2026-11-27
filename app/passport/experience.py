"""Browser personas and a read-only guide over real workflow screens.

Cookies select a subset of the authenticated user's roles, never grant roles. Journey navigation changes
only browser preferences; business decisions stay with the existing, authorized command endpoints.
"""

import re
from dataclasses import dataclass, replace
from urllib.parse import urlencode, urlsplit

from fastapi import Request
from fastapi.responses import RedirectResponse

from . import aftersales, exec_views
from .auth import (ADMIN, AUTHORITY, COMPLIANCE, FLEET_ROLES, OEM, OPERATOR, POLICY_APPROVER, RECYCLER, REVIEWER,
                   principal)

PERSONA_COOKIE = "dbp_persona"
JOURNEY_COOKIE = "dbp_journey"
SERIES_COOKIE = "dbp_series"
LIVE_COOKIE = "dbp_live"
SERIES = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
SERIES_READERS = (OPERATOR, REVIEWER, COMPLIANCE, AUTHORITY, OEM, RECYCLER)
PROFILES = {
    "operator": ("Operations lead", OPERATOR, "/exec"),
    "reviewer": ("Reviewer", REVIEWER, "/exec"),
    "compliance": ("Compliance Officer", COMPLIANCE, "/cases"),
    "policy": ("Policy Approver", POLICY_APPROVER, "/policy"),
    "oem": ("OEM customer", OEM, "/"),
    "recycler": ("Recycler", RECYCLER, "/"),
    "authority": ("Market surveillance authority", AUTHORITY, "/"),
    "admin": ("Administrator", ADMIN, "/settings"),
    "public": ("Public reader", None, "/"),
}
HUMAN_ROLES = frozenset(p[1] for p in PROFILES.values() if p[1])


def identity(request, settings):
    return principal(request.headers, settings.auth_mode)


def available(person):
    return {key: value for key, value in PROFILES.items()
            if person and person.has_any(HUMAN_ROLES) and (value[1] is None or value[1] in person.roles)}


def selected(request):
    # CLI callers and agent callbacks always use their token's roles, not a browser preference.
    return "all" if request.headers.get("authorization") else request.cookies.get(PERSONA_COOKIE, "all")


def effective(request, settings):
    person = identity(request, settings)
    choice = selected(request)
    if person is None or choice == "all":
        return person
    profile = available(person).get(choice)
    # A stale, revoked or forged choice must not fall back to the full set of privileges.
    roles = persona_roles(person, profile[1] if profile else None)
    return replace(person, roles=roles)


def persona_roles(person, role):
    if role == OEM:
        return frozenset({OEM} | (person.roles & FLEET_ROLES.keys()))
    return frozenset((role,)) if role else frozenset()


def safe_page(value):
    if (not isinstance(value, str) or not value.startswith("/") or value.startswith("//")
            or "\\" in value or any(ord(c) < 32 for c in value)):
        return "/"
    parts = urlsplit(value)
    if not re.fullmatch(r"/(?:exec|journey|policy|settings|oem/fleet|cases(?:/[A-Za-z0-9-]+)?|"
                        r"second-life(?:/[A-Za-z0-9-]+)?|(?:p|trail|holds|lots)/[A-Za-z0-9-]+)?", parts.path):
        return "/"
    return value


def may_open(path, roles):
    path = urlsplit(path).path
    if path == "/oem/fleet":
        return OEM in roles
    if path == "/exec" or path.startswith(("/holds/", "/trail/")) or path.startswith("/second-life"):
        return bool(roles & {OPERATOR, REVIEWER})
    if path.startswith("/cases"):
        return bool(roles & {REVIEWER, COMPLIANCE, AUTHORITY})
    if path == "/policy":
        return POLICY_APPROVER in roles
    if path == "/settings":
        return bool(roles & {ADMIN, OPERATOR})
    return True


def switcher(request, settings):
    person = identity(request, settings)
    options = available(person)
    choice = selected(request)
    profile = tour_profile(request, settings)
    return {"options": options, "choice": choice, "name": person.name if person else "",
            "label": "All assigned roles" if choice == "all" else options.get(choice, ("Selection unavailable",))[0],
            "invalid": choice != "all" and choice not in options,
            "return_to": safe_page(str(request.url.path) + ("?" + request.url.query if request.url.query else "")),
            "tour_available": profile in TOURS or (
                profile is None and choice == "all" and any(p in TOURS for p in options))}


def production_scope(request, settings, service, battery=None, lot=None):
    if not hasattr(request.state, "production_scope"):
        choice = request.query_params.get("series")
        remembered = None
        if choice is None and not request.headers.get("authorization"):
            remembered = choice = request.cookies.get(SERIES_COOKIE)
        person = effective(request, settings)
        default = None if person and person.has_any(SERIES_READERS) else "all"
        scope = exec_views.production_scope(service, default if choice is None else choice, battery, lot)
        if remembered is not None and not scope.valid:
            scope = exec_views.production_scope(service, default, battery, lot)
            request.state.series_reset = True
        request.state.production_scope = scope
    return request.state.production_scope


def live_mode(request):
    return request.query_params.get("live", request.cookies.get(LIVE_COOKIE, "1")) != "0"


@dataclass(frozen=True)
class Stop:
    key: str
    title: str
    chapter: str
    profiles: tuple
    target: str
    selector: str
    message: str
    action: str


STOPS = (
    Stop("outcome", "Start with the business outcome", "1. Protect market access", ("operator", "reviewer"),
         "exec", '[aria-label="Progress"]',
         "A passport is a release control, not a green badge. In the opening five-battery story, two publish "
         "and three wait for evidence or a human decision. Read the actual count on screen.",
         "In under two minutes: explain what is held, who must act, and why the clean batteries can proceed."),
    Stop("evidence", "Follow fragmented supplier evidence", "1. Protect market access", ("reviewer",),
         "exec", '[aria-labelledby="evidence"]',
         "Suppliers send different formats and units. Shared preparation normalizes them; signed claims "
         "establish origin and integrity, not truth. The Supplier agent escalates contradictory or adverse evidence.",
         "Point out the lithium discrepancy and the separate cobalt concern. A valid signature clears neither."),
    Stop("hold", "Resolve the lithium discrepancy", "2. Keep people accountable", ("reviewer",),
         "hold", '[aria-labelledby="compare"]',
         "The supplier claim says Mine North; transport says Mine South. The corrected claim must be compared "
         "with the record. Supplier autonomy is L3: investigate and escalate, never waive a hold.",
         "Read the documents, select the correction and submit your own reason in Your decision. Next does not "
         "submit it. A resolved hold is not a published passport."),
    Stop("review-decision", "Record your sourcing decision", "2. Keep people accountable", ("reviewer",),
         "hold", '[aria-labelledby="decision"]',
         "Only a Reviewer can resolve this sourcing discrepancy. A proposed correction is evidence to assess, "
         "not permission to waive a failed gate.",
         "Read the correction, select it and record your own reason. Next leaves the decision pending; "
         "recording a decision triggers checks, not unconditional publication."),
    Stop("case-context", "Understand the sourcing concern", "2. Keep people accountable", ("compliance",),
         "case", '[aria-labelledby="why"]',
         "Your compliance case links an adverse sourcing report to a supplier lot and its affected batteries. "
         "The Supplier agent proposes; you own the risk disposition.",
         "Read why the case opened and which batteries remain held. A verified signature does not resolve "
         "the concern."),
    Stop("case-evidence", "Read the independent evidence", "2. Keep people accountable", ("compliance",),
         "case", '[aria-labelledby="docs"]',
         "Distinguish the evidence available when the case opened from the later audit report. "
         "Uploading evidence does not accept an action plan or close the case.",
         "Open the documents. If needed, upload the generated audit JSON, then read it before citing it."),
    Stop("compliance", "Make the cobalt risk decision", "2. Keep people accountable", ("compliance",),
         "case", '[aria-labelledby="decision"]',
         "A valid cobalt claim coexists with an adverse report. Only Compliance decides this case, not a "
         "Reviewer or agent. Keeping a hold and accepting with an action plan are separate accountable decisions.",
         "Keep the hold first if needed. Read the audit report, then record an owner, due date, evidence and "
         "reason before accepting the action plan. The tour will not fill or submit these for you."),
    Stop("case-history", "Check the recorded disposition", "2. Keep people accountable", ("compliance",),
         "case", '[aria-labelledby="history"]',
         "The history records the accountable person, reason, cited evidence and action-plan commitments. "
         "New adverse evidence can reopen an accepted case.",
         "Check the actual decision history. No decision yet means it remains pending; finishing this tour "
         "changes nothing."),
    Stop("release", "Track release battery by battery", "3. Verify and issue", ("operator", "reviewer"),
         "exec", '[aria-labelledby="batteries"]',
         "Each battery has its own passport status and version. Held batteries wait for a Reviewer or "
         "Compliance decision, which Operations cannot make. A recorded decision triggers reassessment: "
         "Supplier, Carbon, independent verifier, then Issuance. No one presses a publish button.",
         "Identify which batteries are on hold and their supplier lots. After both decisions, refresh: expected "
         "five published with new versions. If one stays held, open its decision trail rather than claiming success."),
    Stop("carbon", "Explain the verified footprint", "3. Verify and issue", ("operator", "reviewer"),
         "trail-clean", '[aria-labelledby="steps"]',
         "Carbon L4 invokes a pinned, deterministic calculation and an independent arithmetic verifier. "
         "Issuance L4 can proceed only with current inputs and passing gates; neither invents a carbon value.",
         "The timeline names each task's lot or battery and source. Supplier entries assess different lots; shared "
         "lots reuse the same assessment across battery trails. Tasks are ordered by creation time and can overlap. "
         "Open a step for source records and checks, or use Expand all steps. The factors are illustrative, not an "
         "approved legal methodology or assurance opinion."),
    Stop("public", "Show the public QR experience", "4. Share only permitted fields", ("public",),
         "passport-clean", '[aria-label="Passport facts"]',
         "This is the public field projection: identity and footprint, without supplier evidence or private "
         "decisions. The QR links to this individual battery's maintained record.",
         "Your real sign-in is retained while its roles are narrowed. This is a public-view preview, not proof "
         "of an anonymous or separate-account authorization test."),
    Stop("public-link", "Use the battery's passport link", "4. Share only permitted fields", ("public",),
         "passport-clean", '[aria-label="QR code and passport link"]',
         "The QR and printable card link to this individual battery's maintained passport, not a supplier batch.",
         "Inspect the link or open the printable card. A QR code does not establish physical authenticity or safety."),
    Stop("oem-identity", "Identify your battery and passport", "4. Share only permitted fields", ("oem",),
         "passport-clean", '[aria-label="Passport facts"]',
         "Check the battery identity, passport version and actual lifecycle state before using its performance data.",
         "Read the current record. Publication is not battery safety clearance or proof of a commercial "
         "second-life offer."),
    Stop("oem", "Read OEM performance information", "4. Share only permitted fields", ("oem",),
         "passport-clean", '[aria-labelledby="sec-performance"]',
         "Performance is shared with other viewers. On your organization's issued pack, check the separate "
         "second-life decision, warranty limits and campaign feed date.",
         "Compare dated equivalent full cycles with the warranty limit, not the design specification. "
         "Missing data is not coverage; a review flag is not a claim decision."),
    Stop("oem-fleet", "Reconcile campaign exposure by holder", "4. Share only permitted fields", ("oem",),
         "fleet", '[aria-labelledby="exposure"]',
         "The fleet counts affected physical packs for your organization, not passport versions. "
         "A transfer changes the holder, not OEM entitlement.",
         "Open a holder and follow an affected pack. Check the delivery date before treating zero as no exposure."),
    Stop("recycler", "Give the recycler the relevant detail", "4. Share only permitted fields", ("recycler",),
         "passport-recycled", '[aria-labelledby="sec-composition"]',
         "The recycler also sees composition, including recycled materials and synthetic handling information. "
         "It does not inherit compliance or reviewer powers.",
         "Show recycled cobalt, lithium and nickel. Do not equate illustrative handling data with a safety clearance."),
    Stop("second-life", "Keep second-life approval human", "5. Maintain the lifecycle", ("reviewer",),
         "second-life", '[aria-labelledby="decision"]',
         "Signed OEM events record state of health and an ownership transfer; the new holder requests repurposing "
         "with a safety assessment reference. Lifecycle L4 records events, but cannot approve a second life.",
         "Read the request and assessment reference, then explicitly approve or reject with your own reason. "
         "An approval creates the linked successor; Back cannot undo a decision."),
    Stop("successor", "Follow the linked successor", "5. Maintain the lifecycle", ("oem",),
         "successor", '[aria-label="Passport facts"]',
         "Approval closes the predecessor as repurposed and creates its active successor with links both ways. "
         "The illustrative value is a hypothesis, not realized second-life revenue.",
         "Check the predecessor link and the inherited decision and estimate. "
         "If no successor exists yet, a Reviewer must "
         "complete the separate approval; this OEM tour cannot do so."),
    Stop("recycling", "Close the recycling loop", "5. Maintain the lifecycle", ("recycler",),
         "passport-recycled", '[aria-label="Passport facts"]',
         "The OEM records end of life and the authorized recycler records closure. History remains; a closed "
         "passport cannot silently accept later operational events.",
         "Check the actual lifecycle state for the recycled battery. If still active, the aftercare events "
         "have not finished."),
    Stop("policy-active", "Read the active guidance", "6. Govern and decide", ("policy",),
         "policy", '[data-tour="policy-active"]',
         "This is the currently active guidance, distinct from pending Watch recommendations and code-owned "
         "publication gates.",
         "Read the version, provenance and approval attribution before assessing a proposed change."),
    Stop("policy", "Review Regulatory Watch recommendations", "6. Govern and decide", ("policy",),
         "policy", '[data-tour="policy-candidates"]',
         "Watch is L2: cited recommendations only. A Policy Approver can activate or reject AI-drafted guidance; "
         "neither the model nor an approval changes code-owned publication gates.",
         "Read a pending candidate and its sources. Decide only if appropriate; no candidate means this path has "
         "not been exercised, not that monitoring found nothing."),
    Stop("assurance", "Show who did what, and the proof limits", "6. Govern and decide", ("reviewer", "operator"),
         "trail-held", '[aria-labelledby="integrity"]',
         "The trail separates agent proposals, human reasons and application checks. Ledger and snapshot badges "
         "report their actual verification state. AI text carries provenance, not enabled model watermarking.",
         "Explain denials, replay and quarantine evidence from the run register. Local memory cannot prove an "
         "Azure SQL ledger or real Entra isolation."),
    Stop("measures", "Return to the executive decision", "6. Govern and decide", ("operator", "reviewer"),
         "exec", '[aria-labelledby="measures"]',
         "Read the four measures: passport coverage, verified footprint per kWh, supplier completeness and "
         "illustrative second-life value. Compare the three plants without counting successors as new production.",
         "Close with observed results, unmeasured cost/latency/quality and the next validation gate. The tour is "
         "navigation, not a completion certificate; missing evidence stays missing."),
)
BY_KEY = {stop.key: stop for stop in STOPS}
TOURS = {profile: tuple(stop for stop in STOPS if profile in stop.profiles)
         for profile in PROFILES if any(profile in stop.profiles for stop in STOPS)}


def tour_profile(request, settings):
    options = available(identity(request, settings))
    choice = selected(request)
    if choice != "all":
        return choice if choice in options else None
    roles = [p for p in options if p != "public"]
    # A single-role account needs no extra selection; multi-role accounts must choose explicitly.
    return roles[0] if len(roles) == 1 else None


def journey_state(request):
    parts = request.cookies.get(JOURNEY_COOKIE, "").split("|")
    if len(parts) != 3:
        return None
    profile, key, series = parts
    if (profile in TOURS and key in {s.key for s in TOURS[profile]}
            and (SERIES.fullmatch(series) or profile == "policy" and not series)):
        return profile, key, series
    return None


def resolve(service, stop, series, oem_id=None):
    """Resolve the selected series only; never silently switch to a different run."""
    if stop.target == "policy":
        return "/policy", ""
    if stop.target == "fleet" and not oem_id:
        return None, "No OEM entitlement is configured for this sign-in."
    with service.store.read() as tx:
        packages = tx.select("Packages", series_id=series)
        if not packages:
            return None, "No package for this series. Prepare it first, then refresh."
        ids = sorted({r["battery_id"] for p in packages for r in tx.select("PackageBatteries", build_id=p["build_id"])})
        if stop.profiles == ("oem",) and oem_id:
            assigned = {p["battery_id"] for p in tx.select("OEMPhysicalPacks", oem_id=oem_id, series_id=series)}
            ids = [b for b in ids if b in assigned and (tx.get("PassportState", b) or {}).get("committed_version")]
            if stop.target == "fleet":
                return "/oem/fleet?" + urlencode({"series_id": series}), ""
            if not ids:
                return None, "No entitled issued packs are available here. Check the imported feed and series."
        states = [s for b in ids if (s := tx.get("PassportState", b))]
        lots = {lot for s in states for lot in s["lot_ids"].split(",")}
        reviews = [r for r in tx.select("Reviews") if r["lot_id"] in lots]
        cases = [c for c in tx.select("ComplianceCases") if c["lot_id"] in lots]
        requests = [r for r in tx.select("SecondLifeRequests") if r["battery_id"] in ids]
    query = "?" + urlencode({"series": series})
    if stop.target == "exec":
        return "/exec" + query, ""
    if stop.target == "hold":
        matching = sorted(reviews, key=lambda r: (r["status"] == "open", r["opened_utc"]), reverse=True)
        return (f"/holds/{matching[0]['review_id']}", "") if matching else (
            None, "No sourcing review exists in this series. Run prepare and check the Supplier result.")
    if stop.target == "case":
        matching = sorted(cases, key=lambda c: (c["status"] == "open", c["opened_utc"]), reverse=True)
        return (f"/cases/{matching[0]['case_id']}", "") if matching else (
            None, "No compliance case exists in this series. Check the adverse evidence and Supplier result.")
    if stop.target in ("second-life", "successor"):
        matching = sorted(requests, key=lambda r: r["requested_utc"], reverse=True)
        if not matching:
            return None, "No second-life request yet. Run aftercare with --wait 3600, then refresh."
        item = matching[0]
        if stop.target == "second-life":
            return f"/second-life/{item['request_id']}", ""
        if item["status"] != "approved":
            return (None, "The second-life request is not approved. Only a Reviewer can decide it; "
                    "no successor is available.")
        return f"/p/{item['successor_id']}", ""
    # Prefer the story's named records, including tags, without using records from another series.
    prefix = "B-005" if stop.target == "passport-recycled" else "B-001" if stop.target == "trail-held" else "B-003"
    battery = next((b for b in ids if b == prefix or b.startswith(prefix + "-")), None)
    if not battery and stop.profiles == ("oem",):
        battery = ids[0]
    if not battery:
        return None, f"This series has no {prefix} story battery. Choose a five-battery synthetic series."
    return f"/{'trail' if stop.target.startswith('trail') else 'p'}/{battery}", ""


def guide(request, settings, service):
    state = journey_state(request)
    if not state or state[0] != tour_profile(request, settings):
        return None
    profile, key, series = state
    stop = BY_KEY[key]
    stops = TOURS[profile]
    target, missing = resolve(service, stop, series, aftersales.organization(effective(request, settings)))
    index = stops.index(stop)
    return {"stop": stop, "series": series, "profile": profile, "label": PROFILES[profile][0],
            "index": index + 1, "total": len(stops), "target": target,
            "missing": missing, "here": bool(target and urlsplit(target).path == request.url.path),
            "previous": stops[index - 1].key if index else None,
            "next": stops[index + 1].key if index + 1 < len(stops) else None}


def cookie(response, name, value, settings):
    response.set_cookie(name, value, httponly=True, secure=settings.auth_mode == "easyauth", samesite="strict",
                        path="/")


def install(app, *, settings, service, render, problem, same_origin):
    @app.post("/personas")
    async def choose_persona(request: Request):
        person = identity(request, settings)
        if person is None:
            return problem(request, 401)
        if not same_origin(request):
            return problem(request, 403, message="Switch personas from this app, not another site.")
        async with request.form() as form:
            choice = form.get("persona")
            target = safe_page(form.get("return_to", "/"))
        options = available(person)
        if choice != "all" and choice not in options:
            return problem(request, 403, message="This persona is not assigned to your authenticated identity.")
        role = options.get(choice, (None, None))[1]
        roles = person.roles if choice == "all" else persona_roles(person, role)
        if not may_open(target, roles):
            target = "/" if choice == "all" else options[choice][2]
        state = journey_state(request)
        next_state = None
        if state and choice != state[0] and choice in TOURS and (state[2] or choice == "policy"):
            stop, series = TOURS[choice][0], state[2]
            target, _ = resolve(service, stop, series, aftersales.organization(replace(person, roles=roles)))
            target = target or "/journey"
            next_state = f"{choice}|{stop.key}|{series}"
        elif state and choice != state[0] and choice in TOURS:
            target = "/journey"
        response = RedirectResponse(target, status_code=303)
        cookie(response, PERSONA_COOKIE, choice, settings)
        if next_state:
            cookie(response, JOURNEY_COOKIE, next_state, settings)
        elif state and choice != state[0]:
            response.delete_cookie(JOURNEY_COOKIE, path="/")
        return response

    @app.get("/journey")
    def journey_page(request: Request):
        person = identity(request, settings)
        if not person or not person.has_any(HUMAN_ROLES):
            return problem(request, 401 if not person else 403, message="A human app role is needed for this guide.")
        profile = tour_profile(request, settings)
        # Discovery follows the selected role, just like the portal's series selector.
        with service.store.read() as tx:
            viewer = effective(request, settings)
            series, _ = exec_views.all_series(tx) if viewer and viewer.has_any(SERIES_READERS) else ([], [])
        state = journey_state(request)
        scope = production_scope(request, settings, service)
        current = scope.series_id if not scope.all else (
            state[2] if state and state[0] == profile == "public" else "")
        return render(request, "journey.html", title="Guided journey", stops=TOURS.get(profile, ()),
                      tour_profile=profile, tour_label=PROFILES[profile][0] if profile else "",
                      series_list=series, chosen_series=current, place={"page": "journey"})

    @app.post("/journey")
    async def navigate(request: Request):
        person = identity(request, settings)
        if not person:
            return problem(request, 401)
        if not same_origin(request):
            return problem(request, 403, message="Navigate the guide from this app, not another site.")
        async with request.form() as form:
            key, series = form.get("step"), form.get("series", "")
            profile = form.get("persona")
        if key == "exit":
            response = RedirectResponse("/journey", status_code=303)
            response.delete_cookie(JOURNEY_COOKIE, path="/")
            return response
        current = tour_profile(request, settings)
        if current not in TOURS or profile != current:
            return problem(request, 403, message="Choose a supported persona in View as and reopen its tour. "
                           "This form belongs to another or unavailable persona.")
        if key not in BY_KEY or not isinstance(series, str) or not (
                SERIES.fullmatch(series) or current == "policy" and not series):
            return problem(request, 422, message="Choose a journey stop and an imported production series.")
        stop = BY_KEY[key]
        if stop not in TOURS[current]:
            return problem(request, 403, message="This stop is not part of your selected persona's tour. "
                           "Next and Back never change your role.")
        target, _ = resolve(service, stop, series, aftersales.organization(effective(request, settings)))
        response = RedirectResponse(target or "/journey", status_code=303)
        cookie(response, JOURNEY_COOKIE, f"{current}|{key}|{series}", settings)
        if series and series != "all" and exec_views.production_scope(service, series).valid:
            cookie(response, SERIES_COOKIE, series, settings)
        return response
