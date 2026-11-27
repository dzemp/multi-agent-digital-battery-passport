"""Microsoft Fabric: capacity power state (Azure Resource Manager through az), and the workspace, lakehouses,
notebooks, pipelines and pipeline runs (Fabric REST API and OneLake, called directly with the operator's tokens).

FabricClient only sends a token to api.fabric.microsoft.com/v1/ or onelake.dfs.fabric.microsoft.com, follows no
redirects and retries throttled calls. Everything this MVP creates carries the marker in its description, so a rerun
updates only its own items and teardown deletes only its own workspace.
"""

import base64
import contextlib
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from data.pipeline import embedded_source
from data.aftersales_flow import AFTER_STEPS

from . import MvpError
from .config import MARKER, ROOT

ARM = "https://management.azure.com"
CAPACITY_API = "2023-11-01"
FABRIC = "https://api.fabric.microsoft.com"
FABRIC_HOST = "api.fabric.microsoft.com"
ONELAKE_HOST = "onelake.dfs.fabric.microsoft.com"
STORAGE = "https://storage.azure.com/"
ONELAKE_VERSION = "2023-11-03"
PAUSED = {"Paused", "Suspended"}
HERE = ROOT / "fabric"
# The measure definitions (data/metrics.py) run in the notebook too; they are not part of the package rules hash.
NOTEBOOK_FILES = (ROOT / "data" / "metrics.py", HERE / "notebooks" / "spark_store.py", HERE / "notebooks" / "steps.py")
LAKEHOUSES = ("ingest", "bronze", "silver", "gold", "reporting")
PIPELINES = {"supply_flow": ("bronze_load", "silver_normalise", "gold_package"),
             "status_flow": ("reporting_measures",), "reset_flow": ("reset_tables",),
             "aftersales_flow": tuple(AFTER_STEPS)}
WORKSPACE_DESCRIPTION = (f"Synthetic battery passport MVP ({MARKER}). Created by python -m dbp_mvp; "
                         "teardown deletes it. Fictional data only.")
ITEM_DESCRIPTION = f"Synthetic battery passport MVP ({MARKER})."
FILES_PATH = re.compile(r"Files(?:/[A-Za-z0-9_.-]+)*")
TOKEN_AGE = 25 * 60
ATTEMPTS = 5
TIMEOUT = 90
OPERATION_LIMIT = 1200
ASSIGN_LIMIT = 600
JOB_LIMIT = 3600
JOB_POLL = 20


# ---- capacity (Azure Resource Manager) ---------------------------------------------------------------------------

def capacity_state(az, capacity_id):
    return ((az.rest("get", f"{ARM}{capacity_id}?api-version={CAPACITY_API}") or {}).get("properties") or {}).get(
        "state")


def set_capacity(az, capacity_id, active, sleep=None, timeout=900):
    """Resume (active=True) or pause the capacity and wait for the change. Billing runs only while Active."""
    sleep = sleep or time.sleep
    target = {"Active"} if active else PAUSED
    state = capacity_state(az, capacity_id)
    if state in target:
        return state
    if state in (PAUSED if active else {"Active"}):
        action = "resume" if active else "suspend"
        az.rest("post", f"{ARM}{capacity_id}/{action}?api-version={CAPACITY_API}")
    waited = 0
    while waited < timeout:
        sleep(15)
        waited += 15
        state = capacity_state(az, capacity_id)
        if state in target:
            return state
    raise MvpError(f"Fabric capacity did not become {'Active' if active else 'Paused'} within "
                   f"{timeout // 60} minutes (state: {state}). Check it in the Azure portal.")


@contextlib.contextmanager
def active_capacity(az, capacity_id, sleep=None, out=print):
    """Keep the capacity Active for the block and pause it afterwards, also when the block fails."""
    try:
        out("  Resuming the Fabric capacity (billed while Active; paused again at the end) ...")
        set_capacity(az, capacity_id, active=True, sleep=sleep)
        yield
    finally:
        try:
            set_capacity(az, capacity_id, active=False, sleep=sleep)
            out("  Fabric capacity paused.")
        except MvpError as error:
            out(f"  WARNING: the Fabric capacity could not be paused and is still billed: {error}\n"
                "  Pause it in the Azure portal, or check it with python -m dbp_mvp status.")


# ---- workspace lookup used by teardown (through az rest) ---------------------------------------------------------

def find_workspace(az, name):
    parameters = None
    while True:
        try:
            page = az.rest("get", f"{FABRIC}/v1/workspaces", resource=FABRIC, parameters=parameters) or {}
        except ValueError as error:
            raise MvpError("The Fabric workspace list could not be paged; find the workspace in the Fabric "
                           "portal instead.") from error
        for workspace in page.get("value", []):
            if workspace.get("displayName") == name:
                return workspace
        token = page.get("continuationToken")
        if not token:
            return None
        parameters = {"continuationToken": token}


def owned(workspace):
    """Only workspaces this MVP created carry the marker; teardown never deletes anything else."""
    return MARKER in (workspace.get("description") or "")


def delete_workspace(az, workspace_id):
    az.rest("delete", f"{FABRIC}/v1/workspaces/{workspace_id}", resource=FABRIC)


# ---- Fabric REST and OneLake ---------------------------------------------------------------------------------------

def guid(value, what="ID"):
    try:
        return str(uuid.UUID(str(value)))
    except ValueError as error:
        raise MvpError(f"Unexpected Fabric {what}: {value!r}") from error


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a redirect becomes an error instead of carrying the token elsewhere


def send(method, url, headers, data, timeout):
    """One HTTPS call: (status, lower-case headers, body bytes). HTTP errors are returned, not raised."""
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as error:
        return error.code, {k.lower(): v for k, v in (error.headers or {}).items()}, error.read()
    except (urllib.error.URLError, OSError) as error:
        raise MvpError(f"Could not reach {urllib.parse.urlsplit(url).netloc}: {error}") from error


def _host(url):
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc.lower()
    if parts.scheme == "https" and not parts.fragment and (
            (host == FABRIC_HOST and parts.path.startswith("/v1/")) or host == ONELAKE_HOST):
        return host
    raise MvpError(f"Refusing to send a Fabric token to an unexpected address ({parts.scheme}://{parts.netloc}).")


def _delay(headers, default):
    try:
        return min(max(int(headers.get("retry-after", default)), 1), 120)
    except ValueError:
        return default


def _reason(content):
    try:
        body = json.loads(content or b"{}")
    except ValueError:
        return (content or b"")[:300].decode("utf-8", errors="replace")
    error = body.get("error") if isinstance(body.get("error"), dict) else body
    code, message = error.get("errorCode") or error.get("code"), error.get("message")
    return ": ".join(str(part) for part in (code, message) if part) or json.dumps(body)[:300]


class FabricHttpError(MvpError):
    def __init__(self, message, status, headers):
        super().__init__(message)
        self.status, self.headers = status, headers


class FabricClient:
    """Fabric REST and OneLake calls with the operator's Azure CLI tokens."""

    def __init__(self, az, send=send, sleep=None, clock=time.monotonic):
        self.az, self.send, self.clock = az, send, clock
        self.sleep = sleep or (lambda seconds: time.sleep(seconds))
        self.tokens = {}

    def token(self, resource):
        token, issued = self.tokens.get(resource, (None, 0))
        if token is None or self.clock() - issued > TOKEN_AGE:
            token = self.az.text("account", "get-access-token", "--resource", resource, "--query", "accessToken",
                                 "--output", "tsv")
            if not token:
                raise MvpError(f"The Azure CLI returned no token for {resource}; run az login again.")
            self.tokens[resource] = (token, self.clock())
        return token

    def call(self, method, url, body=None, data=None, missing_ok=False):
        """(status, headers, bytes); a 404 with missing_ok gives (404, headers, None). Retries 429."""
        host = _host(url)
        headers = {"Authorization": f"Bearer {self.token(FABRIC if host == FABRIC_HOST else STORAGE)}"}
        if host == ONELAKE_HOST:
            headers["x-ms-version"] = ONELAKE_VERSION
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        for attempt in range(ATTEMPTS):
            status, answer, content = self.send(method, url, headers, data, TIMEOUT)
            if status != 429 or attempt == ATTEMPTS - 1:
                break
            self.sleep(_delay(answer, 30))
        if status == 404 and missing_ok:
            return status, answer, None
        if not 200 <= status < 300:
            where = "OneLake" if host == ONELAKE_HOST else "Fabric"
            path = urllib.parse.urlsplit(url).path
            message = f"{where} {method} {path} failed: HTTP {status} {_reason(content)}".rstrip()
            raise FabricHttpError(message, status, answer)
        return status, answer, content

    def request(self, method, path, body=None):
        """A Fabric REST call by path (/workspaces/...) or full URL; the JSON answer is parsed."""
        url = path if path.startswith("https://") else f"{FABRIC}/v1{path}"
        status, headers, content = self.call(method, url, body=body)
        try:
            return status, headers, json.loads(content) if content else {}
        except ValueError as error:
            raise MvpError(f"Fabric {method} {urllib.parse.urlsplit(url).path} returned no JSON.") from error

    def list_all(self, path):
        items, url, first = [], f"{FABRIC}/v1{path}", f"{FABRIC}/v1{path}"
        for _ in range(100):
            _, _, page = self.request("GET", url)
            items += page.get("value", [])
            url = page.get("continuationUri")
            if not url and page.get("continuationToken"):
                url = (first + ("&" if "?" in first else "?") + "continuationToken="
                       + urllib.parse.quote(page["continuationToken"], safe=""))
            if not url:
                return items
        raise MvpError(f"Fabric kept paging {path}; check the workspace in the Fabric portal.")

    def set_description(self, workspace, item, description):
        """Record the definition digest without repeating the completed definition update on a transient error."""
        path = f"/workspaces/{workspace}/items/{item}"
        for attempt in range(ATTEMPTS):
            try:
                _, _, result = self.request("PATCH", path, {"description": description})
            except FabricHttpError as error:
                if error.status not in (500, 502, 503, 504):
                    raise
                # A server error can arrive after Fabric committed the metadata change.
                _, _, result = self.request("GET", path)
                if result.get("id") == item and result.get("description") == description:
                    return
                if attempt == ATTEMPTS - 1:
                    raise
                self.sleep(_delay(error.headers, 2 ** (attempt + 1)))
            else:
                if result.get("id") != item or result.get("description") != description:
                    raise MvpError(f"Fabric PATCH {path} did not confirm the requested item description.")
                return

    def operation(self, path, body, needs_result=True):
        """POST and, when Fabric accepts it as a long-running operation (202), wait for it."""
        status, headers, result = self.request("POST", path, body)
        if status != 202:
            return result
        if headers.get("x-ms-operation-id"):
            poll = f"{FABRIC}/v1/operations/{guid(headers['x-ms-operation-id'], 'operation ID')}"
        elif headers.get("location"):
            poll = headers["location"]
        else:
            raise MvpError(f"Fabric accepted POST {path} but gave no address to follow; check the workspace.")
        waited = 0
        while waited < OPERATION_LIMIT:
            delay = _delay(headers, 5)
            self.sleep(delay)
            waited += delay
            _, headers, state = self.request("GET", poll)
            if state.get("status") in ("Failed", "Cancelled"):
                raise MvpError(f"Fabric operation for {path} {state['status'].lower()}: "
                               f"{_reason(json.dumps(state).encode())}")
            if state.get("status") == "Succeeded":
                return self.request("GET", poll.rstrip("/") + "/result")[2] if needs_result else {}
        raise MvpError(f"Fabric operation for {path} still running after {OPERATION_LIMIT // 60} minutes.")


class OneLake:
    """Files of one workspace's lakehouses through the OneLake DFS API."""

    def __init__(self, client, workspace_id):
        self.client, self.workspace = client, guid(workspace_id, "workspace ID")

    def url(self, lakehouse_id, path):
        if not FILES_PATH.fullmatch(path) or {".", ".."} & set(path.split("/")):
            raise MvpError(f"Unexpected OneLake path: {path}")
        return f"https://{ONELAKE_HOST}/{self.workspace}/{guid(lakehouse_id, 'lakehouse ID')}/{path}"

    def put(self, lakehouse_id, path, data):
        """Create or replace one file: create, append, flush."""
        url = self.url(lakehouse_id, path)
        self.client.call("PUT", f"{url}?resource=file", data=b"")
        if data:
            self.client.call("PATCH", f"{url}?action=append&position=0", data=data)
        self.client.call("PATCH", f"{url}?action=flush&position={len(data)}", data=b"")

    def get(self, lakehouse_id, path):
        return self.client.call("GET", self.url(lakehouse_id, path), missing_ok=True)[2]

    def delete(self, lakehouse_id, path):
        """Recursively remove one validated generated-data directory; a missing directory is already clean."""
        self.client.call("DELETE", f"{self.url(lakehouse_id, path)}?recursive=true", missing_ok=True)

    def names(self, lakehouse_id, folder):
        """File and folder names directly in a folder; empty when it does not exist."""
        self.url(lakehouse_id, folder)
        query = {"resource": "filesystem", "recursive": "false",
                 "directory": f"{guid(lakehouse_id, 'lakehouse ID')}/{folder}"}
        names, continuation = [], None
        for _ in range(100):
            if continuation:
                query["continuation"] = continuation
            _, headers, content = self.client.call(
                "GET", f"https://{ONELAKE_HOST}/{self.workspace}?{urllib.parse.urlencode(query)}", missing_ok=True)
            if content is None:
                return names
            names += [entry["name"].rsplit("/", 1)[-1] for entry in json.loads(content).get("paths", [])]
            continuation = headers.get("x-ms-continuation")
            if not continuation:
                return sorted(names)
        raise MvpError(f"OneLake kept paging {folder}.")


# ---- workspace and items ----------------------------------------------------------------------------------------

def capacity_guid(client, capacity_id):
    """The Fabric ID of the MVP capacity; its display name is the Azure resource name."""
    name = capacity_id.rstrip("/").rsplit("/", 1)[-1]
    matches = [item for item in client.list_all("/capacities") if item.get("displayName") == name]
    if not matches:
        raise MvpError(f"The Fabric capacity {name} is not visible to you. The bootstrap makes the operator its "
                       "administrator; check Capacity administrators in the Azure portal and that Fabric is "
                       "enabled for your tenant.")
    return guid(matches[0]["id"], "capacity ID")


def existing_workspace(client, name):
    """The ID of the MVP workspace, or None. A workspace of the same name without the marker is refused."""
    matches = [item for item in client.list_all("/workspaces") if item.get("displayName") == name]
    if len(matches) > 1:
        raise MvpError(f"More than one Fabric workspace is named {name}; rename the extra ones.")
    if matches and not owned(matches[0]):
        raise MvpError(f"A Fabric workspace named {name} exists but this MVP did not create it (its description "
                       f"has no '{MARKER}'). Rename it, or choose another prefix.")
    return guid(matches[0]["id"], "workspace ID") if matches else None


def ensure_workspace(client, name, capacity, out=print):
    """The MVP workspace, created when missing and assigned to the MVP capacity."""
    workspace = existing_workspace(client, name)
    if workspace is None:
        out(f"  Creating the Fabric workspace {name} ...")
        workspace = guid(client.request("POST", "/workspaces", {"displayName": name,
                                                                 "description": WORKSPACE_DESCRIPTION})[2]["id"])
    details = client.request("GET", f"/workspaces/{workspace}")[2]
    if (details.get("capacityId") or "").lower() != capacity:
        client.request("POST", f"/workspaces/{workspace}/assignToCapacity", {"capacityId": capacity})
        waited = 0
        while (client.request("GET", f"/workspaces/{workspace}")[2].get("capacityId") or "").lower() != capacity:
            if waited >= ASSIGN_LIMIT:
                raise MvpError(f"The workspace {name} was not assigned to the capacity within ten minutes.")
            client.sleep(5)
            waited += 5
    return workspace


def definition_digest(definition):
    return hashlib.sha256(json.dumps(definition, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def ensure_item(client, workspace, collection, name, definition=None, require_marker=False):
    """Create an item, or bring an existing one up to date. The definition's digest is kept in the description, so
    an unchanged definition is not sent again."""
    description = ITEM_DESCRIPTION + (f" Definition {definition_digest(definition)}." if definition else "")
    matches = [item for item in client.list_all(f"/workspaces/{workspace}/{collection}")
               if item.get("displayName") == name]
    if len(matches) > 1:
        raise MvpError(f"More than one Fabric {collection} item is named {name}; delete the extra ones.")
    if not matches:
        body = {"displayName": name, "description": description}
        if definition:
            body["definition"] = definition
        return guid(client.operation(f"/workspaces/{workspace}/{collection}", body)["id"], f"{name} ID")
    if require_marker and MARKER not in (matches[0].get("description") or ""):
        raise MvpError(f"Fabric {collection} item {name!r} is not marked as created by this MVP.")
    item = guid(matches[0]["id"], f"{name} ID")
    if definition and matches[0].get("description") != description:
        client.operation(f"/workspaces/{workspace}/{collection}/{item}/updateDefinition", {"definition": definition},
                         needs_result=False)
        client.set_description(workspace, item, description)
    return item


def part(path, data):
    return {"path": path, "payload": base64.b64encode(data).decode("ascii"), "payloadType": "InlineBase64"}


def notebook_code(step, config):
    """The code cell: CONFIG, the pipeline rules, SparkStore and the steps, then the call for this step."""
    from fabric.notebooks.steps import STEP_PARAMETERS

    if step in AFTER_STEPS:
        scripts = (HERE / "notebooks" / "spark_store.py", ROOT / "data" / "aftersales_flow.py",
                   HERE / "notebooks" / "aftersales.py")
        return (f"CONFIG = {config!r}\n\n{embedded_source(scripts)}\n"
                "TABLES = AFTER_TABLES\nSparkStore = AftersalesSparkStore\n"
                f"after_main({step!r}, CONFIG, {{'delivery_id': delivery_id}}, spark, SparkStore)\n")
    arguments = ", ".join(f"{name!r}: {name}" for name in STEP_PARAMETERS[step])
    reset_tables = ""
    if step == "reset_tables" and config.get("reset_aftersales"):
        from fabric.notebooks.aftersales import AFTER_TABLES
        reset_tables = (f"\nfor layer, tables in {AFTER_TABLES!r}.items():\n"
                        "    TABLES[layer].update(tables)\n")
    return (f"CONFIG = {config!r}\n\n{embedded_source(NOTEBOOK_FILES)}\n\n"
            f"{reset_tables}main({step!r}, CONFIG, {{{arguments}}}, spark)\n")


def notebook_definition(step, config):
    """A generated notebook bound to its step's default lakehouse; the pipeline fills the parameters cell."""
    from fabric.notebooks.steps import STEP_LAKEHOUSE, STEP_PARAMETERS

    def cell(cell_id, text, metadata):
        return {"cell_type": "code", "id": cell_id, "metadata": metadata, "execution_count": None, "outputs": [],
                "source": text.splitlines(keepends=True)}

    default = AFTER_STEPS[step] if step in AFTER_STEPS else STEP_LAKEHOUSE[step]
    names = ("delivery_id",) if step in AFTER_STEPS else STEP_PARAMETERS[step]
    parameters = "".join(f'{name} = ""\n' for name in names)
    cells = [cell("parameters", parameters, {"tags": ["parameters"]}), cell("step", notebook_code(step, config), {})]
    notebook = {
        "nbformat": 4, "nbformat_minor": 5, "cells": cells,
        "metadata": {
            "kernelspec": {"display_name": "Synapse PySpark", "language": "Python", "name": "synapse_pyspark"},
            "kernel_info": {"name": "synapse_pyspark"}, "language_info": {"name": "python"},
            "dependencies": {"lakehouse": {
                "default_lakehouse": config["lakehouses"][default], "default_lakehouse_name": default,
                "default_lakehouse_workspace_id": config["workspace_id"],
                "known_lakehouses": [{"id": value} for value in config["lakehouses"].values()]}}},
    }
    return {"format": "ipynb", "parts": [part("notebook-content.ipynb", json.dumps(notebook).encode("utf-8"))]}


def pipeline_definition(name, workspace, notebooks):
    """fabric/pipelines/<name>.json with the workspace and notebook IDs filled in."""
    text = (HERE / "pipelines" / f"{name}.json").read_text(encoding="utf-8")
    text = text.replace("{{workspace_id}}", guid(workspace, "workspace ID"))
    for step in PIPELINES[name]:
        text = text.replace(f"{{{{notebook:{step}}}}}", guid(notebooks[step], f"{step} ID"))
    if "{{" in text:
        raise MvpError(f"fabric/pipelines/{name}.json has a placeholder that was not filled in.")
    json.loads(text)
    return {"parts": [part("pipeline-content.json", text.encode("utf-8"))]}


def run_job(client, workspace, pipeline, parameters, out=print):
    """Run a pipeline with string parameters and wait for it; returns the job instance ID."""
    _, headers, _ = client.request("POST", f"/workspaces/{workspace}/items/{pipeline}/jobs/instances?jobType=Pipeline",
                                   {"executionData": {"parameters": parameters}})
    job = guid(headers.get("location", "").rstrip("/").rsplit("/", 1)[-1], "job instance ID")
    out(f"  Pipeline run {job} started; on F2 it usually takes 3-10 minutes (Spark start is the slow part).")
    waited = 0
    while waited < JOB_LIMIT:
        client.sleep(JOB_POLL)
        waited += JOB_POLL
        state = client.request("GET", f"/workspaces/{workspace}/items/{pipeline}/jobs/instances/{job}")[2]
        status = state.get("status")
        if status == "Completed":
            return job
        if status in ("Failed", "Cancelled", "Deduped"):
            reason = state.get("failureReason") or {}
            detail = reason.get("message") if isinstance(reason, dict) else reason
            raise MvpError(f"The pipeline run {status.lower()}: {detail or 'no reason given'}. Open the run in "
                           "the Fabric Monitor hub to see which notebook failed.")
        if waited % 120 == 0:
            out(f"  ... {status} after {waited // 60} minutes")
    raise MvpError("The pipeline is still running after an hour; check it in the Fabric Monitor hub.")
