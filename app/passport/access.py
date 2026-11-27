"""Who sees which passport fields: one field policy for the JSON view and the passport page.

Roles come only from the signed-in principal (Easy Auth token or the local identity headers). Query parameters,
other headers and body fields never widen a view. An MVP choice of field groups, not a legal access classification;
the data is synthetic and footprints and values are illustrative. Evidence appears as references only (ID, kind,
lot and SHA-256); file content stays behind GET /evidence/{id}/content (Reviewer or Compliance).
"""

from . import aftersales, domain, lifecycle, provenance
from .auth import ADMIN, AUTHORITY, COMPLIANCE, CONNECTOR, FLEET_ROLES, OEM, OPERATOR, RECYCLER, REVIEWER

STAFF_GROUPS = ("identity", "footprint", "performance", "composition", "calculation", "supply_chain", "due_diligence",
          "ledger", "decisions", "evidence")
OEM_GROUPS = ("second_life", "warranty", "recall")
GROUPS = (*STAFF_GROUPS, *OEM_GROUPS)
PUBLIC = ("identity", "footprint")
FIELD_POLICY = {
    None: PUBLIC,
    ADMIN: PUBLIC,
    CONNECTOR: PUBLIC,
    OEM: PUBLIC + ("performance", *OEM_GROUPS),
    RECYCLER: PUBLIC + ("performance", "composition"),
    AUTHORITY: tuple(g for g in STAFF_GROUPS if g != "evidence"),
    COMPLIANCE: PUBLIC + ("due_diligence", "decisions", "evidence"),
    OPERATOR: STAFF_GROUPS,
    REVIEWER: STAFF_GROUPS,
}
# Which evidence references a role may list: every file of the battery's lots, or only compliance-case files.
EVIDENCE_SCOPE = {OPERATOR: "all", REVIEWER: "all", COMPLIANCE: "compliance_case"}
ROLE_CODES = {OPERATOR: "operator", REVIEWER: "reviewer", ADMIN: "admin", CONNECTOR: "connector",
              AUTHORITY: "authority", COMPLIANCE: "compliance", OEM: "oem", RECYCLER: "recycler"}
GROUP_CODES = {"identity": "id", "footprint": "fp", "performance": "perf", "composition": "comp",
               "calculation": "calc", "supply_chain": "sc", "due_diligence": "dd", "ledger": "led",
               "decisions": "dec", "evidence": "ev", "second_life": "sl", "warranty": "war", "recall": "rc"}
TITLES = {"identity": "Identity and status", "footprint": "Carbon footprint and recycled content",
          "performance": "Performance and holder", "composition": "Composition, dismantling and safety",
          "calculation": "Calculation method and factors", "supply_chain": "Supply chain",
          "due_diligence": "Due diligence", "ledger": "Ledger records", "decisions": "Decisions",
          "evidence": "Evidence references", "second_life": "Second-life decision",
          "warranty": "Warranty and service review", "recall": "Recalls and service campaigns"}
LABELS = ["synthetic", "illustrative"]
# Synthetic dismantling and safety information per chemistry (not a manufacturer's instructions).
DISMANTLING = {"NMC811": {"dismantling": "Discharge to 0 % state of charge, remove the pack cover, disconnect the "
                                         "busbars, then separate the modules (synthetic instruction)",
                          "safety": "Lithium-ion NMC cells: avoid puncture and heat; store discharged and dry "
                                    "(synthetic instruction)",
                          "label": "synthetic"}}


def grant(person):
    """(field groups in policy order, evidence scope or None) for a principal; anonymous gets the public view."""
    roles = [r for r in (person.roles if person else ()) if r in FIELD_POLICY]
    allowed = set(PUBLIC).union(*(FIELD_POLICY[r] for r in roles))
    if not aftersales.organization(person):
        allowed.difference_update(("warranty", "recall"))
        if person and person.has_any(FLEET_ROLES):
            allowed.discard("second_life")
    scopes = {EVIDENCE_SCOPE[r] for r in roles if r in EVIDENCE_SCOPE}
    scope = "all" if "all" in scopes else ("compliance_case" if scopes else None)
    groups = tuple(g for g in GROUPS if g in allowed and (g != "evidence" or scope))
    return groups, scope


def role_group(person):
    """A coded role group for telemetry: never a person's name or ID."""
    if person is None:
        return "public"
    codes = sorted(ROLE_CODES[r] for r in person.roles if r in ROLE_CODES)
    return "-".join(codes) if codes else "no_role"


def field_set(groups, scope):
    codes = [GROUP_CODES[g] + ("c" if g == "evidence" and scope == "compliance_case" else "") for g in groups]
    return "-".join(codes)


def _number(value):
    return None if value is None else float(value)


def project(service, battery_id, person):
    """The passport as the principal may see it, or None for an unknown battery."""
    groups, scope = grant(person)
    party = aftersales.organization(person)
    with service.store.read() as tx:
        state = tx.get("PassportState", battery_id)
        if not state:
            return None
        version, receipt = service._views(tx, state)
        public = domain.public_view(state, version, receipt, service._links(tx, battery_id))
        package_row = service.package_row(tx, state)
        inputs = service.inputs(tx, package_row)
        lots = inputs["lots"]
        lot_ids = sorted(lots)
        data = {"identity": _identity(public, state)}
        data["footprint"] = _footprint(public, inputs)
        if "performance" in groups:
            life = service.lifecycle_view(tx, battery_id)
            data["performance"] = _performance(life, package_row)
        if "second_life" in groups:
            pack = aftersales.entitled_pack(tx, service, state, party)
            if public["passport_status"] == "committed" and pack:
                receipt = aftersales.latest_receipt(tx, pack["series_id"])
                now = service.clock()
                data["second_life"] = _second_life(service, tx, battery_id, life)
                data["warranty"] = aftersales.warranty(tx, pack, package_row, receipt, now)
                data["recall"] = aftersales.recall(tx, pack, receipt, now)
            else:
                groups = tuple(g for g in groups if g not in ("warranty", "recall"))
                # Release 1 compatibility for a base-only OEM. A qualifier never falls back to this policy.
                origin = (life["inherited_from"] or {}).get("battery_id") or battery_id
                original = state if origin == battery_id else tx.get("PassportState", origin)
                if (not person.has_any(FLEET_ROLES) and public["passport_status"] == "committed" and original
                        and (original["holder_id"] == domain.MANUFACTURER or tx.select(
                            "LifecycleEvents", battery_id=origin, issuer=domain.MANUFACTURER, result="recorded"))):
                    data["second_life"] = _second_life(service, tx, battery_id, life)
                else:
                    groups = tuple(g for g in groups if g != "second_life")
        if "composition" in groups:
            data["composition"] = _composition(inputs, state)
        if "calculation" in groups:
            data["calculation"] = _calculation(service, inputs)
        if "supply_chain" in groups:
            data["supply_chain"] = {"lots": [{"lot_id": k, "supplier_id": lots[k]["supplier_id"],
                                              "material": lots[k]["material"], "region": lots[k]["region"],
                                              "origin_site": lots[k]["origin_site"]} for k in lot_ids]}
        cases = [c for k in lot_ids for c in tx.select("ComplianceCases", lot_id=k)]
        if "due_diligence" in groups:
            data["due_diligence"] = _due_diligence(service, tx, lots, cases)
        if "ledger" in groups:
            data["ledger"] = _ledger(tx, battery_id)
        if "decisions" in groups:
            data["decisions"] = _decisions(tx, lot_ids, cases, battery_id)
        if "evidence" in groups:
            data["evidence"] = _evidence(tx, lot_ids, cases, scope)
    view = {"battery_id": battery_id, "field_groups": list(groups), "evidence_scope": scope,
            "role_group": role_group(person), "field_set": field_set(groups, scope), "labels": LABELS}
    if party and public["passport_status"] == "committed" and "recall" not in groups:
        # An unassigned or other organization's pack looks exactly like a pack without a usable feed.
        view["oem_data_status"] = "unavailable"
    return {**view, **{g: data[g] for g in groups}}


def _identity(public, state):
    return {"battery_id": public["battery_id"], "model_id": public["model_id"], "chemistry": public["chemistry"],
            "rated_kwh": _number(public["rated_kwh"]), "passport_status": public["passport_status"],
            "passport_version": public["passport_version"], "committed_utc": public["committed_utc"],
            "lifecycle_status": public["lifecycle_status"],
            "lifecycle_status_label": lifecycle.status_label(public["lifecycle_status"]), "closed": public["closed"],
            "links": public["links"]}


def _footprint(public, inputs):
    shares = domain.recycled_shares(inputs["composition"], inputs["lots"])
    return {"kg_co2e_per_kwh": _number(public["footprint_per_kwh"]), "label": "illustrative",
            "recycled_content_pct": {"cobalt": shares.get("recycled_co_pct"),
                                     "lithium": shares.get("recycled_li_pct"),
                                     "nickel": shares.get("recycled_ni_pct")}}


def _performance(life, row):
    readings = life["soh_readings"]
    return {"holder_id": life["holder_id"], "rated_capacity_ah": _number(row.get("rated_capacity_ah")),
            "expected_cycles": row.get("expected_cycles"),
            "latest_soh_pct": readings[-1]["soh_pct"] if readings else None,
            "soh_history": [{"soh_pct": r["soh_pct"], "measured_utc": r["measured_utc"], "source": r["source"]}
                            for r in readings],
            "inherited_from": (life["inherited_from"] or {}).get("battery_id")}


def _second_life(service, tx, battery_id, life):
    """Only the OEM-facing facts, never the underlying assessment, persons or decision notes."""
    requests = tx.select("SecondLifeRequests", battery_id=battery_id)
    inherited = None
    if not requests and life["inherited_from"]:
        predecessor = life["inherited_from"]["battery_id"]
        requests = tx.select("SecondLifeRequests", battery_id=predecessor, successor_id=battery_id, status="approved")
        if requests:
            inherited = predecessor
    request = max(requests, key=lambda r: (r["requested_utc"], int(r["request_id"].split("-")[1])), default=None)
    readings = life["soh_readings"]
    if request:
        # A later measurement must not change the health or date shown for an earlier decision.
        event = service.recorded_event(tx, request["message_id"])
        readings = service.soh_readings(tx, request["battery_id"], before_version=event["passport_version"])
    reading = readings[-1] if readings else None
    soh = request["soh_pct"] if request else (reading["soh_pct"] if reading else None)
    minimum = lifecycle.SECOND_LIFE["minimum_soh_pct"]
    return {"soh_pct": soh, "measured_utc": reading["measured_utc"] if reading else None,
            "minimum_soh_pct": minimum, "meets_health_screen": None if soh is None else soh >= minimum,
            "inherited_from": inherited, "lifecycle_status": life["status"],
            "request_status": request["status"] if request else None,
            "safety_reference_supplied": bool(request["safety_assessment_ref"]) if request else None,
            "decided_utc": request["decided_utc"] if request else None,
            "illustrative_value_eur": _number(request["illustrative_value_eur"]) if request else None,
            "eur_per_rated_kwh": lifecycle.SECOND_LIFE["value_eur_per_rated_kwh"] if request else None,
            "value_label": request["value_label"] if request else None}


def _composition(inputs, state):
    masses = {}
    for part in inputs["composition"]:
        masses[part["material"]] = masses.get(part["material"], 0.0) + float(part["mass_kg"])
    return {"materials": [{"material": m, "mass_kg": round(kg, 3)} for m, kg in sorted(masses.items())],
            **DISMANTLING.get(state["chemistry"], {"dismantling": "not provided", "safety": "not provided",
                                                   "label": "synthetic"})}


def _calculation(service, inputs):
    batch, result = inputs["batch"] or {}, service.footprint(inputs)
    return {"method_version": batch.get("method_version"), "factor_set_version": batch.get("factor_set_version"),
            "allocation_rule": batch.get("allocation_rule"), "batch_id": batch.get("batch_id"),
            "input_hash": inputs["battery"]["input_hash"],
            "grid_factor_id": result["grid_factor_id"] if result else None,
            "scope": result["scope"] if result else None, "assurance": result["assurance"] if result else None,
            "label": "illustrative"}


def _due_diligence(service, tx, lots, cases):
    claims, revoked = service._claims(tx)
    case_of = {c["lot_id"]: c for c in cases}
    rows = []
    for lot_id in sorted(k for k in lots if lots[k]["material"] in domain.CRITICAL):
        reviews = tx.select("Reviews", lot_id=lot_id)
        case = case_of.get(lot_id)
        rows.append({"lot_id": lot_id, "material": lots[lot_id]["material"],
                     "claim_status": domain.claim_status(lots[lot_id], claims, revoked),
                     "sourcing_review": reviews[-1]["status"] if reviews else "none",
                     "case_id": case["case_id"] if case else None, "case_state": case["status"] if case else "none"})
    return {"critical_lots": rows}


def _ledger(tx, battery_id):
    receipts = {r["version"] for r in tx.select("SnapshotReceipts", battery_id=battery_id)}
    return {"versions": [{"version": v["version"], "committed_utc": v["committed_utc"], "decision": v["decision"],
                          "snapshot_sha256": v["snapshot_sha256"],
                          "snapshot_state": "written" if v["version"] in receipts else "pending"}
                         for v in tx.select("PassportVersions", battery_id=battery_id)],
            "lifecycle_events": [{"event_type": e["event_type"], "issuer": e["issuer"], "result": e["result"],
                                  "reason_code": e["reason_code"], "passport_version": e["passport_version"],
                                  "recorded_utc": e["recorded_utc"]}
                                 for e in tx.select("LifecycleEvents", battery_id=battery_id)]}


def _review_origin(tx, review):
    """Who wrote the opening reason. AI text carries its provenance mark; a person's resolution is separate."""
    mark = provenance.mark(tx.get("AiContentProvenance", review["provenance_id"])) if review["provenance_id"] else None
    opened_by = review["opened_by"] or ""
    role = "agent:supplier" if opened_by.startswith("agent:") else opened_by if opened_by.startswith("system:") \
        else "reviewer"
    return {"opened_by_role": role,
            "reason_origin": review["reason_origin"] or ("ai" if mark else "person"), "reason_provenance": mark}


def _decisions(tx, lot_ids, cases, battery_id):
    reviews = [r for k in lot_ids for r in tx.select("Reviews", lot_id=k)]
    return {"sourcing_reviews": [{"review_id": r["review_id"], "lot_id": r["lot_id"], "status": r["status"],
                                  **_review_origin(tx, r), "reason": r["reason"],
                                  "resolved_by_role": "reviewer" if r["resolved_utc"] else None,
                                  "resolution_reason": r["resolution_reason"], "resolved_utc": r["resolved_utc"]}
                                 for r in reviews],
            "compliance": [{"case_id": d["case_id"], "lot_id": d["lot_id"], "decision": d["decision"],
                            "decided_by_role": "compliance", "reason": d["reason"], "due_date": d["due_date"],
                            "decided_utc": d["decided_utc"]}
                           for c in cases for d in tx.select("ComplianceDecisions", case_id=c["case_id"])],
            "second_life": [{"request_id": r["request_id"], "status": r["status"], "decided_by_role": "reviewer",
                             "reason": r["decision_reason"], "decided_utc": r["decided_utc"]}
                            for r in tx.select("SecondLifeRequests", battery_id=battery_id)]}


def _evidence(tx, lot_ids, cases, scope):
    rows = [r for k in lot_ids for r in tx.select("Evidence", lot_id=k)]
    if scope == "compliance_case":
        cited = {e for c in cases for e in domain.split_ids(c["opening_evidence"])}
        cited |= {e for c in cases for d in tx.select("ComplianceDecisions", case_id=c["case_id"])
                  for e in domain.split_ids(d["evidence_ids"])}
        rows = [r for r in rows if r["evidence_id"] in cited]
    return {"scope": scope, "references": [{"evidence_id": r["evidence_id"], "kind": r["kind"], "lot_id": r["lot_id"],
                                            "sha256": r["sha256"]} for r in rows]}
