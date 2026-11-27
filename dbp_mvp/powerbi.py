"""The optional Power BI semantic model passport-measures (Direct Lake on the reporting lakehouse's Delta tables
success_measures, measures_by_plant, supplier_completeness, passport_status_current and lot_claim_status_current,
reached through its SQL analytics endpoint) and the six-page PBIR report passport-measures bound to it by semantic
model ID (overview, readiness, suppliers and cases, footprint, a finance page on readiness and value, and trust and
gaps). Neither has been deployed to a tenant yet, and the DAX measures have not been evaluated by an engine.

People who open the model need a Power BI Pro or Premium Per User licence on an F2 capacity, and it cannot be queried
while the capacity is paused.
"""

import re

from . import MvpError, fabric

MODEL = "passport-measures"
FOLDER = fabric.HERE / "powerbi" / MODEL
REPORT = "passport-measures"
REPORT_FOLDER = fabric.HERE / "powerbi" / "passport-measures-report"
HOST = re.compile(r"[a-z0-9.-]+\.datawarehouse\.fabric\.microsoft\.com", re.IGNORECASE)
NOTE = ("  Before first use, open the model's settings in the Fabric portal; if it reports a data source credential "
        "problem, sign in there (OAuth2) and refresh it once. Viewers need a Power BI Pro or Premium Per User "
        "licence on F2, and the model cannot be queried while the capacity is paused.")


def _parts(folder, values, required):
    parts = []
    for path in sorted(p for p in folder.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        text = path.read_text(encoding="utf-8")
        for name, value in values.items():
            text = text.replace("{{" + name + "}}", value)
        if "{{" in text:
            raise MvpError(f"{path.relative_to(fabric.ROOT).as_posix()} has a placeholder that was not filled in.")
        parts.append(fabric.part(path.relative_to(folder).as_posix(), text.encode("utf-8")))
    if not any(item["path"] == required for item in parts):
        raise MvpError(f"{folder.relative_to(fabric.ROOT).as_posix()}/{required} is missing.")
    return parts


def definition(host, endpoint_id):
    """The TMDL files of fabric/powerbi/passport-measures with the SQL analytics endpoint filled in."""
    if not isinstance(host, str) or not HOST.fullmatch(host):
        raise MvpError(f"Unexpected SQL analytics endpoint address: {host!r}")
    endpoint_id = fabric.guid(endpoint_id, "SQL analytics endpoint ID")
    values = {"sql_endpoint_host": host, "sql_endpoint_id": endpoint_id}
    return {"format": "TMDL", "parts": _parts(FOLDER, values, "definition.pbism")}


def report_definition(model_id):
    """The PBIR files of fabric/powerbi/passport-measures-report, bound to the semantic model by its ID."""
    model_id = fabric.guid(model_id, "semantic model ID")
    return {"format": "PBIR", "parts": _parts(REPORT_FOLDER, {"semantic_model_id": model_id}, "definition.pbir")}


def publish(client, workspace, host, endpoint_id, out=print):
    """Create the semantic model, then the report on it, or update either when its definition changed. Returns
    (model ID, report ID)."""
    model = fabric.ensure_item(client, workspace, "semanticModels", MODEL, definition(host, endpoint_id))
    out(f"  Power BI semantic model {MODEL} is up to date ({model}).")
    report = fabric.ensure_item(client, workspace, "reports", REPORT, report_definition(model))
    out(f"  Power BI report {REPORT} is up to date ({report}); synthetic data, illustrative values.")
    out(NOTE)
    return model, report
