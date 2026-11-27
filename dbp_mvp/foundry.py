"""B4 building blocks: the five Foundry agents, the hosted agents' access to the app and the app's agent settings.

Supplier and Lifecycle are hosted agents built remotely by Foundry from agents/hosted and a copy of the app's
contracts; Carbon, Issuance and Watch are prompt agents. All are created with the operator's Azure CLI sign-in
through the Foundry SDK (azure-ai-projects). A new agent version is created only when its source or definition
changed, recognised by hashes stored in the version's metadata.
"""

import base64
import hashlib
import importlib.util
import io
import json
import re
import stat
import time
import zipfile
from collections.abc import Mapping
from urllib.parse import urlsplit

from . import MvpError, data_plane, identity, smoke
from .az import AzError
from .config import MARKER, ROOT
from .state import fingerprint

HOSTED_SOURCE = ROOT / "agents" / "hosted"
EVALUATION = ROOT / "agents" / "evaluation" / "evaluate.py"
HOSTED_FILES = ("main.py", "requirements.txt")
CONTRACT_FILES = ("domain.py", "agent_contracts.py")
PACKAGE_INIT = b'"""Passport contracts copied into the hosted agent package by python -m dbp_mvp deploy."""\n'
ZIP_DATE = (1980, 1, 1, 0, 0, 0)
PROTOCOLS = {"supplier": ("responses", "openai/responses"), "lifecycle": ("invocations", "invocations")}
HOSTED = {"cpu": "0.5", "memory": "1Gi", "protocol_version": "2.0.0", "runtime": "python_3_14",
          "entry_point": ["python", "main.py"], "dependency_resolution": "remote_build"}
FOUNDRY_USER = "53ca6127-db72-4b80-b1b0-d745d6d5456d"  # built-in role "Azure AI User", shown as Foundry User
AGENT_ROLE_DESCRIPTION = "Reads the pinned task sections for this agent role only."
POLL_INTERVAL = 20
POLL_TIMEOUT = 20 * 60
UUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
BUILD_HINT = ("See docs/deployment.md#hosted-agent-build-failures-and-logs to inspect this version and its uploaded "
              "ZIP before retrying. Container logs require a started session.")


def contracts():
    """The app's agent contracts (app/passport/agent_contracts.py): roles, models, instructions and tools."""
    return data_plane.app_module("agent_contracts")


def project_client(endpoint):
    from azure.ai.projects import AIProjectClient
    from azure.identity import AzureCliCredential
    return AIProjectClient(endpoint=endpoint, credential=AzureCliCredential())


def sdk_models():
    from azure.ai.projects import models
    return models


def cli_credential():
    from azure.identity import AzureCliCredential
    return AzureCliCredential()


def evaluation_module():
    """agents/evaluation/evaluate.py, which is a script folder rather than a package."""
    spec = importlib.util.spec_from_file_location("dbp_mvp_evaluation", EVALUATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def agent_name(role):
    return f"dbp-{role}"


def project_endpoint(outputs):
    endpoint = (outputs.get("foundryProjectEndpoint") or "").rstrip("/")
    parts = urlsplit(endpoint)
    if (parts.scheme != "https" or not (parts.hostname or "").endswith(".services.ai.azure.com")
            or not parts.path.startswith("/api/projects/") or parts.query or parts.fragment):
        raise MvpError(f"The Foundry project endpoint {endpoint!r} is not an https://*.services.ai.azure.com/api/"
                       "projects/<name> address. Rerun the bootstrap --from A1 on your laptop.")
    return endpoint


def hosted_endpoint(endpoint, role):
    """The documented address of a hosted agent's protocol; the app checks it belongs to this project and agent."""
    return f"{endpoint}/agents/{agent_name(role)}/endpoint/protocols/{PROTOCOLS[role][1]}?api-version=v1"


def package():
    """The hosted agent zip and a hash of its content. Identical sources give identical bytes on any checkout."""
    files = {name: (HOSTED_SOURCE / name).read_bytes() for name in HOSTED_FILES}
    files["passport/__init__.py"] = PACKAGE_INIT
    files.update({f"passport/{name}": (data_plane.APP / "passport" / name).read_bytes() for name in CONTRACT_FILES})
    files = {name: content.replace(b"\r\n", b"\n") for name, content in files.items()}
    digest = hashlib.sha256()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in sorted(files):
            digest.update(name.encode("utf-8") + b"\0" + hashlib.sha256(files[name]).digest())
            info = zipfile.ZipInfo(name, ZIP_DATE)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, files[name])
    return buffer.getvalue(), digest.hexdigest()


def hosted_environment(role, model, app_fqdn, app_id):
    """Settings of a hosted agent. Foundry injects FOUNDRY_PROJECT_ENDPOINT itself."""
    return {"AGENT_ROLE": role, "AZURE_AI_MODEL_DEPLOYMENT_NAME": model, "PASSPORT_APP_URL": f"https://{app_fqdn}",
            "PASSPORT_APP_AUDIENCE": f"api://{app_id}", "PYTHONNOUSERSITE": "1"}


def prompt_hash(contracts_module, role):
    spec = contracts_module.ROLES[role]
    return fingerprint([spec["model"], spec["version"], contracts_module.instructions(role),
                        contracts_module.tool_definition(role)])


def _value(item):
    return getattr(item, "value", item)


def _metadata(version):
    return dict(getattr(version, "metadata", None) or {})


def _missing(error):
    return getattr(error, "status_code", None) == 404


def latest_version(project, name):
    try:
        agent = project.agents.get(agent_name=name)
    except Exception as error:  # azure.core ResourceNotFoundError; the SDK is imported lazily
        if _missing(error):
            return None
        raise
    return agent.versions.latest


def _identity(project, name, version):
    found = getattr(version, "instance_identity", None)
    if not getattr(found, "principal_id", None):
        found = getattr(project.agents.get(agent_name=name), "instance_identity", None)
    return getattr(found, "principal_id", None), getattr(found, "client_id", None)


def _wrong_environment(version, environment):
    actual = getattr(getattr(version, "definition", None), "environment_variables", None) or {}
    return sorted(key for key, value in environment.items() if actual.get(key) != value)


def _version_error(version):
    """Read the service error even when it is an unmodeled SDK JSON field."""
    error = version.get("error") if isinstance(version, Mapping) else getattr(version, "error", None)
    parts = [error.get(field) if isinstance(error, Mapping) else getattr(error, field, None)
             for field in ("code", "message")]
    return ": ".join(str(_value(part)) for part in parts if part) or "Foundry returned no error details."


def wait_active(project, name, version, sleep=time.sleep, clock=time.monotonic, out=print):
    """Poll a hosted version until it is active and has its Microsoft Entra agent identity."""
    deadline = clock() + POLL_TIMEOUT
    number = str(version.version)
    while True:
        status = _value(getattr(version, "status", None))
        if status in ("failed", "deleting", "deleted"):
            raise MvpError(f"Foundry reports {name} version {number} as {status}.\n{_version_error(version)}\n"
                           f"{BUILD_HINT}")
        if status in (None, "active"):
            principal, client = _identity(project, name, version)
            if principal:
                return version, principal, client
        if clock() >= deadline:
            raise MvpError(f"{name} version {number} was not ready after {POLL_TIMEOUT // 60} minutes (status "
                           f"{status}).\n{_version_error(version)}\n{BUILD_HINT}")
        out(f"  Waiting for {name} version {number} (status {status}) ...")
        sleep(POLL_INTERVAL)
        version = project.agents.get_version(agent_name=name, agent_version=number)


def ensure_hosted(project, models, role, environment, code, source_sha, sleep=time.sleep, clock=time.monotonic,
                  out=print):
    """Reuse the latest hosted version built from the same source and settings, or upload a new one."""
    name = agent_name(role)
    config_sha = fingerprint({"environment": environment, "protocol": PROTOCOLS[role][0], **HOSTED})
    latest = latest_version(project, name)
    metadata = _metadata(latest) if latest else {}
    if (latest and metadata.get("source_sha256") == source_sha and metadata.get("config_sha256") == config_sha
            and _value(getattr(latest, "status", None)) in (None, "active", "creating")
            and not _wrong_environment(latest, environment)):
        version = latest
    else:
        definition = models.HostedAgentDefinition(
            cpu=HOSTED["cpu"], memory=HOSTED["memory"], environment_variables=environment,
            protocol_versions=[models.ProtocolVersionRecord(protocol=PROTOCOLS[role][0],
                                                            version=HOSTED["protocol_version"])],
            code_configuration=models.CodeConfiguration(runtime=HOSTED["runtime"], entry_point=HOSTED["entry_point"],
                                                        dependency_resolution=HOSTED["dependency_resolution"]))
        stream = io.BytesIO(code)
        stream.name = "agent.zip"
        out(f"  Uploading a new version of {name}; Foundry builds it remotely (several minutes).")
        version = project.agents.create_version_from_code(
            name, definition=definition, code=stream, code_zip_sha256=hashlib.sha256(code).hexdigest(),
            description=f"Synthetic battery passport {role} agent; the passport app owns every state change.",
            metadata={"source_sha256": source_sha, "config_sha256": config_sha, "managed_by": MARKER})
    version, principal, client = wait_active(project, name, version, sleep, clock, out)
    kind = _value(getattr(getattr(version, "definition", None), "kind", None))
    if kind != "hosted":
        raise MvpError(f"Foundry agent {name} is a {kind} agent, not a hosted one. Delete it in the Foundry portal "
                       "and rerun 'python -m dbp_mvp deploy --from B4'.")
    wrong = _wrong_environment(version, environment)
    if wrong:
        raise MvpError(f"{name} version {version.version} reports different settings ({', '.join(wrong)}) than "
                       "were uploaded. Rerun 'python -m dbp_mvp deploy --from B4'.")
    return {"name": name, "version": str(version.version), "principal_id": principal, "client_id": client,
            "endpoint": None}


def ensure_prompt(project, models, contracts_module, role):
    """Reuse the latest prompt version with the same model, instructions and tool, or create a new one."""
    name = agent_name(role)
    digest = prompt_hash(contracts_module, role)
    latest = latest_version(project, name)
    if latest and _metadata(latest).get("prompt_sha256") == digest:
        return {"name": name, "version": str(latest.version)}
    spec = contracts_module.ROLES[role]
    definition = models.PromptAgentDefinition(
        model=spec["model"], instructions=contracts_module.instructions(role),
        tools=[models.FunctionTool(**contracts_module.tool_definition(role))])
    created = project.agents.create_version(
        agent_name=name, definition=definition, metadata={"prompt_sha256": digest, "managed_by": MARKER},
        description=f"Synthetic battery passport {role} agent; the passport app owns every state change.")
    return {"name": name, "version": str(created.version)}


def authorize(az, registration, role, principal_id, client_id=None, sleep=time.sleep):
    """Give a hosted agent's identity its one app role on the passport app. Returns the identity's client ID."""
    if not UUID.fullmatch(principal_id or ""):
        raise MvpError(f"Foundry returned an agent identity {principal_id!r} that is not an object ID.")
    value = f"Passport.Agent.{role.title()}"
    path = f"/applications/{registration['object_id']}"
    roles = (identity.graph(az, "get", path, parameters={"$select": "appRoles"}, sleep=sleep) or {}).get(
        "appRoles") or []
    existing = next((item for item in roles if item.get("value") == value), None)
    if existing and existing.get("allowedMemberTypes") != ["Application"]:
        raise MvpError(f"The app role {value} exists on {registration['display_name']} for other members than "
                       "applications. Remove it in Microsoft Entra ID (App registrations > App roles), then rerun.")
    role_id = existing["id"] if existing else identity.stable_id(registration["app_id"], value, roles)
    if not existing:
        try:
            identity.graph(az, "patch", path, {"appRoles": [*roles, {
                "id": role_id, "value": value, "displayName": value, "description": AGENT_ROLE_DESCRIPTION,
                "allowedMemberTypes": ["Application"], "isEnabled": True}]}, sleep=sleep)
        except AzError as error:
            if identity.denied(error):
                raise identity.refusal(f"add the app role {value} to {registration['display_name']}", "B4") from None
            raise
    assignments = f"/servicePrincipals/{principal_id}/appRoleAssignments"
    held = (identity.graph(az, "get", assignments, sleep=sleep) or {}).get("value", [])
    if not any(item.get("appRoleId") == role_id and item.get("resourceId") == registration["service_principal_id"]
               for item in held):
        try:
            identity.graph(az, "post", assignments, {"principalId": principal_id,
                                                     "resourceId": registration["service_principal_id"],
                                                     "appRoleId": role_id}, sleep=sleep)
        except AzError as error:
            if identity.denied(error):
                raise identity.refusal(f"grant {value} to the {role} agent identity", "B4") from None
            raise
    found = identity.graph(az, "get", f"/servicePrincipals/{principal_id}", parameters={"$select": "appId"},
                           sleep=sleep) or {}
    app_id = found.get("appId") or ""
    if not UUID.fullmatch(app_id):
        raise MvpError(f"Microsoft Entra ID returned no client ID for the {role} agent identity {principal_id}.")
    if client_id and client_id.lower() != app_id.lower():
        raise MvpError(f"Microsoft Entra ID and Foundry disagree about the {role} agent identity ({app_id} versus "
                       f"{client_id}). Inspect it in the Foundry portal before rerunning.")
    return app_id.lower()


def verify_models(az, resource_group, outputs, roles, resolve):
    """Foundry is private, injected into the agents subnet, and serves the exact model versions the roles need."""
    account = outputs["foundryAccountName"]
    properties = (az("cognitiveservices", "account", "show", "--resource-group", resource_group, "--name",
                     account) or {}).get("properties") or {}
    problems = []
    if properties.get("provisioningState") != "Succeeded":
        problems.append(f"{account} is {properties.get('provisioningState')}, not Succeeded")
    if properties.get("publicNetworkAccess") != "Disabled":
        problems.append(f"{account} has public network access {properties.get('publicNetworkAccess')}, "
                        "expected Disabled")
    subnet = outputs["agentsSubnetId"].lower()
    if not any(item.get("scenario") == "agent" and (item.get("subnetArmId") or "").lower() == subnet
               and item.get("useMicrosoftManagedNetwork") is False
               for item in properties.get("networkInjections") or []):
        problems.append(f"{account} has no agent network injection into {outputs['agentsSubnetId']}")
    deployed = {item.get("name"): item.get("properties") or {} for item in
                az("cognitiveservices", "account", "deployment", "list", "--resource-group", resource_group,
                   "--name", account) or []}
    for model, version in sorted({(spec["model"], spec["version"]) for spec in roles.values()}):
        found = deployed.get(model, {})
        actual = found.get("model") or {}
        if actual.get("name") != model or actual.get("version") != version:
            problems.append(f"model deployment {model} must serve {model} version {version} (found "
                            f"{actual.get('name')} {actual.get('version')}); check [[models]] in config/mvp.toml")
        elif found.get("provisioningState") != "Succeeded":
            problems.append(f"model deployment {model} is {found.get('provisioningState')}, not Succeeded")
    problems += smoke.dns_problems([urlsplit(outputs["foundryProjectEndpoint"]).hostname], resolve)
    if problems:
        raise MvpError("Foundry is not ready for the agents:\n  - " + "\n  - ".join(problems))


def grant_project(az, outputs):
    """The project's identity calls the models for the hosted agents. Returns True when the role was added now."""
    principal, scope = outputs["foundryProjectPrincipalId"], outputs["foundryAccountId"]
    if az("role", "assignment", "list", "--assignee-object-id", principal, "--scope", scope, "--role", FOUNDRY_USER,
          "--query", "[].id"):
        return False
    az("role", "assignment", "create", "--assignee-object-id", principal, "--assignee-principal-type",
       "ServicePrincipal", "--role", FOUNDRY_USER, "--scope", scope)
    return True


def app_config(endpoint, roles, agents):
    """DBP_FOUNDRY_CONFIG_B64 for the app: which agent version and address serves each role."""
    config = {"project_endpoint": endpoint, "roles": {}}
    for role, spec in roles.items():
        entry = {"kind": spec["kind"], "name": agents[role]["name"], "version": agents[role]["version"],
                 "model": spec["model"], "model_version": spec["version"]}
        if spec["kind"] == "hosted":
            entry.update(principal_id=agents[role]["principal_id"], endpoint=hosted_endpoint(endpoint, role))
        config["roles"][role] = entry
    return config


def app_extras(config, client_ids):
    """Extra app.bicep parameters for agent mode: the agent settings and the hosted identities Easy Auth admits."""
    encoded = base64.b64encode(json.dumps(config, sort_keys=True).encode("utf-8")).decode("ascii")
    return {"foundryConfigB64": encoded, "extraAllowedClientIds": sorted(set(client_ids))}
