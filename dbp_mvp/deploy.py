"""Stage B, run on the developer VM after az login: sign-in, database, the passport app, the agents, the synthetic
data, checks.

B1 app registration -> B2 Azure SQL schema, app user and ledger digest -> B3 image and app -> B4 Foundry agents and
the app's agent mode -> B5 synthetic supply package, built in Fabric when it is deployed -> B6 package import and
evidence uploads into the app -> B7 checks, including one call of the hosted Supplier agent -> B8 summary. B2 runs
before the app because a running replica is ready only once its database user exists. Finished phases are skipped
while their inputs are unchanged; --from repeats a phase and every later one. This machine keeps its own
output/state.json.
"""

import base64
import contextlib
import dataclasses
import hashlib
import json
import shutil
import socket
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from data import simulator

from . import MvpError, aftersales, data_plane, fabric, foundry, identity, powerbi, preflight, smoke, supply
from . import image as images
from .az import AzError
from .bootstrap import flatten_outputs
from .config import INFRA, OUTPUT, PROJECT_TAG
from .progress import stage
from .state import files_fingerprint, fingerprint

ORDER = ("B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8")
TEMPLATE = INFRA / "modules" / "app.bicep"
RENEW_DAYS = 7
REQUIRED = ("agentsSubnetId", "appIdentityClientId", "appIdentityName", "appParameters", "appStorageName",
            "appStorageUrl", "applicationInsightsAppId", "databaseName", "digestStorageName", "digestStorageUrl",
            "environmentDomain", "environmentName", "foundryAccountId", "foundryAccountName",
            "foundryProjectEndpoint", "foundryProjectPrincipalId", "logsName", "registryLoginServer",
            "registryName", "sqlServerFqdn", "sqlServerName", "workbookIds")
PERSONAS = ("executive", "compliance", "agents", "operations")
SMOKE_COUNTER = "supplier_smoke"
DURABLE_TASK_API = "2026-02-01"
APP_BLOB_CONTAINERS = ("evidence", "passports", "intake")
BLOB_DATA_OWNER = "Storage Blob Data Owner"
# Without deleteBlobVersion/action, deleting a previous version is refused with 403, sometimes under this misleading
# code rather than an authorization one.
VERSION_DENIED = ("AuthorizationPermissionMismatch", "AuthorizationFailure", "OperationNotAllowedOnAutomaticSnapshot")
VERSION_ATTEMPTS = 20  # 30 seconds apart: a new data-plane role can take up to ten minutes to apply
APP_STATUS_QUERY = ("{mode:properties.configuration.activeRevisionsMode, "
                    "fqdn:properties.configuration.ingress.fqdn, state:properties.provisioningState, "
                    "revision:properties.latestRevisionName, readyRevision:properties.latestReadyRevisionName, "
                    "containers:properties.template.containers[].{name:name,image:image,"
                    "prices:env[?name=='DBP_PRICES_B64']}}")
APP_PRICES_QUERY = "properties.template.containers[?name=='passport'].{prices:env[?name=='DBP_PRICES_B64']}"
APP_REVISION_QUERY = "{weight:properties.trafficWeight,images:properties.template.containers[?name=='passport'].image}"


def _connect(outputs, sleep, out):
    return data_plane.connect(outputs, sleep=sleep, out=out)


@dataclasses.dataclass
class Runtime:
    """Clock, network and SDK access, replaced by fakes in tests."""
    sleep: object = time.sleep
    clock: object = time.monotonic
    now: object = lambda: datetime.now(timezone.utc)  # noqa: E731
    resolve: object = socket.getaddrinfo
    http_get: object = smoke.http_get
    http_post: object = smoke.http_post
    http_get_auth: object = smoke.http_get_auth
    http_post_multipart: object = smoke.http_post_multipart
    connect: object = _connect
    container: object = data_plane.blob_container
    fabric_send: object = fabric.send
    sql_connect: object = supply.sql_connect
    output: object = OUTPUT
    project: object = foundry.project_client
    models: object = foundry.sdk_models
    credential: object = foundry.cli_credential
    evaluation: object = foundry.evaluation_module


def infrastructure(config, az):
    """The outputs of the bootstrap's infrastructure deployment, read from Azure (not from the laptop's state)."""
    name = config.names.deployment
    result = az.find("deployment", "sub", "show", "--name", name, "--query",
                     "{state:properties.provisioningState, outputs:properties.outputs}")
    if result is None:
        raise MvpError(f"Deployment {name} was not found in subscription {config.subscription_id}. Run "
                       "python -m dbp_mvp bootstrap on your laptop first.")
    if result.get("state") != "Succeeded":
        raise MvpError(f"Deployment {name} is {result.get('state')}. Finish it from your laptop with "
                       "python -m dbp_mvp bootstrap --from A1.")
    outputs = flatten_outputs(result.get("outputs"))
    missing = [key for key in REQUIRED if not outputs.get(key)]
    if missing:
        raise MvpError(f"Deployment {name} lacks the outputs {', '.join(missing)}. Update this checkout to the "
                       "same version as the laptop's and rerun bootstrap --from A1 there.")
    return outputs


def workbook_links(tenant_id, workbooks):
    links = {item["persona"]: f"https://portal.azure.com/#@{tenant_id}/resource{item['id']}"
             for item in workbooks if item.get("persona") in PERSONAS and item.get("id")}
    return dict(sorted(links.items()))


def days_left(expires, now):
    try:
        return (date.fromisoformat(expires) - now.date()).days
    except (TypeError, ValueError):
        return -1


def _registration(config, az, state, session, outputs, runtime, out):
    """B1. The Entra app registration, its service principal and the operator's roles. No secret yet."""
    name = config.names.app_registration
    fqdn = f"{config.names.app}.{outputs['environmentDomain']}"
    phase_fingerprint = fingerprint({"name": name, "fqdn": fqdn, "operator": session["operator_object_id"],
                                     "definition": identity.definition()})
    saved = state.outputs("B1")
    if state.done("B1", phase_fingerprint) and identity.exists(az, saved["object_id"], runtime.sleep):
        out("B1 app registration: unchanged, skipped.")
        return saved
    with stage("B1 Registering the app in Microsoft Entra ID"):
        try:
            registration = identity.ensure_registration(az, name, fqdn, runtime.sleep,
                                                        owner_id=session["operator_object_id"])
        except AzError as error:
            if identity.denied(error):
                raise identity.refusal(f"update app registration {name} or make you its owner", "B1") from None
            raise
        added, held = identity.assign_roles(az, registration["service_principal_id"],
                                            session["operator_object_id"], runtime.sleep)
    if added:
        out(f"  Assigned {', '.join(added)} to {config.operator_upn}.")
    registration["roles"] = held
    state.record("B1", phase_fingerprint, registration)
    return registration


def _database(config, state, outputs, runtime, out):
    """B2. Ledger tables, the app identity's database user and a first anchored ledger digest."""
    grants = data_plane.grant_script(outputs["appIdentityName"], outputs["appIdentityClientId"])
    scripts = hashlib.sha256(data_plane.SCHEMA.read_bytes() + b"\0" + grants.encode("utf-8")).hexdigest()
    phase_fingerprint = fingerprint({"scripts": scripts, **{key: outputs[key] for key in (
        "sqlServerFqdn", "databaseName", "appIdentityName", "appIdentityClientId", "digestStorageUrl")}})
    if state.done("B2", phase_fingerprint):
        out("B2 database: unchanged, skipped.")
        return state.outputs("B2")
    with stage("B2 Preparing Azure SQL: ledger tables, app user and ledger digest"):
        connection = runtime.connect(outputs, runtime.sleep, out)
        try:
            tables = data_plane.apply_schema(connection, outputs)
            if not tables:
                raise MvpError("The schema script ran but no tables exist in the dbp schema.")
            anchored = data_plane.anchor(connection, outputs, runtime.container, runtime.sleep, out)
        finally:
            connection.close()
    result = {"tables": tables, "ledger_block": anchored["block_id"], "digest_blob": anchored["blob"]}
    state.record("B2", phase_fingerprint, result)
    return result


def prices_parameter(config, az=None):
    """Seed prices from settings; preserve an applied price list during later app/agent deployments."""
    if az is not None:
        containers = az.find("containerapp", "show", "--resource-group", config.names.resource_group,
                             "--name", config.names.app, "--query", APP_PRICES_QUERY) or []
        entries = [entry for container in containers for entry in container.get("prices") or []]
        if len(entries) > 1:
            raise MvpError("The app has multiple DBP_PRICES_B64 values; inspect its configuration before deploying.")
        if entries and entries[0].get("secretRef"):
            raise MvpError("Cannot preserve a secret-backed DBP_PRICES_B64. Use app prices --file to apply "
                           "a plain model price list before deploying.")
        if entries and entries[0].get("value"):
            return {"pricesB64": entries[0]["value"]}
    if not config.prices:
        return {}
    return {"pricesB64": base64.b64encode(json.dumps(config.prices, sort_keys=True).encode("utf-8")).decode("ascii")}


def registry_parameter(output):
    """The party registry (public keys only) of the runtime signing keys under output/keys, which the B6 seed and the
    acts sign with; the app reads it from DBP_PARTY_REGISTRY_B64."""
    document = simulator.party_registry(simulator.signing_keys(output))
    return {"partyRegistryB64": base64.b64encode(json.dumps(document, sort_keys=True).encode("utf-8")).decode("ascii")}


def deploy_app(config, az, outputs, registration, image, links, runtime, out, agent_mode=False, extra=None):
    """Deploy infra/modules/app.bicep with a new sign-in secret; older secrets are removed once it succeeds."""
    prices = prices_parameter(config, az)
    object_id = registration["object_id"]
    key_id, secret, expires = identity.add_secret(az, object_id, runtime.now(), runtime.sleep)
    parameters = {**outputs["appParameters"], "image": image, "appClientId": registration["app_id"],
                  "appClientSecret": secret, "warmReplica": config.warm_replica, "workbookLinks": links,
                  "agentMode": agent_mode, **prices, **registry_parameter(runtime.output),
                  **(extra or {})}
    try:
        with az.parameter_file(parameters) as parameter_file, stage("Deploying the passport app (Container Apps)"):
            result = az("deployment", "group", "create", "--resource-group", config.names.resource_group, "--name",
                        f"{config.names.app}-app", "--template-file", str(TEMPLATE), "--parameters", parameter_file,
                        "--query", "properties.outputs")
    except BaseException:
        try:
            identity.remove_secret(az, object_id, key_id, runtime.sleep)
        except MvpError as error:
            out(f"  The unused sign-in secret could not be removed ({error}); it expires on {expires}.")
        raise
    try:
        identity.prune_secrets(az, object_id, keep=key_id, sleep=runtime.sleep)
    except MvpError as error:
        out(f"  Older sign-in secrets could not be removed ({error}); they expire on their own.")
    deployed = flatten_outputs(result)
    return {"appName": deployed.get("appName"), "appFqdn": deployed.get("appFqdn"), "appUrl": deployed.get("appUrl"),
            "sign_in_expires": expires}


def _app(config, az, state, session, outputs, registration, runtime, image, extras, out):
    """B3. Deploy the app with Easy Auth sign-in; image is the one to deploy, or None to build app/ first.

    extras are the agent settings B4 recorded last time, so a new image keeps the app in agent mode.
    """
    saved = state.outputs("B3")
    source = {"image": image} if image else {"app": files_fingerprint(images.APP)}
    links = workbook_links(session["tenant_id"], outputs["workbookIds"])
    phase_fingerprint = fingerprint({"source": source, "template": hashlib.sha256(TEMPLATE.read_bytes()).hexdigest(),
                                     "appParameters": outputs["appParameters"],
                                     "appClientId": registration["app_id"], "warmReplica": config.warm_replica,
                                     "workbookLinks": links, **registry_parameter(runtime.output),
                                     **prices_parameter(config, az)})
    present = az.find("resource", "show", "--resource-group", config.names.resource_group, "--name",
                      config.names.app, "--resource-type", "Microsoft.App/containerApps", "--query", "id")
    remaining = days_left(saved.get("sign_in_expires"), runtime.now())
    unchanged = state.done("B3", phase_fingerprint) and present
    if unchanged and remaining > RENEW_DAYS:
        out("B3 app: unchanged, skipped.")
        return saved
    if unchanged:
        out(f"B3 app: the sign-in secret expires in {max(remaining, 0)} days; renewing it.")
        chosen = saved["image"]
    else:
        chosen = image
    if not chosen:
        with stage("B3 Building the app image with ACR Tasks (long-running)"):
            chosen = images.build(az, config.names.resource_group, outputs["registryName"],
                                  outputs["registryLoginServer"], runtime.now())
    result = {"image": chosen, **deploy_app(config, az, outputs, registration, chosen, links, runtime, out,
                                            agent_mode=bool(extras), extra=extras),
              "agent_extra": fingerprint(extras) if extras else ""}
    state.record("B3", phase_fingerprint, result)
    return result


def app_redeploy(config, az, state, runtime=None, out=print):
    """Rebuild app/ and replace only the existing Container App's image, without running any Stage B phases."""
    runtime = runtime or Runtime()
    outputs = _command(config, az, state)
    if not state.outputs("B3").get("image"):
        raise MvpError("This machine has no deployed app image recorded. Use the developer VM and its existing "
                       "output/state.json; app redeploy does not provision an application.")
    group, name = config.names.resource_group, config.names.app
    app = az.find("containerapp", "show", "--resource-group", group, "--name", name, "--query", APP_STATUS_QUERY)
    if app is None:
        raise MvpError(f"Container App {name} was not found in {group}. App-only redeployment requires an existing "
                       "app; nothing was built or deployed.")
    containers = [item for item in app.get("containers") or [] if item.get("name") == "passport"]
    if app.get("mode") != "Single" or not app.get("fqdn") or len(containers) != 1:
        raise MvpError("App-only redeployment requires the existing Single revision mode, HTTPS ingress and "
                       "passport container. No configuration or traffic settings were changed.")
    out(f"App-only redeploy of {name}. Previous image: {containers[0].get('image')}")
    with stage("Building a fresh passport app image with ACR Tasks (long-running)"):
        image = images.build(az, group, outputs["registryName"], outputs["registryLoginServer"], runtime.now())
    with stage("Updating only the passport container image"):
        revision = az("containerapp", "update", "--resource-group", group, "--name", name,
                      "--container-name", "passport", "--image", image, "--query", "properties.latestRevisionName")
    if not revision or revision == app.get("revision"):
        raise MvpError("Container Apps did not report a new revision. Inspect the app's revisions and log stream "
                       "before retrying python -m dbp_mvp app redeploy.")
    with stage(f"Waiting for app revision {revision} to serve traffic"):
        _wait_app_revision(config, az, revision, image, runtime, out)
        smoke.wait_ready(f"https://{app['fqdn']}", runtime.http_get, runtime.sleep, runtime.clock, out,
                         retry_command="python -m dbp_mvp app redeploy")
    state.amend("B3", {"image": image})
    out(f"App-only redeploy complete: {image}\n  https://{app['fqdn']}\n"
        "  No deployment phases, data reset, import or reporting refresh were run.")
    return {"image": image, "revision": revision, "appUrl": f"https://{app['fqdn']}"}


def app_prices(config, az, runtime=None, out=print):
    """Apply prices to the existing app from either machine, without Stage B state or private data-plane access."""
    runtime = runtime or Runtime()
    if not config.prices:
        raise MvpError("No model prices configured. Set prices_file in config/mvp.toml or run "
                       "python -m dbp_mvp app prices --file config/prices.json.")
    preflight.session_check(config, az)
    group, name = config.names.resource_group, config.names.app
    resource_group = az.find("group", "show", "--name", group, "--query", "{location:location,tags:tags}")
    if not resource_group or (resource_group.get("tags") or {}).get("project") != PROJECT_TAG \
            or resource_group.get("location") != config.location:
        raise MvpError(f"Price-only updates require the existing MVP resource group {group} in {config.location} "
                       f"with project={PROJECT_TAG}. Nothing was changed.")
    app = az.find("containerapp", "show", "--resource-group", group, "--name", name, "--query", APP_STATUS_QUERY)
    if app is None:
        raise MvpError(f"Container App {name} was not found in {group}. Deploy the app before applying prices.")
    containers = [item for item in app.get("containers") or [] if item.get("name") == "passport"]
    if app.get("mode") != "Single" or not app.get("fqdn") or not app.get("revision") or len(containers) != 1 \
            or not containers[0].get("image"):
        raise MvpError("Price-only updates require Single revision mode, HTTPS ingress and an existing passport "
                       "container image. No configuration or traffic settings were changed.")
    encoded = prices_parameter(config)["pricesB64"]
    image = containers[0]["image"]
    values = [entry.get("value") for entry in containers[0].get("prices") or []]
    revision = app["revision"]
    if values == [encoded]:
        out("Model prices are unchanged; checking the current app revision.")
    else:
        out(f"Applying {config.prices['currency']} prices for {len(config.prices['models'])} models to {name}.")
        with stage("Updating only the passport container's model prices (same image)"):
            revision = az("containerapp", "update", "--resource-group", group, "--name", name,
                          "--container-name", "passport", "--set-env-vars", f"DBP_PRICES_B64={encoded}",
                          "--query", "properties.latestRevisionName")
        if not revision or revision == app["revision"]:
            raise MvpError("Container Apps did not report a new revision. Inspect its revisions and log stream "
                           "before retrying the same app prices command.")
    retry = "the same python -m dbp_mvp app prices command (including --file, if used)"
    with stage(f"Waiting for app revision {revision} to serve the configured prices"):
        _wait_app_revision(config, az, revision, image, runtime, out, prices=encoded, retry_command=retry)
        smoke.wait_ready(f"https://{app['fqdn']}", runtime.http_get, runtime.sleep, runtime.clock, out,
                         retry_command=retry)
    out(f"Model prices configured ({config.prices['currency']}): https://{app['fqdn']}\n"
        "  No image build, deployment phases, data reset, import or agent changes were run.\n"
        "  Refresh the app; runs with recorded token counts now use this price list.")
    return {"revision": revision, "appUrl": f"https://{app['fqdn']}", "currency": config.prices["currency"]}


def _wait_app_revision(config, az, revision, image, runtime, out, *, prices=None,
                       retry_command="python -m dbp_mvp app redeploy"):
    """Wait for the named revision (or pin the latest on first read) to serve the expected image."""
    deadline = runtime.clock() + smoke.READY_TIMEOUT
    while True:
        current = az("containerapp", "show", "--resource-group", config.names.resource_group, "--name",
                     config.names.app, "--query", APP_STATUS_QUERY) or {}
        if revision is None:
            revision = current.get("revision")
        images_now = [item.get("image") for item in current.get("containers") or []
                      if item.get("name") == "passport"]
        prices_now = [entry.get("value") for item in current.get("containers") or []
                      if item.get("name") == "passport" for entry in item.get("prices") or []]
        if (not revision or current.get("revision") != revision or images_now != [image]
                or current.get("mode") != "Single" or (prices is not None and prices_now != [prices])):
            raise MvpError("The Container App changed during redeployment. Inspect its revisions before retrying; "
                           "the requested app configuration has not been confirmed as serving traffic.")
        if current.get("state") in ("Failed", "Canceled"):
            raise MvpError(f"Container App revision {revision} failed ({current['state']}). Read Container App > "
                           f"Monitoring > Log stream, fix the cause and rerun {retry_command}.")
        progress = f"latest ready: {current.get('readyRevision') or 'none'}"
        if current.get("state") == "Succeeded" and current.get("readyRevision") == revision:
            serving = az("containerapp", "revision", "show", "--resource-group", config.names.resource_group,
                         "--name", config.names.app, "--revision", revision, "--query", APP_REVISION_QUERY) or {}
            if serving.get("images") != [image]:
                raise MvpError(f"Revision {revision} does not report the expected passport image {image}. "
                               f"Inspect Container App > Revisions before retrying {retry_command}.")
            weight = serving.get("weight")
            if weight == 100:
                out(f"  Revision {revision} serves {image} with 100% of traffic.")
                return
            progress = f"traffic: {weight}%" if weight is not None else "traffic weight not reported"
        if runtime.clock() >= deadline:
            raise MvpError(f"Container App revision {revision} did not become ready with 100% of traffic after "
                           f"{smoke.READY_TIMEOUT // 60} minutes ({progress}). Inspect Container App > Revisions "
                           f"and Monitoring > Log stream, then rerun {retry_command}. No data reset is performed.")
        out(f"  Waiting for revision {revision} ({progress}) ...")
        runtime.sleep(smoke.READY_INTERVAL)


def _agents(config, az, state, session, outputs, registration, app, runtime, out):
    """B4. The five Foundry agents, the hosted agents' app roles, then the app switched to agent mode."""
    contracts = foundry.contracts()
    endpoint = foundry.project_endpoint(outputs)
    code, source_sha = foundry.package()
    prompts = {role: foundry.prompt_hash(contracts, role)
               for role, spec in contracts.ROLES.items() if spec["kind"] == "prompt"}
    phase_fingerprint = fingerprint({
        "source": source_sha, "prompts": prompts, "roles": contracts.ROLES, "hosted": foundry.HOSTED,
        "endpoint": endpoint, "appFqdn": app["appFqdn"], "appClientId": registration["app_id"],
        **{key: outputs[key] for key in ("foundryAccountId", "foundryProjectPrincipalId", "agentsSubnetId")}})
    saved = state.outputs("B4")
    deployed_extra = app.get("agent_extra", "")
    if state.done("B4", phase_fingerprint) and saved.get("extra_fingerprint") == deployed_extra:
        out("B4 agents: unchanged, skipped.")
        return app
    with stage("B4 Checking Foundry: private access, agent subnet and model versions"):
        foundry.verify_models(az, config.names.resource_group, outputs, contracts.ROLES, runtime.resolve)
        if foundry.grant_project(az, outputs):
            out("  Gave the Foundry project's identity the Foundry User role on the Foundry account.")
    agents, client_ids = {}, []
    models = runtime.models()
    with runtime.project(endpoint) as project:
        for role, spec in contracts.ROLES.items():
            name = foundry.agent_name(role)
            if spec["kind"] == "hosted":
                with stage(f"B4 Hosted agent {name} and its app role"):
                    environment = foundry.hosted_environment(role, spec["model"], app["appFqdn"],
                                                             registration["app_id"])
                    agents[role] = foundry.ensure_hosted(project, models, role, environment, code, source_sha,
                                                         runtime.sleep, runtime.clock, out)
                    client_ids.append(foundry.authorize(az, registration, role, agents[role]["principal_id"],
                                                        agents[role]["client_id"], runtime.sleep))
            else:
                with stage(f"B4 Prompt agent {name}"):
                    agents[role] = foundry.ensure_prompt(project, models, contracts, role)
    extras = foundry.app_extras(foundry.app_config(endpoint, contracts.ROLES, agents), client_ids)
    extra_fingerprint = fingerprint(extras)
    if extra_fingerprint != deployed_extra:
        out("B4 Switching the app to agent mode.")
        links = workbook_links(session["tenant_id"], outputs["workbookIds"])
        deployed = {**deploy_app(config, az, outputs, registration, app["image"], links, runtime, out,
                                 agent_mode=True, extra=extras), "agent_extra": extra_fingerprint}
        state.amend("B3", deployed)
        app = {**app, **deployed}
    state.record("B4", phase_fingerprint, {
        "agent_mode": True, "app_extra": extras, "extra_fingerprint": extra_fingerprint,
        "versions": {role: agents[role]["version"] for role in contracts.ROLES},
        "smoke_key": phase_fingerprint[:12]})
    return app


def _supply(config, az, state, outputs, runtime, out, new_build=False):
    """B5. The synthetic series and its passport package, built in Fabric when it is deployed, otherwise locally.

    A Fabric run that fell back to the local package is recorded as unfinished, so the next deploy tries Fabric again.
    """
    prepared = supply.prepare(config, new_build=new_build, output=runtime.output)
    capacity_id = (outputs.get("fabricCapacityId") or "") if config.deploy_fabric else ""
    phase_fingerprint = fingerprint({"identity": config.identity, "batteries": config.extra_batteries,
                                     "sources": supply.sources(), "workspace": config.names.fabric_workspace,
                                     "capacity": capacity_id, "build": prepared["build_id"],
                                     "manifest": prepared["manifest_sha256"]})
    if state.done("B5", phase_fingerprint):
        out("B5 synthetic data: unchanged, skipped.")
        return state.outputs("B5")
    label = "in Fabric (long-running)" if capacity_id else "locally (Fabric is not deployed)"
    with stage(f"B5 Building the synthetic supply package {label}"):
        result = supply.run_supply(config, az, outputs, send=runtime.fabric_send, sleep=runtime.sleep,
                                   output=runtime.output, out=out)
    retry = bool(capacity_id) and result["package_source"] != "fabric"
    state.record("B5", ("retry:" if retry else "") + phase_fingerprint, result)
    return result


def seed_fingerprint(config, app_id, app_url, prepared, output):
    """What B6 imported: the app, its sign-in and the main series' build and evidence."""
    return fingerprint({"identity": config.identity, "appUrl": app_url, "appClientId": app_id,
                        "aftersales_flow": supply.sources() if config.deploy_fabric else None,
                        **supply.seed_inputs(prepared, output)})


def seed_aftersales(config, az, outputs, app_url, app_id, prepared, container, runtime, out):
    """Import the bytes fetched from Gold, not a locally generated substitute. Verify the SQL receipt before B6."""
    if not config.deploy_fabric:
        out("  Fabric is disabled: no deployed warranty/service or recall feed was imported.")
        return None
    envelope = aftersales.run_cloud(config, az, outputs, prepared, send=runtime.fabric_send, sleep=runtime.sleep,
                                    output=runtime.output, out=out)
    content = aftersales.gold_path(prepared, runtime.output).read_bytes()
    delivery_id = envelope["delivery_id"]
    supply.upload_intake(container, f"aftersales/{delivery_id}", {"aftersales.json": content}, runtime.sleep, out)
    status, text = runtime.http_post(f"{app_url.rstrip('/')}/imports/aftersales/{delivery_id}", {},
                                     smoke.app_token(az, app_id))
    if status not in (200, 201):
        raise MvpError(f"Aftersales import {delivery_id} answered HTTP {status}: {(text or '')[:240]}")
    try:
        receipt = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise MvpError(f"Aftersales import {delivery_id} returned an invalid receipt.") from exc
    if not isinstance(receipt, dict) or any(receipt.get(k) != envelope[k] for k in
                                             ("series_id", "delivery_id", "as_of_utc", "sha256")):
        raise MvpError(f"Aftersales receipt for {delivery_id} does not match the Fabric Gold package.")
    out(f"  Warranty/service and recall: imported Fabric delivery {delivery_id} for {receipt['series_id']}.")
    return receipt


def _seed(config, az, state, outputs, registration, app, runtime, out):
    """B6. The series' package copied to the app's intake container and imported, then its evidence uploaded."""
    prepared = supply.prepare(config, output=runtime.output)
    if state.done("B6", seed_fingerprint(config, registration["app_id"], app["appUrl"], prepared, runtime.output)):
        out("B6 seed data: unchanged, skipped.")
        return state.outputs("B6")
    with stage("B6 Waiting for the app to become ready"):
        smoke.wait_ready(app["appUrl"], runtime.http_get, runtime.sleep, runtime.clock, out)
    with stage("B6 Importing the synthetic package and evidence into the app"), \
            runtime.container(outputs["appStorageUrl"], supply.INTAKE) as container:
        result = supply.seed_app(config, az, app["appUrl"], registration["app_id"], container, runtime.http_post,
                                 runtime.http_post_multipart, runtime.sleep, runtime.output, out)
        seeded = supply.prepare(config, output=runtime.output)
        result["aftersales"] = seed_aftersales(config, az, outputs, app["appUrl"], registration["app_id"],
                                                seeded, container, runtime, out)
    supplied = state.outputs("B5")
    if supplied.get("package_source") == "fabric" and supplied.get("build_id") == result["build_id"]:
        out(f"  Imported package {result['build_id']}: Fabric built it and it matches the local build.")
    else:
        why = supplied.get("reason") if supplied.get("build_id") == result["build_id"] else \
            "B6 prepared it after B5, so Fabric has not built it yet"
        out(f"  Imported package {result['build_id']} from the local build ({why}).")
    state.record("B6", seed_fingerprint(config, registration["app_id"], app["appUrl"], seeded, runtime.output),
                 result)
    return result


def ledger(config, az, action, runtime=None, out=print):
    """python -m dbp_mvp ledger anchor|verify: anchor a new digest, or verify against the anchored ones."""
    runtime = runtime or Runtime()
    preflight.session_check(config, az)
    outputs = infrastructure(config, az)
    connection = runtime.connect(outputs, runtime.sleep, out)
    try:
        if action == "anchor":
            result = data_plane.anchor(connection, outputs, runtime.container, runtime.sleep, out)
            out(f"Anchored ledger block {result['block_id']} ({result['digests']} digests stored); SQL verified "
                f"the ledger up to block {result['verified_block']}.")
        else:
            result = data_plane.verify_anchored(connection, outputs, runtime.container, runtime.sleep, out)
            out(f"SQL verified the ledger against {result['digests']} anchored digests, up to block "
                f"{result['verified_block']}.")
    finally:
        connection.close()
    return result


def data_reset(config, az, state, yes=False, runtime=None, confirm=input, out=print):
    """python -m dbp_mvp data reset: delete and recreate the SQL database, then repeat B2 (schema, grants, first
    digest) and B6 (package import and evidence). A new schema version needs a fresh database; the recreated
    database has a new creation time, so its ledger digests start under a new prefix and the old ones stay put."""
    runtime = runtime or Runtime()
    preflight.session_check(config, az)
    state.bind(config.identity)
    outputs = infrastructure(config, az)
    registration, app = state.outputs("B1"), state.outputs("B3")
    if not registration.get("app_id") or not app.get("appUrl"):
        raise MvpError("This machine has no deployed passport app recorded. Run python -m dbp_mvp deploy on the "
                       "developer VM first.")
    group, server, name = config.names.resource_group, outputs["sqlServerName"], outputs["databaseName"]
    _recreate_database(config, az, group, server, name, yes=yes, confirm=confirm, out=out)
    for phase in ("B2", "B6", "B7"):
        state.data["phases"].pop(phase, None)
    state.save()
    database = _database(config, state, outputs, runtime, out)
    seeded = _seed(config, az, state, outputs, registration, app, runtime, out)
    out(f"Database {name} recreated: ledger block {database['ledger_block']} anchored, package "
        f"{seeded['build_id']} imported. Run python -m dbp_mvp deploy --from B7 to repeat the checks.")
    return {"database": database, "seed": seeded}


def _recreate_database(config, az, group, server, name, yes=False, confirm=input, out=print):
    current = az.find("sql", "db", "show", "--resource-group", group, "--server", server, "--name", name)
    out(f"This permanently deletes database {name} on {server} with every passport, review, agent record and "
        "ledger entry in it, then recreates it empty. Anchored digests of the old database stay in the digest "
        "storage account.")
    if not yes and confirm(f"Type {name} to confirm: ").strip() != name:
        raise MvpError("Cancelled; nothing was deleted.")
    with stage(f"Recreating database {name}"):
        if current:
            az("sql", "db", "delete", "--resource-group", group, "--server", server, "--name", name, "--yes")
        az("sql", "db", "create", "--resource-group", group, "--server", server, "--name", name,
           *recreate_arguments(current or {}))
    return current


def _deactivate_app(config, az, runtime, out):
    group, name = config.names.resource_group, config.names.app

    def active():
        return az("containerapp", "revision", "list", "--name", name, "--resource-group", group,
                  "--query", "[?properties.active].name") or []

    for revision in active():
        out(f"  Deactivating app revision {revision} while stored data is replaced.")
        az("containerapp", "revision", "deactivate", "--name", name, "--resource-group", group,
           "--revision", revision)
    for _ in range(60):
        if not active():
            return
        runtime.sleep(5)
    raise MvpError("The passport app still has an active revision after five minutes. No data stores were reset; "
                   "check Container Apps and retry.")


def _reset_task_hub(config, az, runtime, out):
    resource_id = (f"/subscriptions/{config.subscription_id}/resourceGroups/{config.names.resource_group}"
                   f"/providers/Microsoft.DurableTask/schedulers/{config.prefix}-dts/taskHubs/"
                   f"{config.prefix}-passport-hub")
    current = az.find("resource", "show", "--ids", resource_id)
    if current:
        out("  Deleting the MVP Durable Task task hub to remove queued and completed workflow state.")
        az("resource", "delete", "--ids", resource_id)
        for _ in range(180):
            if az.find("resource", "show", "--ids", resource_id) is None:
                break
            runtime.sleep(5)
        else:
            raise MvpError("The Durable Task task hub was not deleted within 15 minutes. The app remains offline; "
                           "rerun `python -m dbp_mvp data clean` after Azure finishes the deletion.")
    url = f"https://management.azure.com{resource_id}?api-version={DURABLE_TASK_API}"
    out("  Recreating the empty task hub; the deployment will restore its managed-identity role.")
    az.rest("put", url, body={"properties": {}})
    for _ in range(180):
        created = az.find("resource", "show", "--ids", resource_id)
        if created and (created.get("properties") or {}).get("provisioningState") in (None, "Succeeded"):
            return
        runtime.sleep(5)
    raise MvpError("The empty task hub was not ready within 15 minutes. The app remains offline; rerun "
                   "`python -m dbp_mvp data clean` after checking the scheduler in Azure.")


@contextlib.contextmanager
def _version_delete_access(config, az, outputs, operator, out):
    """Only Storage Blob Data Owner may delete previous Blob versions (deleteBlobVersion/action). The operator keeps
    Contributor, so the clean grants Owner on the app account for this step and removes it afterwards. The fixed
    assignment name lets a rerun remove a grant left behind by an interrupted clean."""
    account = outputs["appStorageName"]
    scope = (f"/subscriptions/{config.subscription_id}/resourceGroups/{config.names.resource_group}"
             f"/providers/Microsoft.Storage/storageAccounts/{account}")
    name = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{scope}/{operator}/data-clean-blob-versions"))
    held = az("role", "assignment", "list", "--assignee", operator, "--scope", scope, "--role", BLOB_DATA_OWNER,
              "--include-inherited", "--include-groups", "--query", "[].{id:id, name:name}") or []
    temporary = [item["id"] for item in held if item.get("name") == name]
    if not held:
        out(f"  Granting you {BLOB_DATA_OWNER} on {account} until its previous Blob versions are deleted.")
        temporary.append(az("role", "assignment", "create", "--name", name, "--assignee-object-id", operator,
                            "--assignee-principal-type", "User", "--role", BLOB_DATA_OWNER, "--scope", scope,
                            "--query", "id"))
    try:
        yield
    finally:
        for assignment in temporary:
            try:
                az("role", "assignment", "delete", "--ids", assignment)
                out(f"  Removed your temporary {BLOB_DATA_OWNER} role on {account}.")
            except AzError as error:
                out(f"  warning: could not remove your temporary {BLOB_DATA_OWNER} role on {account}: {error}\n"
                    f"  Remove it with: az role assignment delete --ids {assignment}")


def _version_denied(error):
    return getattr(error, "status_code", None) == 403 or getattr(error, "error_code", None) in VERSION_DENIED


def _clear_app_blobs(config, az, outputs, operator, runtime, out):
    """Delete the current objects with their snapshots, then every previous version, of the generated containers."""
    from azure.core.exceptions import HttpResponseError

    def previous(container):
        return [blob for blob in container.list_blobs(include=["versions"])
                if blob.version_id and not blob.is_current_version]

    def delete_current(container):
        for blob_name in container.list_blob_names():
            container.delete_blob(blob_name, delete_snapshots="include")
        return list(container.list_blob_names()), previous(container)

    def delete_previous(container):
        for blob in previous(container):
            container.delete_blob(blob.name, version_id=blob.version_id)
        return previous(container)

    def each(name, action):
        with runtime.container(outputs["appStorageUrl"], name) as container:
            return action(container)

    pending = []
    for name in APP_BLOB_CONTAINERS:
        try:
            remaining, versions = each(name, delete_current)
        except HttpResponseError as error:
            raise MvpError(f"Could not clear app Blob container {name}: {error}") from error
        if remaining:
            raise MvpError(f"App Blob container {name} still has {len(remaining)} active object(s); "
                           "the clean reset is incomplete.")
        if versions:
            pending.append(name)
        else:
            out(f"  Cleared current objects and versions from app Blob container {name}.")
    if not pending:
        return
    with _version_delete_access(config, az, outputs, operator, out):
        for name in pending:
            for attempt in range(VERSION_ATTEMPTS):
                try:
                    versions = each(name, delete_previous)
                    break
                except HttpResponseError as error:
                    if not _version_denied(error):
                        raise MvpError(f"Could not delete previous versions in app Blob container {name}: "
                                       f"{error}") from error
                    if attempt == VERSION_ATTEMPTS - 1:
                        raise MvpError(f"Azure still refuses to delete previous versions in app Blob container "
                                       f"{name} after ten minutes. That needs {BLOB_DATA_OWNER}, which the clean "
                                       f"grants you on {outputs['appStorageName']} while it runs; check that role "
                                       "assignment, then rerun `python -m dbp_mvp data clean`.") from error
                    out(f"  Waiting for the {BLOB_DATA_OWNER} role to take effect ...")
                    runtime.sleep(30)
            if versions:
                raise MvpError(f"App Blob container {name} still has {len(versions)} previous version(s); "
                               "the clean reset is incomplete.")
            out(f"  Cleared current objects and versions from app Blob container {name}.")


def _clear_local_generated_data(output):
    output = Path(output)
    if output.is_symlink() or output.is_junction():
        raise MvpError(f"Refusing to clear a linked output directory: {output}")
    output = output.resolve()
    if output.name != "output":
        raise MvpError(f"Refusing to clear generated files outside an output directory: {output}")
    for name in ("series", "runs", "exports", "evaluation", "credential_negatives"):
        target = output / name
        if target.is_symlink() or target.is_junction():
            raise MvpError(f"Refusing to remove a linked generated-data directory: {target}")
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            raise MvpError(f"Expected a generated-data directory, found a file: {target}")


def data_clean(config, az, state, yes=False, runtime=None, confirm=input, out=print):
    """Reset active business data across SQL, app Blob, Fabric and the Durable Task hub, then create a fresh run."""
    runtime = runtime or Runtime()
    operator = preflight.session_check(config, az)["operator_object_id"]
    state.bind(config.identity)
    outputs = infrastructure(config, az)
    registration = state.outputs("B1")
    if not registration.get("app_id"):
        raise MvpError("This machine has no deployed app registration recorded. Run deploy on the developer VM "
                       "before the full data clean.")
    group = az("group", "show", "--name", config.names.resource_group)
    if (group.get("tags") or {}).get("project") != PROJECT_TAG:
        raise MvpError(f"Resource group {config.names.resource_group} is not marked as created by this MVP; "
                       "refusing to clean it.")

    marker = Path(runtime.output).resolve() / "data-clean.json"
    resumed = False
    if marker.exists():
        try:
            progress = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise MvpError(f"Cannot read clean-reset progress at {marker}: {error}") from error
        if progress.get("identity") != config.identity:
            raise MvpError(f"Clean-reset progress at {marker} belongs to another environment; refusing to continue.")
        resumed = True
    elif not state.outputs("B3").get("appUrl"):
        raise MvpError("No deployed app URL is recorded. Complete deploy before starting a full data clean.")

    confirmation = f"{config.prefix}-data-clean"
    out("This clean reset replaces the SQL database, removes current app evidence/passport/package blobs and their "
        "previous versions, clears MVP Fabric data files and table rows, replaces the Durable Task hub, and deletes "
        "local generated run artifacts. It then redeploys and imports a fresh main-series package. Infrastructure, "
        "workspace items, app settings, bootstrap state and signing keys remain; Stage B phase records are rebuilt. "
        f"Deleting Blob versions needs {BLOB_DATA_OWNER}: you get it on the app storage account only for that step. "
        "Blob soft-deleted versions, SQL backups, Delta history, Foundry service history and Azure monitoring logs "
        "remain under their retention policies.")
    if not yes and confirm(f"Type {confirmation} to confirm: ").strip() != confirmation:
        raise MvpError("Cancelled; no data was deleted.")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"identity": config.identity, "started_utc": datetime.now(timezone.utc).isoformat()},
                                 indent=2) + "\n", encoding="utf-8")
    out("Resuming an interrupted clean reset." if resumed else "Starting the full clean reset.")

    _deactivate_app(config, az, runtime, out)
    _reset_task_hub(config, az, runtime, out)
    _clear_app_blobs(config, az, outputs, operator, runtime, out)
    if outputs.get("fabricCapacityId"):
        supply.clear_fabric_data(config, az, outputs, send=runtime.fabric_send, sleep=runtime.sleep, out=out)
    _recreate_database(config, az, config.names.resource_group, outputs["sqlServerName"], outputs["databaseName"],
                       yes=True, out=out)
    _clear_local_generated_data(runtime.output)
    state.forget_from("B2", ORDER)
    out("Rebuilding the app, agents, a new main-series package and its evidence from the preserved configuration.")
    run(config, az, state, start="B2", runtime=runtime, out=out, new_build=True, skip_supplier_check=True)
    marker.unlink(missing_ok=True)
    if config.deploy_fabric and outputs.get("fabricCapacityId"):
        try:
            report_publish(config, az, state, runtime, out)
        except MvpError as error:
            raise MvpError(f"The clean reset and fresh app data completed, but the reporting refresh failed: {error}. "
                           "Run `python -m dbp_mvp report publish` to refresh it.") from error
    out("Clean reset complete. The active app data now contains the fresh main-series run.\n"
        "Next, start the story: python -m dbp_mvp run story (the main series).\n"
        "For a named series instead: python -m dbp_mvp run story --tag <TAG>. Tags used before the clean are "
        "free again. See docs/scenario-walkthrough.md#before-you-start.")
    return {"app": config.names.app, "database": outputs["databaseName"],
            "fabric": bool(outputs.get("fabricCapacityId"))}


def recreate_arguments(current):
    """Use platform.bicep's paid compute settings; keep the previous backup redundancy and tags."""
    arguments = ["--edition", "GeneralPurpose", "--family", "Gen5", "--capacity", "2",
                 "--compute-model", "Serverless", "--auto-pause-delay", "60", "--min-capacity", "0.5",
                 "--backup-storage-redundancy", current.get("requestedBackupStorageRedundancy") or "Local"]
    tags = current.get("tags") or {}
    if tags:
        arguments += ["--tags", *(f"{key}={value}" for key, value in sorted(tags.items()))]
    return arguments


def _command(config, az, state):
    """Commands on the developer VM use the signed-in operator and the existing deployment's outputs."""
    preflight.session_check(config, az)
    state.bind(config.identity)
    return infrastructure(config, az)


def _deployed_app(state):
    app_id, app_url = state.outputs("B1").get("app_id"), state.outputs("B3").get("appUrl")
    if not app_id or not app_url:
        raise MvpError("This machine has no deployed passport app recorded. Run python -m dbp_mvp deploy on the "
                       "developer VM first.")
    return app_id, app_url


def fabric_check(config, az, state, runtime=None, out=print):
    """python -m dbp_mvp fabric check: read-only look at the capacity and the workspace."""
    runtime = runtime or Runtime()
    outputs = _command(config, az, state)
    return supply.check(config, az, outputs, runtime.fabric_send, runtime.sleep, out)


def fabric_run(config, az, state, flow, new_build=False, export_id=None, runtime=None, out=print):
    """Run the requested supply, aftersales or status pipeline, resuming and pausing capacity around it."""
    runtime = runtime or Runtime()
    if flow != "status" and export_id:
        raise MvpError("--export-id belongs to fabric run status.")
    if flow != "supply" and new_build:
        raise MvpError("--new-build belongs to fabric run supply.")
    outputs = _command(config, az, state)
    supply.capacity(config, outputs)
    if flow == "aftersales":
        app_id, app_url = _deployed_app(state)
        prepared = simulator.load_state(runtime.output)
        if not prepared:
            raise MvpError("No MAIN production package is recorded. Run deploy first.")
        with runtime.container(outputs["appStorageUrl"], supply.INTAKE) as container:
            return seed_aftersales(config, az, outputs, app_url, app_id, prepared, container, runtime, out)
    if flow == "status":
        result = supply.run_status(config, az, outputs, export_id, runtime.fabric_send, runtime.sleep,
                                   runtime.sql_connect, out)
        out(f"Reporting tables rebuilt from {result['export_id']}"
            + ("." if result["views"] else "; the views or the semantic model need another run (see above)."))
        return result
    result = supply.run_supply(config, az, outputs, new_build=new_build, send=runtime.fabric_send,
                               sleep=runtime.sleep, output=runtime.output, out=out)
    if result["package_source"] != "fabric":
        raise MvpError(f"Fabric did not provide the package of {result['build_id']}: {result['reason']}. The local "
                       "package is ready; python -m dbp_mvp deploy --from B6 imports it.")
    out(f"Fabric built package {result['build_id']}. Import it into the app with python -m dbp_mvp deploy --from B6 "
        "(or upload it in the app's console).")
    return result


def report_publish(config, az, state, runtime=None, out=print):
    """python -m dbp_mvp report publish: the app's passport status exported to the Fabric reporting lakehouse."""
    runtime = runtime or Runtime()
    app_id, app_url = _deployed_app(state)
    outputs = _command(config, az, state)
    result = supply.report_publish(config, az, outputs, app_url, app_id, runtime.http_get_auth, runtime.fabric_send,
                                   runtime.sleep, runtime.sql_connect, runtime.output, out)
    out(f"Status export {result['export_id']} ({result['rows']} passports) published to the reporting lakehouse"
        + ("." if result["views"] else "; the views or the semantic model need another run (see above)."))
    return result


def roles(config, az, state, action, values=(), user=None, runtime=None, out=print):
    """python -m dbp_mvp roles assign|remove|list: the MVP's app roles of one user (default: the signed-in user)."""
    runtime = runtime or Runtime()
    if action == "list" and values:
        raise MvpError("roles list takes no role names.")
    if action != "list" and not values:
        raise MvpError(f"Name at least one role to {action}: {', '.join(identity.DECLARED_ROLES)}.")
    identity.check_roles(values)
    preflight.session_check(config, az)
    state.bind(config.identity)
    principal = state.outputs("B1").get("service_principal_id")
    if not principal:
        raise MvpError("This machine has no app registration recorded. Run python -m dbp_mvp deploy on the "
                       "developer VM first.")
    user_id, name = identity.find_user(az, user, runtime.sleep)
    if action == "list":
        _, held = identity.held_roles(az, principal, user_id, runtime.sleep)
        held = sorted(held)
    else:
        changed, held = identity.change_roles(az, principal, user_id, values, action == "remove", runtime.sleep)
        verb = "Assigned" if action == "assign" else "Removed"
        out(f"{verb} {', '.join(changed)} {'to' if action == 'assign' else 'from'} {name}." if changed
            else f"No change: {name} already {'has' if action == 'assign' else 'lacks'} {', '.join(values)}.")
        if changed:
            out("  The change applies at the next sign-in (sign out of the app, or get a new token).")
    out(f"{name} holds: {', '.join(held) or 'no passport roles'}.")
    return held


def powerbi_publish(config, az, state, runtime=None, out=print):
    """python -m dbp_mvp powerbi: the Power BI semantic model on the reporting lakehouse, then its report."""
    runtime = runtime or Runtime()
    outputs = _command(config, az, state)
    result = supply.publish_model(config, az, outputs, runtime.fabric_send, runtime.sleep, out)
    out(f"Semantic model {powerbi.MODEL} and report {powerbi.REPORT} published in workspace "
        f"{config.names.fabric_workspace}.")
    return result


def _checks(config, az, state, outputs, registration, app, agents, previous, runtime, out, skip_supplier_check=False):
    """B7. Always runs: DNS, posture, app readiness and ledger; the optional clean path skips the Supplier call."""
    with stage("B7 Checking private name resolution and network posture"):
        problems = smoke.dns_problems(smoke.private_hosts(outputs), runtime.resolve)
        problems += smoke.audit(az, config.names.resource_group, outputs)
    if problems:
        raise MvpError("Deployment checks failed:\n  - " + "\n  - ".join(problems))
    with stage("B7 Waiting for the app to become ready"):
        ready = smoke.wait_ready(app["appUrl"], runtime.http_get, runtime.sleep, runtime.clock, out)
    with stage("B7 Verifying the SQL ledger against the anchored digests"):
        connection = runtime.connect(outputs, runtime.sleep, out)
        try:
            verified = data_plane.verify_anchored(connection, outputs, runtime.container, runtime.sleep, out)
        finally:
            connection.close()
    result = {"store": ready.get("store"), "blobs": ready.get("blobs"),
              "ledger_verified_block": verified["verified_block"], "ledger_digests": verified["digests"]}
    if agents.get("agent_mode"):
        if skip_supplier_check:
            result.update(supplier="not run (clean reset)", telemetry="not checked")
        else:
            result.update(_supplier_check(az, state, outputs, registration, app, agents, previous, runtime, out))
    state.record("B7", fingerprint(config.identity), result)
    return result


def _supplier_check(az, state, outputs, registration, app, agents, previous, runtime, out):
    """One hosted Supplier call on a lot with only its signed claim, then its agent.run event in Application Insights.

    A repeated check reuses its task ID, so the app answers from its record without calling the agent again; a
    failed check moves to a new task ID.
    """
    task_id = f"smoke-{agents['smoke_key']}-{state.counter(SMOKE_COUNTER)}"
    with stage(f"B7 Asking the hosted Supplier agent to assess {smoke.SMOKE_LOT}"):
        try:
            outcome = smoke.supplier_smoke(az, app["appUrl"], registration["app_id"], task_id, runtime.http_post)
        except MvpError as error:
            state.bump(SMOKE_COUNTER)
            raise MvpError(f"{error}\nSee docs/deployment.md#hosted-agent-runtime-failures for the provider error "
                           "and session logs. A failed response alone does not establish a network problem.") from error
    out(f"  The Supplier agent {outcome}.")
    seen = previous.get("smoke_task") == task_id
    if seen and previous.get("telemetry") == "found":
        return {"smoke_task": task_id, "supplier": outcome, "telemetry": "found"}
    with stage("B7 Looking for the agent.run event in Application Insights"):
        found = smoke.agent_telemetry(az, outputs["applicationInsightsAppId"], runtime.sleep, runtime.clock, out,
                                      timeout=0 if seen else smoke.TELEMETRY_TIMEOUT)
    if not found:
        out("  Warning: no agent.run event of the Supplier agent reached Application Insights yet. The agents "
            "workbook stays empty until it does; continuing.")
    return {"smoke_task": task_id, "supplier": outcome, "telemetry": "found" if found else "missing"}


def _summary(config, az, state, tenant_id, outputs, registration, app, agents, data, checks, out):
    """B8. Always runs: pause Fabric and print how to use the app."""
    if config.deploy_fabric and outputs.get("fabricCapacityId"):
        with stage("B8 Pausing the Fabric capacity"):
            fabric.set_capacity(az, outputs["fabricCapacityId"], active=False)
    state.record("B8", fingerprint(config.identity), {})
    roles = ", ".join(registration.get("roles") or []) or "none"
    lines = ["", "Stage B complete. Open the passport app:",
             f"  {app['appUrl']}",
             f"  Sign in as {config.operator_upn} (app roles: {roles}).",
             "  Passport.Admin (Settings page) is not assigned automatically. An administrator can assign it in the "
             f"Microsoft Entra admin center > App registrations > {registration['display_name']} > Managed "
             "application in local directory > Users and groups.",
             f"  Data: Azure SQL ledger verified up to block {checks['ledger_verified_block']} against "
             f"{checks['ledger_digests']} anchored digests."]
    lines += _data_lines(config, *data)
    if agents.get("agent_mode"):
        versions = ", ".join(f"{role} v{version}" for role, version in agents["versions"].items())
        lines.append(f"  Agents: Foundry agents {versions}; the Supplier agent check {checks.get('supplier')}.")
        if checks.get("telemetry") == "not checked":
            lines.append("  The clean reset skipped the Supplier smoke call so it would not leave a test task in the "
                         "fresh database. Run 'python -m dbp_mvp deploy --from B7' to exercise it.")
        elif checks.get("telemetry") != "found":
            lines.append("  Warning: Application Insights has no agent.run event of the Supplier agent yet, so the "
                         "agents workbook is empty. Rerun 'python -m dbp_mvp deploy --from B7' in a few minutes.")
    links = workbook_links(tenant_id, outputs["workbookIds"])
    if links:
        lines += ["", "Monitoring workbooks (Azure portal):"]
        lines += [f"  {persona:<11} {link}" for persona, link in links.items()]
    lines += ["",
              "Passport Container App: 1 vCPU and 2 GiB per replica, up to 3 replicas.",
              ("At least one replica stays running for the workflow worker." if agents.get("agent_mode") else
               "At least one replica stays running (warm_replica = true)." if config.warm_replica else
               "The app can scale to zero when idle; the first page after a pause takes longer."),
              f"The sign-in secret expires on {app['sign_in_expires']}. Rerun 'python -m dbp_mvp deploy --from B3' "
              "before then; a normal rerun renews it when fewer than seven days remain.",
              "", f"On your laptop: python -m dbp_mvp vm stop (the VM also shuts down at {config.vm_auto_shutdown}, "
              f"{config.time_zone})."]
    out("\n".join(lines))


def _data_lines(config, supplied, seeded):
    """The synthetic data part of the summary: where the package came from and what the app imported."""
    source = (f"built in Fabric, {supplied.get('match') or 'byte-identical to the local build'}"
              if supplied.get("package_source") == "fabric" else f"the local build ({supplied.get('reason')})")
    lines = [f"  Synthetic data: package {seeded['build_id']} {seeded['import_outcome']} with {seeded['evidence']} "
             f"evidence documents; package {supplied['build_id']} came from {source}."]
    if seeded["build_id"] != supplied["build_id"]:
        lines.append("  The app already had a newer package, so B6 prepared and imported a new build locally; the "
                     "next deploy builds it in Fabric too.")
    if config.deploy_fabric:
        if receipt := seeded.get("aftersales"):
            lines.append(f"  OEM warranty and recall: Fabric delivery {receipt['delivery_id']} imported.")
        lines.append("  Fabric: python -m dbp_mvp report publish exports the passport status to the reporting "
                     "lakehouse" + (" and its semantic model." if config.powerbi else
                                    "; python -m dbp_mvp powerbi adds the semantic model."))
    return lines


def evaluation(config, az, apply=False, repeats=1, assignment=None, shield=False, runtime=None, out=print):
    """python -m dbp_mvp scenario evaluation: the fixed agent evaluation cases, written to output/evaluation.

    Without --apply this checks the scoring code against the labelled answers and the scripted double's rules
    against the same cases, with no Azure call. With
    --apply (developer VM, [scenarios] evaluation = true) every case runs against the deployed models through
    temporary prompt agents, which are deleted afterwards; the model calls are billable. With --apply --shield the
    Prompt Shields probes are screened by the deployed Content Safety endpoint instead; no model is called.
    """
    runtime = runtime or Runtime()
    module = runtime.evaluation()
    if shield and not apply:
        raise MvpError("--shield belongs to scenario evaluation with --apply.")
    if not apply:
        report = module.run_local(OUTPUT / "evaluation" / "local")
        for assignment, summary in report["summary"].items():
            out(f"  {assignment:<17} {summary['passed']}/{summary['cases']} passed, "
                f"{summary['critical_failures']} critical failures")
        out(f"Scored {len({row['id'] for row in report['rows']})} labelled cases against the expected answers and "
            f"the scripted double (checks of the scoring code and the double's rules, not of a model). Report: "
            f"{OUTPUT / 'evaluation' / 'local' / 'report.json'}")
    else:
        if not config.scenarios.get("evaluation"):
            raise MvpError("The model evaluation makes billable model calls. Set evaluation = true under "
                           "[scenarios] in config/mvp.toml first.")
        if repeats not in range(1, 6):
            raise MvpError("--repeats must be between 1 and 5.")
        preflight.session_check(config, az)
        outputs = infrastructure(config, az)
        if shield:
            folder = OUTPUT / "evaluation" / "shield"
            with stage("Screening the Prompt Shields probes (billable Content Safety calls)"):
                report = module.run_shield(foundry.project_endpoint(outputs), runtime.credential(), folder)
            summary = report["summary"]["prompt_shields"]
            out(f"  Prompt Shields: {summary['attacks_detected']}/{summary['attacks']} attacks detected, "
                f"{summary['false_positives']}/{summary['clean']} benign notes flagged, {summary['errors']} errors, "
                f"decision {summary['decision']}")
            out(f"Report: {folder / 'report.json'}")
            if summary["errors"]:
                raise MvpError("Prompt Shields calls failed; see the report. Check the Cognitive Services User "
                               "role on the Foundry account (python -m dbp_mvp deploy).")
            return report
        foundry.verify_models(az, config.names.resource_group, outputs, foundry.contracts().ROLES, runtime.resolve)
        folder = OUTPUT / "evaluation" / "models"
        with stage("Comparing three model assignments on the evaluation cases (billable)"):
            report = module.run_models(foundry.project_endpoint(outputs), runtime.credential(), folder, repeats,
                                       project_factory=lambda endpoint, _credential: runtime.project(endpoint),
                                       prices=config.prices,
                                       assignments=(assignment,) if assignment else None)
        for assignment, summary in report["summary"].items():
            out(f"  {assignment:<13} {summary['passed']}/{summary['cases']} passed, {summary['critical_failures']} "
                f"critical failures, decision {summary['decision']}")
        out(f"Report: {folder / 'report.json'}")
    if module.failed(report):
        raise MvpError("The evaluation found failures; see the report. " + (report.get("fatal_error") or ""))
    return report


def credential_negatives(config, az, state, apply=False, runtime=None, out=print):
    """python -m dbp_mvp scenario credential_negatives: forged, misaddressed, expired, revoked, unknown and replayed
    signed messages against the mock connector, written to output/credential_negatives.

    Without --apply the checks run against an in-memory app with keys generated at runtime. With --apply (developer
    VM, [scenarios] credential_negatives = true) they go to the deployed app as the operator, whose Connector role
    delivers partner messages; every refused message stays in the append-only connector log.
    """
    try:
        from scenario import credentials
    except ImportError as error:
        raise MvpError(f"scenario credential_negatives needs the app's packages ({error.name} is missing). Install "
                       "them with python -m pip install -r requirements.txt.") from None
    if not apply:
        folder = OUTPUT / "credential_negatives" / "local"
        report = credentials.run_local(folder, out=out)
    else:
        if not config.scenarios.get("credential_negatives"):
            raise MvpError("The deployed credential checks write refused messages permanently into the app's "
                           "append-only connector log. Set credential_negatives = true under [scenarios] in "
                           "config/mvp.toml first.")
        from . import acts
        runtime = runtime or Runtime()
        preflight.session_check(config, az)
        app_id, app_url = _deployed_app(state)
        if not simulator.load_state(runtime.output):
            raise MvpError("The main series is not imported. Run python -m dbp_mvp deploy first (stage B6).")
        series = acts._series(config, None, runtime.output)
        folder = OUTPUT / "credential_negatives" / "deployed"
        report = credentials.run(acts.Http(app_url, acts.Token(az, app_id), runtime), series,
                                 simulator.signing_keys(runtime.output), "deployed", folder, out=out)
    out(f"{report['passed']} of {len(report['checks'])} credential checks passed"
        f"{', ' + str(report['not_exercised']) + ' not exercised' if report['not_exercised'] else ''} "
        f"(mock connector, synthetic messages). Report: {folder / 'report.json'}")
    if report["verdict"] != "PASS":
        raise MvpError("A credential check did not get the expected answer; see the report.")
    return report


def run(config, az, state, start=None, skip_build=False, image=None, runtime=None, out=print, new_build=False,
        skip_supplier_check=False):
    runtime = runtime or Runtime()
    if image and skip_build:
        raise MvpError("Use either --image or --skip-build, not both.")
    session = preflight.session_check(config, az)
    state.bind(config.identity)
    previous_image = state.outputs("B3").get("image")  # read before --from forgets B3
    extras = state.outputs("B4").get("app_extra")  # agent settings a redeployed app keeps
    previous_checks = state.outputs("B7")
    if skip_build and not previous_image:
        raise MvpError("--skip-build reuses the image of an earlier deployment from this machine, and none is "
                       "recorded. Run deploy without --skip-build.")
    if start:
        state.forget_from(start, ORDER)
    outputs = infrastructure(config, az)
    if image:
        image = images.check_image(image, outputs["registryLoginServer"])
    elif skip_build:
        image = previous_image
    registration = _registration(config, az, state, session, outputs, runtime, out)
    _database(config, state, outputs, runtime, out)
    app = _app(config, az, state, session, outputs, registration, runtime, image, extras, out)
    app = _agents(config, az, state, session, outputs, registration, app, runtime, out)
    agents = state.outputs("B4")
    supplied = _supply(config, az, state, outputs, runtime, out, new_build=new_build)
    with stage("Waiting for the deployed passport revision to serve traffic before B6 and B7"):
        _wait_app_revision(config, az, None, app["image"], runtime, out, retry_command="python -m dbp_mvp deploy")
    seeded = _seed(config, az, state, outputs, registration, app, runtime, out)
    checks = _checks(config, az, state, outputs, registration, app, agents, previous_checks, runtime, out,
                     skip_supplier_check=skip_supplier_check)
    _summary(config, az, state, session["tenant_id"], outputs, registration, app, agents, (supplied, seeded), checks,
             out)
