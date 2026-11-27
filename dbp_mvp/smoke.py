"""Checks after a deployment: private name resolution, network posture, app readiness and one agent call."""

import ipaddress
import json
import socket
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlsplit

from . import MvpError
from .image import registry_problem, show_registry

PRIVATE = [ipaddress.ip_network(block) for block in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
PRIVATE_ONLY = {  # output with the resource name -> Azure resource type
    "sqlServerName": "Microsoft.Sql/servers",
    "appStorageName": "Microsoft.Storage/storageAccounts",
    "digestStorageName": "Microsoft.Storage/storageAccounts",
    "foundryAccountName": "Microsoft.CognitiveServices/accounts",
}
READY_TIMEOUT = 300
READY_INTERVAL = 15
# A critical-material lot of the main series whose only evidence, after B6, is its consistent signed claim: a correct
# Supplier agent finds no issue, which is also the lot's expected assessment in the story.
SMOKE_LOT = "LOT-GR-A"
TELEMETRY_TIMEOUT = 600
TELEMETRY_INTERVAL = 30
TELEMETRY_QUERY = ('customEvents | where cloud_RoleName == "passport-app" and name == "agent.run" '
                   'and tostring(customDimensions.role) == "supplier" | take 1')


def private_hosts(outputs):
    """Names the developer VM must reach through private endpoints."""
    hosts = [outputs["sqlServerFqdn"]]
    hosts += [urlsplit(outputs[key]).hostname for key in ("appStorageUrl", "digestStorageUrl",
                                                          "foundryProjectEndpoint")]
    return [host for host in hosts if host]


def is_private(address):
    try:
        address = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    return any(address in block for block in PRIVATE)


def dns_problems(hosts, resolve=socket.getaddrinfo):
    problems = []
    for host in hosts:
        try:
            addresses = sorted({item[4][0] for item in resolve(host, 443, proto=socket.IPPROTO_TCP)})
        except socket.gaierror as error:
            problems.append(f"{host} does not resolve ({error}); run this on the developer VM, which uses the "
                            "private DNS zones")
            continue
        public = [address for address in addresses if not is_private(address)]
        if public or not addresses:
            problems.append(f"{host} resolves to {', '.join(addresses) or 'nothing'}, not only private addresses; "
                            "check the private DNS zone links of the network")
    return problems


def audit(az, resource_group, outputs):
    """Every data store and Foundry are private; the registry and app ingress are the documented exceptions."""
    problems = []
    for key, kind in PRIVATE_ONLY.items():
        access = az("resource", "show", "--resource-group", resource_group, "--name", outputs[key],
                    "--resource-type", kind, "--query", "properties.publicNetworkAccess")
        if access != "Disabled":
            problems.append(f"{outputs[key]} ({kind}) has public network access {access}, expected Disabled")
    problem = registry_problem(show_registry(az, resource_group, outputs["registryName"]))
    if problem:
        problems.append(problem)
    logs = az("monitor", "log-analytics", "workspace", "show", "--resource-group", resource_group,
              "--workspace-name", outputs["logsName"], "--query",
              "{ingestion:publicNetworkAccessForIngestion, query:publicNetworkAccessForQuery}") or {}
    if logs.get("ingestion") != "Enabled" or logs.get("query") != "Enabled":
        problems.append(f"Log Analytics workspace {outputs['logsName']} must accept public ingestion and queries "
                        f"(found {logs.get('ingestion')}/{logs.get('query')})")
    environment = az("resource", "show", "--resource-group", resource_group, "--name", outputs["environmentName"],
                     "--resource-type", "Microsoft.App/managedEnvironments", "--query",
                     "{access:properties.publicNetworkAccess, internal:properties.vnetConfiguration.internal, "
                     "subnet:properties.vnetConfiguration.infrastructureSubnetId}") or {}
    expected_subnet = outputs["agentsSubnetId"].rsplit("/subnets/", 1)[0] + "/subnets/apps"
    if (environment.get("access") != "Enabled" or environment.get("internal") is not False
            or (environment.get("subnet") or "").lower() != expected_subnet.lower()):
        problems.append(f"Container Apps environment {outputs['environmentName']} must be external with public "
                        f"access in the apps subnet (found access {environment.get('access')}, internal "
                        f"{environment.get('internal')}, subnet {environment.get('subnet')})")
    endpoints = az("network", "private-endpoint", "list", "--resource-group", resource_group, "--query",
                   "[].{name:name, states:privateLinkServiceConnections[].privateLinkServiceConnectionState.status}")
    if not endpoints:
        problems.append("no private endpoints found in the resource group")
    for endpoint in endpoints or []:
        if not endpoint.get("states") or any(status != "Approved" for status in endpoint["states"]):
            problems.append(f"private endpoint {endpoint.get('name')} is not approved ({endpoint.get('states')})")
    return problems


def _open(request, timeout):
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return None, str(error)


def http_get(url, timeout=30):
    """(status, body text) without following the sign-in redirect."""
    return _open(url, timeout)


def http_post(url, body, token, timeout=150):
    """(status, body text) of a JSON POST with a bearer token."""
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"})
    return _open(request, timeout)


def http_get_auth(url, token, timeout=60):
    """(status, body text) of a GET with a bearer token."""
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    return _open(request, timeout)


def http_post_multipart(url, fields, file, token, timeout=150):
    """(status, body text) of a multipart/form-data POST; file is (field, file name, content type, bytes)."""
    boundary = f"----dbp-mvp-{uuid.uuid4().hex}"
    lines = []
    for name, value in fields.items():
        lines += [f"--{boundary}".encode(), f'Content-Disposition: form-data; name="{name}"'.encode(), b"",
                  str(value).encode("utf-8")]
    field, file_name, content_type, data = file
    lines += [f"--{boundary}".encode(),
              f'Content-Disposition: form-data; name="{field}"; filename="{file_name}"'.encode(),
              f"Content-Type: {content_type}".encode(), b"", data, f"--{boundary}--".encode(), b""]
    request = urllib.request.Request(url, data=b"\r\n".join(lines), method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Accept": "application/json"})
    return _open(request, timeout)


def app_token(az, app_id):
    """The operator's access token for the passport app's API."""
    return az.text("account", "get-access-token", "--scope", f"api://{app_id}/user_impersonation", "--query",
                   "accessToken", "--output", "tsv")


def supplier_smoke(az, app_url, app_id, task_id, post=http_post):
    """Ask the hosted Supplier agent, through the app, to assess a critical lot that has only its signed claim.

    A cited no_issue, a deferral, or the app refusing an uncited decision shows the whole path works: the operator's
    token, the app, the hosted agent, its model and its call back to the app for the task sections. Returns the
    outcome text. A review or compliance escalation on the claim alone is a wrong judgement and fails the check.
    """
    token = app_token(az, app_id)
    status, text = post(f"{app_url.rstrip('/')}/agents/supplier/run", {"task_id": task_id, "subject": SMOKE_LOT},
                        token)
    try:
        body = json.loads(text) if text else {}
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    if status == 200 and (body.get("outcome") or {}).get("status") == "deferred":
        return "deferred"
    action = (body.get("decision") or {}).get("action") if status == 200 else None
    if action == "no_issue":
        return "found no issue"
    if status == 422 and body.get("error") == "citation_required":
        return "refused an uncited decision"
    if action:
        raise MvpError(f"The Supplier agent check (task {task_id}) answered {action} for {SMOKE_LOT}, whose only "
                       "evidence is its signed claim. Read the agent's reason in the app before you rerun deploy.")
    detail = body.get("error") or (text or "")[:200]
    raise MvpError(f"The Supplier agent check (task {task_id}) answered {status}: {detail} "
                   f"{body.get('message') or ''}".rstrip())


def agent_telemetry(az, app_insights_id, sleep=time.sleep, clock=time.monotonic, out=print,
                    timeout=TELEMETRY_TIMEOUT):
    """Whether Application Insights has an agent.run event of the Supplier from the last day. Never raises."""
    deadline = clock() + timeout
    while True:
        try:
            result = az.rest("post", f"https://api.applicationinsights.io/v1/apps/{app_insights_id}/query",
                             body={"query": TELEMETRY_QUERY, "timespan": "P1D"},
                             resource="https://api.applicationinsights.io") or {}
            if any(table.get("rows") for table in result.get("tables") or []):
                return True
        except MvpError as error:
            reason = (getattr(error, "stderr", None) or str(error)).strip().splitlines() or ["no error output"]
            out(f"  Application Insights query failed ({reason[-1][:300]}).")
        if clock() >= deadline:
            return False
        out("  Waiting for the agent.run event in Application Insights (ingestion takes a few minutes) ...")
        sleep(TELEMETRY_INTERVAL)


def wait_ready(app_url, get=http_get, sleep=time.sleep, clock=time.monotonic, out=print,
               timeout=READY_TIMEOUT, retry_command="python -m dbp_mvp deploy --from B7"):
    """Poll /health/ready until the app uses Azure SQL and Blob storage; a new revision needs a few minutes."""
    url = f"{app_url.rstrip('/')}/health/ready"
    deadline = clock() + timeout
    while True:
        status, body = get(url)
        try:
            result = json.loads(body) if body else {}
        except ValueError:
            result = {}
        if status == 200 and result.get("status") == "ready":
            if result.get("store") != "sql" or result.get("blobs") != "azure":
                raise MvpError(f"The app is running on local storage (store {result.get('store')}, blobs "
                               f"{result.get('blobs')}), not Azure SQL and Blob storage. Check its settings.")
            return result
        if clock() >= deadline:
            raise MvpError(f"The app was not ready after {timeout // 60} minutes ({url} answered {status}: "
                           f"{(body or '')[:200]}). Read its log stream in the Azure portal (Container App > "
                           f"Monitoring > Log stream), then rerun {retry_command}.")
        out(f"  Waiting for the app to become ready (answered {status}) ...")
        sleep(READY_INTERVAL)
