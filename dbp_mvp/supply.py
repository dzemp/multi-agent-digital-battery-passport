"""Synthetic supply data through Fabric into the passport app: deploy stages B5 and B6, and the fabric, report and
powerbi commands.

B5 prepares the synthetic series, uploads its supplier deliveries to the ingest lakehouse, runs the supply_flow
pipeline and checks that Fabric's gold package matches the local one (data files byte for byte, the manifest apart from
its time fields). When Fabric is off or the run fails, the local package is used and the reason is printed. B6 copies
the package to the app's Blob intake, imports it and uploads the series' evidence documents. Synthetic data only.
"""

import hashlib
import json
import time
from pathlib import Path

from data import simulator
from data.package import package_errors, read_folder
from data.factors import load_factors
from data.parties import registry
from data.pipeline import code_hash
from data.pipeline.deliveries import DELIVERY_MANIFEST, parse_delivery_set
from data.pipeline.flows import EXPORT_FILE, EXPORT_ID, LOT_CLAIMS_FILE, TABLES, make_export, measure_tables

from . import MvpError, data_plane, fabric, powerbi, smoke
from .config import MARKER, OUTPUT, ROOT
from .state import files_fingerprint

SQL_ENDPOINT_LIMIT = 300
SQL_ENDPOINT_POLL = 15
VIEWS = fabric.HERE / "sql" / "views.sql"
REPORTING = "reporting"
INTAKE = "intake"
MANIFEST = "manifest.json"
EXPORT_NAMES = (EXPORT_FILE, LOT_CLAIMS_FILE, MANIFEST)  # manifest last
MANIFEST_TIME_FIELDS = ("completed_utc",)
RESET_FILES = {"ingest": ("deliveries",), "gold": ("packages",), REPORTING: ("exports",)}
# A Fabric failure, or an answer of an unexpected shape, falls back to the local package instead of stopping B5.
FALLBACK = (MvpError, ValueError, KeyError, TypeError)


def _json(text):
    try:
        body = json.loads(text) if text else {}
    except ValueError:
        body = {}
    return body if isinstance(body, dict) else {}


def _manifest_last(files):
    return sorted(files, key=lambda name: (name in (MANIFEST, DELIVERY_MANIFEST), name))


def capacity(config, outputs):
    """The Fabric capacity's resource ID; the fabric, report and powerbi commands need one."""
    capacity_id = outputs.get("fabricCapacityId") or ""
    if not config.deploy_fabric or not capacity_id:
        raise MvpError("Fabric is not deployed. Set deploy_fabric = true in config/mvp.toml, run "
                       "python -m dbp_mvp bootstrap --from A1 on your laptop, then deploy again on the VM.")
    return capacity_id


def prepare(config, new_build=False, min_sequence=None, output=OUTPUT, tag=None):
    try:
        return simulator.prepare(config.extra_batteries, tag, output, new_build=new_build, min_sequence=min_sequence)
    except ValueError as error:
        raise MvpError(f"The synthetic data could not be prepared: {error}") from error


def sources():
    """Fingerprints of the generator, the pipeline rules and the Fabric item sources."""
    return {"data": files_fingerprint(ROOT / "data"), "fabric": files_fingerprint(fabric.HERE)}


def seed_inputs(state, output=OUTPUT):
    """What B6 sends to the app for a prepared series: its build, manifest, evidence documents and signed claims."""
    paths = simulator.build_paths(output, state)
    return {"build": state["build_id"], "manifest": state["manifest_sha256"],
            "evidence": {name: hashlib.sha256((paths["evidence"] / name).read_bytes()).hexdigest()
                         for name, _, _ in simulator.evidence_uploads(state["tag"], state["batteries"])},
            "registry": hashlib.sha256(paths["registry"].read_bytes()).hexdigest(),
            "messages": {message_id: hashlib.sha256((paths["messages"] / f"{message_id}.json").read_bytes())
                         .hexdigest() for message_id in simulator.opening_messages(state["tag"], state["batteries"])}}


# ---- Fabric ------------------------------------------------------------------------------------------------------

def check(config, az, outputs, send=fabric.send, sleep=None, out=print):
    """Read-only: the capacity, its visibility in Fabric and the workspace name. Nothing is resumed or created."""
    capacity_id = capacity(config, outputs)
    name = config.names.fabric_workspace
    out(f"Fabric capacity {capacity_id.rsplit('/', 1)[-1]}: {fabric.capacity_state(az, capacity_id)} (paused "
        "between runs; every Fabric command resumes it and pauses it again).")
    client = fabric.FabricClient(az, send=send, sleep=sleep)
    out(f"  Visible to you in Fabric as capacity {fabric.capacity_guid(client, capacity_id)}.")
    workspace = fabric.existing_workspace(client, name)
    if workspace is None:
        out(f"  Workspace {name}: not created yet; python -m dbp_mvp fabric run supply (or deploy B5) creates it.")
    else:
        out(f"  Workspace {name}: {workspace} (created by this MVP).")
    out("  The tenant settings for creating Fabric items are confirmed by the first fabric run supply; the region "
        "is the capacity's, which the infrastructure created.")
    return {"capacity_id": capacity_id, "workspace_id": workspace}


def deploy_items(client, name, capacity_id, out=print, flows=("supply_flow", "status_flow", "reset_flow"),
                 reset_aftersales=False):
    """The workspace, lakehouses, notebooks and pipelines. Unchanged definitions are not sent again."""
    workspace = fabric.ensure_workspace(client, name, fabric.capacity_guid(client, capacity_id), out)
    strict = "aftersales_flow" in flows
    lakehouses = {lakehouse: fabric.ensure_item(client, workspace, "lakehouses", lakehouse, require_marker=strict)
                  for lakehouse in fabric.LAKEHOUSES}
    settings = {"workspace_id": workspace, "lakehouses": lakehouses, "registry": registry(), "code_hash": code_hash(),
                "factors": load_factors()}
    reset_settings = {**settings, "reset_aftersales": True} if reset_aftersales else settings
    notebooks = {}
    for flow in flows:
        for step in fabric.PIPELINES[flow]:
            definition = fabric.notebook_definition(step, reset_settings if step == "reset_tables" else settings)
            notebooks[step] = fabric.ensure_item(client, workspace, "notebooks", step, definition,
                                                 require_marker=strict)
    pipelines = {pipeline: fabric.ensure_item(client, workspace, "dataPipelines", pipeline,
                                              fabric.pipeline_definition(pipeline, workspace, notebooks),
                                              require_marker=strict)
                 for pipeline in flows}
    return {"workspace_id": workspace, "lakehouses": lakehouses, "notebooks": notebooks, "pipelines": pipelines}


def clear_fabric_data(config, az, outputs, send=fabric.send, sleep=None, out=print):
    """Clear only MVP-owned Fabric data files and current table rows, keeping workspace items and capacity."""
    capacity_id = outputs.get("fabricCapacityId") or ""
    if not capacity_id:
        raise MvpError("The deployment has no Fabric capacity output; refusing to clear an unverified workspace.")
    client = fabric.FabricClient(az, send=send, sleep=sleep)
    workspace = fabric.existing_workspace(client, config.names.fabric_workspace)
    if workspace is None:
        raise MvpError("The MVP Fabric workspace is missing; refusing a partial clean reset.")

    with fabric.active_capacity(az, capacity_id, sleep=sleep, out=out):
        lakehouse_items = client.list_all(f"/workspaces/{workspace}/lakehouses")
        for name in fabric.LAKEHOUSES:
            matches = [item for item in lakehouse_items if item.get("displayName") == name]
            if len(matches) != 1 or MARKER not in (matches[0].get("description") or ""):
                raise MvpError(f"Fabric lakehouse {name!r} is missing, duplicated, or not marked as created by "
                               "this MVP; refusing to delete its data.")

        flows = ["supply_flow", "status_flow", "reset_flow"]
        has_after = False
        for item in client.list_all(f"/workspaces/{workspace}/dataPipelines"):
            if item.get("displayName") == "aftersales_flow":
                if MARKER not in (item.get("description") or ""):
                    raise MvpError("Unmarked aftersales_flow in the MVP workspace; refusing a partial reset.")
                has_after = True
        if has_after:
            flows.append("aftersales_flow")
        items = deploy_items(client, config.names.fabric_workspace, capacity_id, out, flows, reset_aftersales=has_after)
        workspace = items["workspace_id"]
        for pipeline_name in flows:
            pipeline_id = items["pipelines"][pipeline_name]
            jobs = client.list_all(f"/workspaces/{workspace}/items/{pipeline_id}/jobs/instances?jobType=Pipeline")
            active = [job for job in jobs if job.get("status") in {"InProgress", "NotStarted", "Queued", "Running"}]
            if active:
                raise MvpError(f"Fabric pipeline {pipeline_name} has {len(active)} active run(s). Wait for or cancel "
                               "them in Fabric Monitor, then retry the clean reset.")

        lake = fabric.OneLake(client, workspace)
        folders_to_clear = dict(RESET_FILES)
        for layer in ("ingest", "gold"):
            folders_to_clear[layer] += ("aftersales",)
        for name, folders in folders_to_clear.items():
            lakehouse_id = items["lakehouses"][name]
            for folder in folders:
                lake.delete(lakehouse_id, f"Files/{folder}")
                if folder in lake.names(lakehouse_id, "Files"):
                    raise MvpError(f"Fabric did not clear {name}/Files/{folder}; the clean reset is incomplete.")

        result = fabric.run_job(client, workspace, items["pipelines"]["reset_flow"], {}, out)
    tables_cleared = sum(len(tables) for tables in TABLES.values())
    if has_after:
        from fabric.notebooks.aftersales import AFTER_TABLES
        tables_cleared += sum(map(len, AFTER_TABLES.values()))
    out(f"Fabric generated files removed and {tables_cleared} table contents cleared; workspace, lakehouses, "
        "pipelines and reports remain.")
    return {"workspace_id": workspace, "tables_cleared": tables_cleared, "job_id": result}


def upload_deliveries(lake, ingest_id, ingest_folder, delivery_set, out=print):
    """Copy the deliveries to the ingest lakehouse, each delivery's manifest last. An identical delivery is skipped;
    one with the same ID and other content is refused, because Fabric keeps what it has already loaded."""
    sent = 0
    for party_id, delivery in parse_delivery_set(delivery_set):
        files = read_folder(Path(ingest_folder) / "deliveries" / party_id / delivery)
        folder = f"Files/deliveries/{party_id}/{delivery}"
        remote = lake.get(ingest_id, f"{folder}/{DELIVERY_MANIFEST}")
        if remote is not None:
            if remote == files.get(DELIVERY_MANIFEST):
                continue
            raise MvpError(f"The ingest lakehouse already has delivery {party_id}/{delivery} with other content. "
                           "Run python -m dbp_mvp fabric run supply --new-build to send a new build.")
        for name in _manifest_last(files):
            lake.put(ingest_id, f"{folder}/{name}", files[name])
        sent += 1
    out(f"  Uploaded {sent} supplier deliveries to the ingest lakehouse (identical ones are skipped).")
    return sent


def fetch_package(lake, gold_id, build_id):
    """Every file of a completed gold package; the manifest is written last, so it marks a complete package."""
    folder = f"Files/packages/{build_id}"
    if lake.get(gold_id, f"{folder}/{MANIFEST}") is None:
        raise MvpError(f"The gold lakehouse has no complete package for {build_id}.")
    files = {}
    for name in lake.names(gold_id, folder):
        data = lake.get(gold_id, f"{folder}/{name}")
        if data is None:
            raise MvpError(f"The gold package file {folder}/{name} disappeared while it was read.")
        files[name] = data
    return files


def compare(fabric_files, local_files):
    """None when Fabric's package matches the local one, otherwise the reason it is not used. Data files must be
    byte-identical and the manifest equal apart from its time fields."""
    errors = package_errors(fabric_files)
    if errors:
        return "the Fabric package failed its check: " + "; ".join(errors[:3])
    if set(fabric_files) != set(local_files):
        return f"the Fabric package has the files {sorted(fabric_files)}, the local one {sorted(local_files)}"
    changed = sorted(name for name in local_files if name != MANIFEST and fabric_files[name] != local_files[name])
    if changed:
        return "the Fabric package differs from the local one in " + ", ".join(changed)
    remote, here = (json.loads(files[MANIFEST]) for files in (fabric_files, local_files))
    keys = sorted(key for key in remote.keys() | here.keys()
                  if key not in MANIFEST_TIME_FIELDS and remote.get(key) != here.get(key))
    if keys:
        return "the Fabric package manifest differs from the local one in " + ", ".join(keys)
    return None


def run_supply(config, az, outputs, new_build=False, min_sequence=None, send=fabric.send, sleep=None, output=OUTPUT,
               out=print):
    """Prepare the series and build its package in Fabric; the local package is the fallback."""
    for line in simulator.estimate_lines(config.extra_batteries):
        out(line)
    state = prepare(config, new_build, min_sequence, output)
    paths = simulator.build_paths(output, state)
    local = read_folder(paths["package"])
    result = {"build_id": state["build_id"], "build_sequence": state["build_sequence"],
              "manifest_sha256": state["manifest_sha256"], "package_source": "local", "reason": None,
              "workspace_id": None}
    out(f"  Synthetic build {state['build_id']} prepared in {paths['package'].parent}.")
    capacity_id = outputs.get("fabricCapacityId") or ""
    if not config.deploy_fabric or not capacity_id:
        result["reason"] = "Fabric is not deployed (deploy_fabric = false)"
        out(f"  Using the local package: {result['reason']}.")
        return result
    try:
        client = fabric.FabricClient(az, send=send, sleep=sleep)
        with fabric.active_capacity(az, capacity_id, sleep=sleep, out=out):
            items = deploy_items(client, config.names.fabric_workspace, capacity_id, out)
            result["workspace_id"] = items["workspace_id"]
            lake = fabric.OneLake(client, items["workspace_id"])
            upload_deliveries(lake, items["lakehouses"]["ingest"], paths["ingest"], state["delivery_set"], out)
            gold = items["lakehouses"]["gold"]
            if lake.get(gold, f"Files/packages/{state['build_id']}/{MANIFEST}") is None:
                fabric.run_job(client, items["workspace_id"], items["pipelines"]["supply_flow"], {
                    "delivery_set": state["delivery_set"], "build_id": state["build_id"],
                    "build_sequence": str(state["build_sequence"]), "completed_utc": state["completed_utc"]}, out)
            else:
                out("  Fabric already built this package; the pipeline is not run again.")
            fetched = fetch_package(lake, gold, state["build_id"])
            reason = compare(fetched, local)
    except FALLBACK as error:
        reason = f"the Fabric run failed ({type(error).__name__}: {error})"
    if reason is None:
        result["package_source"] = "fabric"
        if fetched == local:
            out("  Fabric's gold package is byte-identical to the local package.")
        else:
            result["match"] = "matching the local build apart from the manifest's time fields"
            out("  Fabric's gold package matches the local package: data files byte-identical, manifest equal apart "
                f"from {', '.join(MANIFEST_TIME_FIELDS)}.")
    else:
        result["reason"] = reason
        out(f"  WARNING: using the local package instead of Fabric's: {reason}.")
    return result


def sql_endpoint(client, workspace, lakehouse_id):
    """(host, endpoint ID) of a lakehouse's SQL analytics endpoint, waiting while it is provisioned."""
    waited = 0
    while True:
        item = client.request("GET", f"/workspaces/{workspace}/lakehouses/{lakehouse_id}")[2]
        endpoint = (item.get("properties") or {}).get("sqlEndpointProperties") or {}
        status = endpoint.get("provisioningStatus")
        if status == "Success":
            host = (endpoint.get("connectionString") or "").strip()
            host = (host[4:] if host.lower().startswith("tcp:") else host).split(",", 1)[0].lower()
            if not powerbi.HOST.fullmatch(host):
                raise MvpError(f"Unexpected SQL analytics endpoint address: {host!r}")
            return host, fabric.guid(endpoint.get("id"), "SQL analytics endpoint ID")
        if status == "Failed":
            raise MvpError("The lakehouse's SQL analytics endpoint failed to provision; open the lakehouse in the "
                           "Fabric portal.")
        if waited >= SQL_ENDPOINT_LIMIT:
            raise MvpError(f"The SQL analytics endpoint was not ready after {SQL_ENDPOINT_LIMIT // 60} minutes "
                           f"(status {status}).")
        client.sleep(SQL_ENDPOINT_POLL)
        waited += SQL_ENDPOINT_POLL


def sql_connect(host, database, driver=None, credential=None):
    """A connection to a Fabric SQL analytics endpoint with the operator's Azure CLI sign-in."""
    with data_plane.sql_errors("Connecting", where="the Fabric SQL analytics endpoint"):
        if driver is None:
            import mssql_python as driver
        if credential is None:
            from azure.identity import AzureCliCredential
            credential = AzureCliCredential()
        target = data_plane.app_module("sql_store").connection_string(host, database)
        return driver.connect(target, autocommit=True, token_provider=credential)


def reporting_views(client, workspace, reporting_id, powerbi_on, connect=sql_connect, out=print):
    """Best effort: the reporting views and, when enabled, the semantic model and report. Returns whether everything
    worked."""
    try:
        host, endpoint = sql_endpoint(client, workspace, reporting_id)
        try:
            client.operation(f"/workspaces/{workspace}/sqlEndpoints/{endpoint}/refreshMetadata", {},
                             needs_result=False)
        except MvpError as error:
            out(f"  WARNING: the SQL analytics endpoint did not refresh its metadata ({error}); the views may "
                "miss new tables.")
        with data_plane.sql_errors("Creating the reporting views", where="the Fabric SQL analytics endpoint"):
            connection = connect(host, REPORTING)
            try:
                data_plane.run_script(connection, VIEWS.read_text(encoding="utf-8"))
            finally:
                connection.close()
        out("  Reporting views updated in the reporting lakehouse's SQL analytics endpoint.")
        if powerbi_on:
            powerbi.publish(client, workspace, host, endpoint, out)
        return True
    except MvpError as error:
        extra = " and the Power BI model and report" if powerbi_on else ""
        out(f"  WARNING: {error}\n  The reporting tables are in place; run python -m dbp_mvp fabric run status to "
            f"retry the views{extra}.")
        return False


def latest_export(lake, reporting_id):
    """The newest complete status export in the reporting lakehouse."""
    for name in sorted((name for name in lake.names(reporting_id, "Files/exports") if EXPORT_ID.fullmatch(name)),
                       reverse=True):
        if lake.get(reporting_id, f"Files/exports/{name}/{MANIFEST}") is not None:
            return name
    raise MvpError("The reporting lakehouse has no status export yet; run python -m dbp_mvp report publish first.")


def report_publish(config, az, outputs, app_url, app_id, get=smoke.http_get_auth, send=fabric.send, sleep=None,
                   connect=sql_connect, output=OUTPUT, out=print):
    """Export the app's passport status, load it into the reporting lakehouse and refresh the views."""
    capacity_id = capacity(config, outputs)
    status, text = get(f"{app_url.rstrip('/')}/report/status", smoke.app_token(az, app_id))
    report = _json(text)
    rows = report.get("rows")
    if status != 200 or not isinstance(rows, list):
        raise MvpError(f"The app's status report answered {status}: {(text or '')[:200]}")
    export_id, generated = report.get("export_id"), report.get("generated_utc")
    if report.get("schema_version") != "2" or not isinstance(export_id, str) or not EXPORT_ID.fullmatch(export_id):
        raise MvpError("The app's status report is not a version 2 export with an export ID. Redeploy the app with "
                       "python -m dbp_mvp deploy --from B3.")
    lot_claims = report.get("lot_claims")
    try:
        if not isinstance(lot_claims, list):
            raise ValueError("The export has no lot_claims list")
        files = make_export(rows, lot_claims, export_id, generated)
    except ValueError as error:
        raise MvpError(f"The status export could not be made: {error}. Passports exist after deploy stage B6 "
                       "imports the synthetic package.") from error
    folder = Path(output) / "exports" / export_id
    folder.mkdir(parents=True, exist_ok=True)
    for name in EXPORT_NAMES:
        (folder / name).write_bytes(files[name])
    out(f"  {len(rows)} passport status rows and {len(lot_claims)} lot claim rows exported (local copy in "
        f"{folder}).")
    print_measures(rows, lot_claims, out)
    client = fabric.FabricClient(az, send=send, sleep=sleep)
    with fabric.active_capacity(az, capacity_id, sleep=sleep, out=out):
        items = deploy_items(client, config.names.fabric_workspace, capacity_id, out)
        lake = fabric.OneLake(client, items["workspace_id"])
        reporting = items["lakehouses"][REPORTING]
        for name in EXPORT_NAMES:
            lake.put(reporting, f"Files/exports/{export_id}/{name}", files[name])
        fabric.run_job(client, items["workspace_id"], items["pipelines"]["status_flow"], {"export_id": export_id},
                       out)
        views = reporting_views(client, items["workspace_id"], reporting, config.powerbi, connect, out)
    return {"export_id": export_id, "rows": len(rows), "lot_claims": len(lot_claims), "views": views}


def print_measures(rows, lot_claims, out=print):
    """The four measures of the export, computed locally with data/metrics.py, the definition status_flow runs."""
    tables = measure_tables(rows, lot_claims, {"export_id": None, "generated_utc": None})
    success = tables["success_measures"][0]

    def share(value, unit="%"):
        return "n/a" if value is None else f"{value}{unit}"

    out("  Measures of this export (synthetic; footprint and value illustrative; status_flow writes the same "
        "values to success_measures):")
    out(f"    passport coverage      {success['published']} of {success['produced']} produced "
        f"({share(success['coverage_pct'])})")
    out(f"    verified footprint     {success['verified_footprint_batteries']} batteries, average "
        f"{share(success['avg_kg_co2e_per_kwh'], ' kg CO2e/kWh')}")
    out(f"    supplier completeness  {success['complete_lots']} of {success['lots']} lots "
        f"({share(success['supplier_completeness_pct'])})")
    out(f"    second life            {success['second_life_count']} repurposed, "
        f"{success['second_life_value_eur_illustrative']:,.0f} EUR (illustrative)")
    return tables


def run_status(config, az, outputs, export_id=None, send=fabric.send, sleep=None, connect=sql_connect, out=print):
    """Rebuild the reporting tables from an export already in the lakehouse (the newest by default)."""
    capacity_id = capacity(config, outputs)
    if export_id is not None and not EXPORT_ID.fullmatch(export_id):
        raise MvpError(f"{export_id!r} is not an export ID like EXP-20261112T101500Z.")
    client = fabric.FabricClient(az, send=send, sleep=sleep)
    with fabric.active_capacity(az, capacity_id, sleep=sleep, out=out):
        items = deploy_items(client, config.names.fabric_workspace, capacity_id, out)
        lake = fabric.OneLake(client, items["workspace_id"])
        reporting = items["lakehouses"][REPORTING]
        if export_id is None:
            export_id = latest_export(lake, reporting)
        elif lake.get(reporting, f"Files/exports/{export_id}/{MANIFEST}") is None:
            raise MvpError(f"The reporting lakehouse has no complete export {export_id}.")
        fabric.run_job(client, items["workspace_id"], items["pipelines"]["status_flow"], {"export_id": export_id},
                       out)
        views = reporting_views(client, items["workspace_id"], reporting, config.powerbi, connect, out)
    return {"export_id": export_id, "views": views}


def publish_model(config, az, outputs, send=fabric.send, sleep=None, out=print):
    """Publish the Power BI semantic model and its report on the reporting lakehouse, whatever the powerbi setting
    says."""
    capacity_id = capacity(config, outputs)
    client = fabric.FabricClient(az, send=send, sleep=sleep)
    with fabric.active_capacity(az, capacity_id, sleep=sleep, out=out):
        items = deploy_items(client, config.names.fabric_workspace, capacity_id, out)
        reporting = items["lakehouses"][REPORTING]
        latest_export(fabric.OneLake(client, items["workspace_id"]), reporting)
        host, endpoint = sql_endpoint(client, items["workspace_id"], reporting)
        model, report = powerbi.publish(client, items["workspace_id"], host, endpoint, out)
    return {"workspace_id": items["workspace_id"], "model_id": model, "report_id": report}


# ---- the passport app (B6) ---------------------------------------------------------------------------------------

def upload_intake(container, build_id, files, sleep=time.sleep, out=print):
    """Copy a package to <build_id>/ in the app's intake container, the manifest last. Identical blobs are kept; a
    different blob of the same name is refused, because intake blobs are never overwritten."""
    prefix = f"{build_id}/"

    def action():
        existing = {blob.name for blob in container.list_blobs(name_starts_with=prefix)}
        added = 0
        for name in _manifest_last(files):
            blob = prefix + name
            if blob in existing:
                if container.download_blob(blob).readall() != files[name]:
                    raise MvpError(f"intake/{blob} already exists with other content. Prepare a new build with "
                                   "python -m dbp_mvp fabric run supply --new-build, then deploy --from B5.")
                continue
            container.upload_blob(blob, files[name], overwrite=False)
            added += 1
        return added

    added = data_plane._blob_retry(action, sleep, out, account="app storage account", failure="Package intake")
    out(f"  Package {build_id}: {added} of {len(files)} files uploaded to the app's intake container.")
    return added


def import_package(post, app_url, token, build_id, manifest_sha256):
    """(status, answer) of the app's package import; the command ID follows from the manifest, so a rerun replays."""
    status, text = post(f"{app_url.rstrip('/')}/packages/import",
                        {"intake_folder": build_id, "command_id": f"seed-import-{manifest_sha256[:16]}"}, token)
    return status, _json(text)


def _refused(what, status, body, text=""):
    hint = (" Your token has no Passport app role yet; wait a few minutes, sign in again with az login, then rerun "
            "with --from B6." if status in (401, 403) else "")
    detail = f"{body.get('error') or (text or '')[:200]} {body.get('message') or ''}".strip().rstrip(".")
    return MvpError(f"{what} answered {status}: {detail}.{hint}")


def upload_evidence(post_multipart, app_url, token, folder, uploads):
    """Upload the series' evidence documents; each command ID follows from the file, so a rerun replays."""
    results = []
    for file_name, kind, lot_id in uploads:
        data = (Path(folder) / file_name).read_bytes()
        key = f"{file_name}|{kind}|{lot_id}|{hashlib.sha256(data).hexdigest()}"
        command = "seed-evidence-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        status, text = post_multipart(f"{app_url.rstrip('/')}/evidence",
                                      {"kind": kind, "lot_id": lot_id, "command_id": command},
                                      ("file", file_name, "application/json", data), token)
        body = _json(text)
        if status != 201:
            raise _refused(f"The evidence upload of {file_name}", status, body, text)
        results.append({"file": file_name, "evidence_id": body.get("evidence_id"),
                        "replayed": bool(body.get("replayed"))})
    return results


def seed_app(config, az, app_url, app_id, container, post=smoke.http_post, post_multipart=smoke.http_post_multipart,
             sleep=time.sleep, output=OUTPUT, out=print, tag=None):
    """Import the series' package into the app and upload its evidence. When the app already has a newer package
    (for example after output/ was deleted, or for another series), a new build is prepared once and imported."""
    state = prepare(config, output=output, tag=tag)
    token = smoke.app_token(az, app_id)
    rebuilt = False
    while True:
        files = read_folder(simulator.build_paths(output, state)["package"])
        upload_intake(container, state["build_id"], files, sleep, out)
        status, body = import_package(post, app_url, token, state["build_id"], state["manifest_sha256"])
        if status == 200:
            break
        if status == 409 and body.get("error") in ("stale_package", "build_conflict") and not rebuilt:
            latest = body.get("latest_build_sequence")
            out(f"  The app answered {body['error']}; preparing a new build after "
                f"{body.get('latest_build_id') or 'it'}.")
            state = prepare(config, new_build=True, min_sequence=latest + 1 if isinstance(latest, int) else None,
                            output=output, tag=tag)
            rebuilt = True
            continue
        raise _refused(f"The package import of {state['build_id']}", status, body)
    out(f"  Package {state['build_id']}: {body.get('outcome')}.")
    evidence = upload_evidence(post_multipart, app_url, token, simulator.build_paths(output, state)["evidence"],
                               simulator.evidence_uploads(state["tag"], state["batteries"]))
    out(f"  Evidence: {len(evidence)} documents ({sum(item['replayed'] for item in evidence)} already uploaded).")
    claims = connect(post, app_url, token, output, state, out)
    return {"build_id": state["build_id"], "build_sequence": state["build_sequence"],
            "manifest_sha256": state["manifest_sha256"], "import_outcome": body.get("outcome"),
            "evidence": len(evidence), "claims": claims, "rebuilt": rebuilt}


def connect(post, app_url, token, output, state, out=print):
    """Record the mock connector agreements, then deliver every lot's signed claim version 1 through the connector.
    Agreement command IDs and message IDs follow from the content, so a rerun replays."""
    paths = simulator.build_paths(output, state)
    base = app_url.rstrip("/")
    for agreement in simulator.connector_agreements(json.loads(paths["registry"].read_bytes())):
        status, text = post(f"{base}/connector/v1/agreements", agreement, token)
        if status != 201:
            raise _refused(f"The connector agreement {agreement['agreement_id']}", status, _json(text), text)
    results = []
    for message_id in simulator.opening_messages(state["tag"], state["batteries"]):
        envelope = json.loads((paths["messages"] / f"{message_id}.json").read_bytes())
        status, text = post(f"{base}/connector/v1/messages", {"envelope": envelope}, token)
        body = _json(text)
        if status != 201 or body.get("result") != "verified":
            raise _refused(f"The connector message {message_id}", status, body, text)
        results.append(body.get("receipt_id"))
    out(f"  Connector (mock): {len(results)} signed claims verified.")
    return len(results)
