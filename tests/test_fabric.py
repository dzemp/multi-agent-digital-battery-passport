"""Exercise the Fabric path (deploy stages B5 and B6, and the fabric, report and powerbi commands) against a fake
Fabric REST API and OneLake, and against the passport app's own service with local storage.

The fake runs the code of the generated notebooks over local folders, so the tests show that the notebook text, the
pipeline definitions and the orchestrator fit together and build the same package as the simulator. Nothing here
reaches Microsoft Fabric, Spark, Delta Lake, a SQL analytics endpoint or Power BI, so the tests do not show that
Fabric accepts these definitions.
"""

import ast
import base64
import email.parser
import email.policy
import json
import re
import shutil
import sys
import unittest
import urllib.parse
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_orchestrator import (CAPACITY, ROOT, Fail, FakeAzure, Raw, Workspace, make_config,  # noqa: F401
                               setUpModule, tearDownModule)

from data import metrics, simulator
from data.package import read_folder
from data.pipeline import code_hash
from data.pipeline.deliveries import DELIVERY_MANIFEST
from data.pipeline.flows import (EXPORT_FILE, EXPORT_ID, LOT_CLAIM_COLUMNS, LOT_CLAIMS_FILE, STATUS_COLUMNS, TABLES,
                                 measure_tables)
from data.pipeline.rules import canonical
from dbp_mvp import MvpError, fabric, powerbi, smoke, supply
from dbp_mvp.config import MARKER

if str(ROOT / "app") not in sys.path:
    sys.path.append(str(ROOT / "app"))
from passport import domain, exec_views  # noqa: E402
from passport.blobs import LocalBlobs  # noqa: E402
from passport.connector import Connector  # noqa: E402
from passport.service import PassportService  # noqa: E402
from passport.store import MemoryStore  # noqa: E402

CAPACITY_GUID = "f0000000-0000-0000-0000-000000000001"
SQL_HOST = "x6eps4xrq2xudenlfv6naeo3i4-abcdefgh.datawarehouse.fabric.microsoft.com"
SQL_ENDPOINT = "f0000000-0000-0000-0000-0000000000e1"
APP_URL = "https://mvp01-passport.calmsea-1234.swedencentral.azurecontainerapps.io"
APP_ID = "aaaaaaaa-0000-0000-0000-000000000002"
ACTOR = "operator (test)"
PIPELINE_PARAMETER = re.compile(r"@pipeline\(\)\.parameters\.([a-z_]+)")


def respond(status, body=None, headers=None):
    content = body if isinstance(body, bytes) else b"" if body is None else json.dumps(body).encode("utf-8")
    return status, dict(headers or {}), content


def decode(definition, path):
    return base64.b64decode(next(part["payload"] for part in definition["parts"] if part["path"] == path))


def public(item):
    return {key: value for key, value in item.items() if key != "definition"}


class FakeFabric:
    """Fabric REST and OneLake over a folder. A pipeline run executes its notebooks' code, with Delta tables kept in
    memory and each notebook's default lakehouse files in the folder."""

    def __init__(self, root, token="token"):
        self.root, self.token = Path(root), token
        self.capacities = [{"id": CAPACITY_GUID, "displayName": "mvp01fabric", "sku": "F2"}]
        self.workspaces, self.items, self.operations, self.jobs, self.active_jobs = {}, {}, {}, {}, {}
        self.tables, self.pending, self.calls, self.faults = {}, {}, [], []
        self.ran, self.runs, self.broken = [], [], set()
        self.assign_delay, self.assigning = 0, None
        self.operation_state = {"status": "Succeeded"}
        self.sql_statuses = []

    def fail(self, method, pattern, status, body=None, headers=None):
        """The next matching call answers with this error instead."""
        self.faults.append((method, re.compile(pattern), status, body, headers))

    def collection(self, name):
        return [item for (_, kind), items in self.items.items() if kind == name for item in items.values()]

    def table(self, lakehouse, table):
        return self.tables.get((lakehouse, table), [])

    def __call__(self, method, url, headers, data, timeout):
        self.calls.append((method, url))
        assert headers["Authorization"] == f"Bearer {self.token}", headers
        for fault in self.faults:
            if fault[0] == method and fault[1].search(url):
                self.faults.remove(fault)
                return respond(*fault[2:])
        parts = urllib.parse.urlsplit(url)
        if parts.netloc == fabric.ONELAKE_HOST:
            assert headers["x-ms-version"] == fabric.ONELAKE_VERSION
            return self.onelake(method, parts, data)
        assert parts.netloc == fabric.FABRIC_HOST and parts.path.startswith("/v1/"), url
        return self.api(method, parts.path[3:], urllib.parse.parse_qs(parts.query), json.loads(data) if data else None)

    # ---- Fabric REST ------------------------------------------------------------------------------------------------
    def accepted(self, result):
        operation = str(uuid.uuid4())
        self.operations[operation] = result or {}
        return respond(202, None, {"x-ms-operation-id": operation, "retry-after": "1"})

    def api(self, method, path, query, body):
        segments = path.strip("/").split("/")
        if path == "/capacities":
            return respond(200, {"value": self.capacities})
        if segments[0] == "operations":
            return respond(200, self.operations[segments[1]] if len(segments) == 3 else self.operation_state)
        if path == "/workspaces":
            if method == "GET":
                return respond(200, {"value": list(self.workspaces.values())})
            workspace = str(uuid.uuid4())
            self.workspaces[workspace] = {"id": workspace, "displayName": body["displayName"],
                                          "description": body["description"], "type": "Workspace"}
            return respond(201, self.workspaces[workspace])
        workspace = self.workspaces[segments[1]]
        if len(segments) == 2:
            if self.assigning:
                capacity, remaining = self.assigning
                self.assigning = (capacity, remaining - 1) if remaining else None
                if not remaining:
                    workspace["capacityId"] = capacity
            return respond(200, workspace)
        if segments[2] == "assignToCapacity":
            self.assigning = (body["capacityId"], self.assign_delay)
            return respond(202)
        if segments[2] == "items":
            if len(segments) == 4:
                item = next(items[segments[3]] for (ws, _), items in self.items.items()
                            if ws == workspace["id"] and segments[3] in items)
                if method == "PATCH":
                    assert set(body) == {"description"}
                    item.update(body)
                else:
                    assert method == "GET"
                return respond(200, public(item))
            return self.job(method, workspace["id"], segments, query, body)
        if segments[2] == "sqlEndpoints":
            assert segments[3] == SQL_ENDPOINT and segments[4] == "refreshMetadata"
            return self.accepted(None)
        items = self.items.setdefault((workspace["id"], segments[2]), {})
        if len(segments) == 3:
            if method == "GET":
                return respond(200, {"value": [public(item) for item in items.values()]})
            item = {"id": str(uuid.uuid4()), "displayName": body["displayName"], "description": body["description"],
                    "workspaceId": workspace["id"], "definition": body.get("definition")}
            items[item["id"]] = item
            return self.accepted(public(item))
        item = items[segments[3]]
        if segments[4:] == ["updateDefinition"]:
            item["definition"] = body["definition"]
            return self.accepted(None)
        if method == "PATCH":
            item.update(body)
            return respond(200, public(item))
        status = self.sql_statuses.pop(0) if self.sql_statuses else "Success"
        return respond(200, {**public(item), "properties": {"sqlEndpointProperties": {
            "provisioningStatus": status, "connectionString": SQL_HOST, "id": SQL_ENDPOINT}}})

    def job(self, method, workspace, segments, query, body):
        pipeline = self.items[(workspace, "dataPipelines")][segments[3]]
        if method == "GET":
            if len(segments) == 6:
                return respond(200, {"value": self.active_jobs.get(pipeline["id"], [])})
            return respond(200, self.jobs[segments[6]])
        assert query == {"jobType": ["Pipeline"]}, query
        job = str(uuid.uuid4())
        parameters = body["executionData"]["parameters"]
        self.runs.append((pipeline["displayName"], parameters))
        self.jobs[job] = self.run_pipeline(workspace, pipeline, parameters)
        return respond(202, None, {"location": f"{fabric.FABRIC}/v1/workspaces/{workspace}/items/{pipeline['id']}"
                                               f"/jobs/instances/{job}"})

    def run_pipeline(self, workspace, pipeline, parameters):
        content = json.loads(decode(pipeline["definition"], "pipeline-content.json"))
        notebooks = self.items[(workspace, "notebooks")]
        for activity in content["properties"]["activities"]:
            settings = activity["typeProperties"]
            assert settings["workspaceId"] == workspace
            notebook = notebooks[settings["notebookId"]]
            values = {name: parameters[PIPELINE_PARAMETER.fullmatch(spec["value"]["value"]).group(1)]
                      for name, spec in settings["parameters"].items()}
            try:
                if any(not isinstance(value, str) for value in values.values()):
                    raise TypeError("pipeline parameters are text")
                if notebook["displayName"] in self.broken:
                    raise RuntimeError("Spark session lost")
                self.run_notebook(workspace, notebook, values)
            except Exception as error:  # noqa: BLE001 - a notebook failure fails the activity
                return {"status": "Failed", "failureReason": {"message": f"{activity['name']}: {error}"}}
            self.ran.append(activity["name"])
        return {"status": "Completed"}

    def run_notebook(self, workspace, notebook, values):
        document = json.loads(decode(notebook["definition"], "notebook-content.ipynb"))
        assert document["cells"][0]["metadata"] == {"tags": ["parameters"]}
        parameters, code = ("".join(cell["source"]) for cell in document["cells"])
        default = document["metadata"]["dependencies"]["lakehouse"]["default_lakehouse"]
        body, call = code.rstrip("\n").rsplit("\n", 1)
        namespace = {"__name__": "notebook"}
        exec(parameters, namespace)
        namespace.update(values)
        exec(body, namespace)
        fake, files = self, self.root / workspace / default / "Files"

        class LocalSparkStore(namespace["SparkStore"]):
            """Files in the default lakehouse's folder; Delta tables in memory, read back in another order."""

            def __init__(self, spark, workspace_id, lakehouses, files_lakehouse):
                assert workspace_id == workspace and lakehouses[files_lakehouse] == default
                super().__init__(spark, workspace_id, lakehouses, files_lakehouse, files)

            def read_table(self, lakehouse, table):
                self.table_path(lakehouse, table)
                return list(reversed(fake.tables.get((lakehouse, table), [])))

            def replace_rows(self, lakehouse, table, rows):
                self.table_path(lakehouse, table)
                fake.tables[(lakehouse, table)] = json.loads(json.dumps(namespace["shape"](lakehouse, table, rows)))

        namespace.update(SparkStore=LocalSparkStore, spark=None, print=lambda *args, **kwargs: None)
        exec(call, namespace)

    # ---- OneLake ----------------------------------------------------------------------------------------------
    def onelake(self, method, parts, data):
        query = urllib.parse.parse_qs(parts.query)
        segments = parts.path.strip("/").split("/")
        if query.get("resource") == ["filesystem"]:
            assert method == "GET" and len(segments) == 1 and query["recursive"] == ["false"]
            folder = self.root / segments[0] / query["directory"][0]
            if not folder.is_dir():
                return respond(404, {"error": {"code": "PathNotFound", "message": "The path does not exist."}})
            return respond(200, {"paths": [{"name": f"{query['directory'][0]}/{path.name}"}
                                           for path in sorted(folder.iterdir())]})
        target = self.root.joinpath(*segments)
        if method == "DELETE":
            assert query == {"recursive": ["true"]}
            if not target.is_dir():
                return respond(404, {"error": {"code": "PathNotFound", "message": "The path does not exist."}})
            shutil.rmtree(target)
            return respond(200)
        if method == "PUT":
            assert query == {"resource": ["file"]} and data == b""
            self.pending[target] = bytearray()
            return respond(201)
        if method == "PATCH" and query["action"] == ["append"]:
            assert int(query["position"][0]) == len(self.pending[target])
            self.pending[target] += data
            return respond(202)
        if method == "PATCH" and query["action"] == ["flush"]:
            buffer = self.pending.pop(target)
            assert int(query["position"][0]) == len(buffer)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(bytes(buffer))
            return respond(200)
        assert method == "GET" and not query
        if not target.is_file():
            return respond(404, {"error": {"code": "PathNotFound", "message": "The path does not exist."}})
        return respond(200, target.read_bytes())


class Capacity:
    """The Fabric capacity's power state behind az rest, and the operator's tokens."""

    def __init__(self, state="Paused"):
        self.state, self.actions = state, []

    def routes(self):
        return [(r"^account get-access-token", Raw("token"))] + self.power_routes()

    def power_routes(self):
        arm = r"^rest --method {} --url https://management\.azure\.com/\S+/capacities/mvp01fabric"
        return [(arm.format("get") + r"\?", lambda args: {"properties": {"state": self.state}}),
                (arm.format("post") + r"/(resume|suspend)\?", self.act)]

    def act(self, args):
        action = re.search(r"/(resume|suspend)\?", " ".join(args)).group(1)
        self.actions.append(action)
        self.state = "Active" if action == "resume" else "Paused"


class Endpoint:
    """A SQL analytics endpoint connection that records the batches it runs."""

    def __init__(self, error=None):
        self.error, self.opened, self.batches = error, [], []

    def __call__(self, host, database):
        if self.error:
            raise self.error
        self.opened.append((host, database))
        return self

    def cursor(self):
        return self

    def execute(self, batch):
        self.batches.append(batch)

    def close(self):
        pass


class App:
    """The passport app's service over local storage, reached through the same calls B6 makes over HTTPS and Blob."""

    def __init__(self, folder, token="token", output=None):
        self.blobs = LocalBlobs(folder)
        self.service = PassportService(MemoryStore(), self.blobs)
        self.uploaded, self.token, self.output, self.connector = [], token, output, None

    def list_blobs(self, name_starts_with=""):
        return [SimpleNamespace(name=name) for name in self.blobs.list("intake", name_starts_with)]

    def download_blob(self, name):
        data = self.blobs.get("intake", name)
        return SimpleNamespace(readall=lambda: data)

    def upload_blob(self, name, data, overwrite=True):
        self.blobs.put("intake", name, data, overwrite=overwrite)
        self.uploaded.append(name)

    def post(self, url, body, token):
        assert token == self.token, url
        if url == f"{APP_URL}/packages/import":
            status, answer = self.service.import_package(body["intake_folder"], ACTOR, body["command_id"])
        elif url.startswith(f"{APP_URL}/imports/aftersales/"):
            from passport.aftersales_import import import_delivery
            status, answer = import_delivery(self.service, url.rsplit("/", 1)[-1], ACTOR)
        else:
            # The deployed app reads the registry B3 wrote into DBP_PARTY_REGISTRY_B64 from the runtime keys.
            self.connector = self.connector or Connector(
                self.service, simulator.party_registry(simulator.signing_keys(self.output)))
            if url == f"{APP_URL}/connector/v1/agreements":
                status, answer = self.connector.record_agreement(body, ACTOR)
            else:
                assert url == f"{APP_URL}/connector/v1/messages", url
                status, answer = self.connector.receive(body, ACTOR)
        return status, json.dumps(answer)

    def post_multipart(self, url, fields, file, token):
        assert (url, token) == (f"{APP_URL}/evidence", self.token) and file[0] == "file"
        status, answer = self.service.upload_evidence(fields["kind"], fields["lot_id"], file[1], file[2], file[3],
                                                      ACTOR, fields["command_id"])
        return status, json.dumps(answer)

    def get(self, url, token):
        assert (url, token) == (f"{APP_URL}/report/status", self.token), url
        return 200, json.dumps(self.service.report_export())


STORY_IDS = ("B-001", "B-002", "B-003", "B-004", "B-005")
PLANTS = {"B-001": "PLANT-SE", "B-002": "PLANT-SE", "B-003": "PLANT-DE", "B-004": "PLANT-PL", "B-005": "PLANT-PL"}


def status_rows(committed=("B-001",)):
    return [{"battery_id": battery, "model_id": simulator.MODEL_ID, "plant_id": PLANTS[battery],
             "batch_id": "BATCH-1", "chemistry": "NMC811", "rated_kwh": 75.0, "eligibility": "eligible",
             "hold_reason": None, "lifecycle_status": "active", "committed_version": 1 if battery in committed else 0,
             "committed_utc": "2026-11-12T08:00:00Z" if battery in committed else None,
             "snapshot_state": "written" if battery in committed else "none",
             "footprint_per_kwh": 61.5 if battery in committed else None,
             "footprint_verified": 1 if battery in committed else 0, "recycled_co_pct": 12.0,
             "recycled_li_pct": 4.0, "recycled_ni_pct": 8.0, "successor_id": None,
             "second_life_value_eur_illustrative": None, "registry_status": "not_registered",
             "data_label": "synthetic"} for battery in STORY_IDS]


def lot_claims():
    return [{"lot_id": lot, "supplier_id": supplier, "material": material, "claim_status": status,
             "case_state": "none", "fields_complete": 1, "data_label": "synthetic"}
            for lot, supplier, material, status in (("LOT-LI-A", "SUP-LI", "lithium", "verified"),
                                                    ("LOT-LI-B", "SUP-LI", "lithium", "missing"))]


def status_export(rows=None, export_id="EXP-20261112T080000Z"):
    return json.dumps({"schema_version": "2", "export_id": export_id, "generated_utc": "2026-11-12T08:00:00Z",
                       "rows": status_rows() if rows is None else rows, "lot_claims": lot_claims(),
                       "data_label": "synthetic"})


class FabricCase(Workspace):
    def setUp(self):
        super().setUp()
        folder = self.state.path.parent
        self.output, self.other = folder / "output", folder / "other-output"
        self.fake = FakeFabric(folder / "onelake")
        self.capacity = Capacity()
        self.az = FakeAzure(self.capacity.routes())
        self.config = make_config()
        self.outputs = {"fabricCapacityId": CAPACITY}
        self.sleeps = []
        self.client = fabric.FabricClient(self.az, send=self.fake, sleep=self.sleeps.append)
        self.endpoint = Endpoint()
        self.app = App(folder / "app-storage", output=self.output)

    def options(self, **extra):
        return {"send": self.fake, "sleep": self.sleeps.append, "out": self.out, **extra}

    def supply(self, config=None, **kwargs):
        return supply.run_supply(config or self.config, self.az, self.outputs, output=self.output,
                                 **self.options(**kwargs))

    def publish(self, get=None, config=None):
        get = get or (lambda url, token: (200, status_export()))
        return supply.report_publish(config or self.config, self.az, self.outputs, APP_URL, APP_ID, get=get,
                                     connect=self.endpoint, output=self.output, **self.options())

    def seed(self, **kwargs):
        options = {"post": self.app.post, "post_multipart": self.app.post_multipart, **kwargs}
        return supply.seed_app(self.config, self.az, APP_URL, APP_ID, self.app, sleep=self.sleeps.append,
                               output=self.output, out=self.out, **options)

    def lakehouse_files(self, name):
        workspace = next(iter(self.fake.workspaces))
        lakehouse = next(item["id"] for item in self.fake.collection("lakehouses") if item["displayName"] == name)
        return self.fake.root / workspace / lakehouse / "Files"

    def gold_files(self, build_id):
        return read_folder(self.lakehouse_files("gold") / "packages" / build_id)

    def posts(self, pattern):
        return [url for method, url in self.fake.calls if method == "POST" and re.search(pattern, url)]


class ClientTests(FabricCase):
    def test_throttled_calls_wait_as_advised_and_errors_name_the_call(self):
        self.fake.fail("GET", r"/v1/capacities$", 429, {"message": "slow down"}, {"retry-after": "7"})
        self.assertEqual(fabric.capacity_guid(self.client, CAPACITY), CAPACITY_GUID)
        self.assertEqual(self.sleeps, [7])
        self.fake.fail("GET", r"/v1/workspaces$", 403, {"errorCode": "InsufficientPrivileges", "message": "No access"})
        with self.assertRaisesRegex(MvpError, r"^Fabric GET /v1/workspaces failed: HTTP 403 InsufficientPrivileges: "
                                              r"No access$"):
            self.client.list_all("/workspaces")
        self.assertEqual(len(self.az.called("get-access-token --resource https://api.fabric")), 1)

    def test_tokens_go_only_to_fabric_and_onelake(self):
        for url in ("https://example.com/v1/workspaces", "http://api.fabric.microsoft.com/v1/workspaces",
                    "https://api.fabric.microsoft.com/v2/workspaces",
                    "https://api.fabric.microsoft.com.example.com/v1/workspaces",
                    "https://onelake.dfs.fabric.microsoft.com.example.com/ws"):
            with self.subTest(url=url), self.assertRaisesRegex(MvpError, "Refusing to send a Fabric token"):
                self.client.request("GET", url)
        self.assertEqual((self.fake.calls, self.az.calls), ([], []))

    def test_lists_follow_continuation_tokens(self):
        seen = []

        def send(method, url, headers, data, timeout):
            seen.append(url)
            page = {"value": [{"id": "1"}], "continuationToken": "a/b"} if len(seen) == 1 else {"value": [{"id": "2"}]}
            return respond(200, page)

        client = fabric.FabricClient(self.az, send=send, sleep=self.sleeps.append)
        self.assertEqual(client.list_all("/workspaces"), [{"id": "1"}, {"id": "2"}])
        self.assertEqual(seen, [f"{fabric.FABRIC}/v1/workspaces",
                                f"{fabric.FABRIC}/v1/workspaces?continuationToken=a%2Fb"])

    def test_lists_and_folders_follow_continuation_addresses(self):
        seen, workspace, lakehouse = [], "a0000000-0000-0000-0000-000000000001", "b0000000-0000-0000-0000-000000000001"
        later = f"{fabric.FABRIC}/v1/workspaces?continuationToken=next"

        def send(method, url, headers, data, timeout):
            seen.append(url)
            if urllib.parse.urlsplit(url).netloc == fabric.ONELAKE_HOST:
                more = "continuation" not in url
                paths = [{"name": f"{lakehouse}/Files/packages/{'b' if more else 'a'}"}]
                return respond(200, {"paths": paths}, {"x-ms-continuation": "page 2"} if more else {})
            return respond(200, {"value": [{"id": "1"}], "continuationUri": later} if len(seen) == 1
                           else {"value": [{"id": "2"}]})

        client = fabric.FabricClient(self.az, send=send, sleep=self.sleeps.append)
        self.assertEqual(client.list_all("/workspaces"), [{"id": "1"}, {"id": "2"}])
        self.assertEqual(seen[1], later)
        self.assertEqual(fabric.OneLake(client, workspace).names(lakehouse, "Files/packages"), ["a", "b"])
        self.assertIn("continuation=page+2", seen[-1])

    def test_an_operation_without_an_operation_id_is_followed_by_its_location(self):
        poll = f"{fabric.FABRIC}/v1/workspaces/w/items/i/operations/o"
        states = [{"status": "Running"}, {"status": "Succeeded"}]
        seen = []

        def send(method, url, headers, data, timeout):
            seen.append((method, url))
            if method == "POST":
                return respond(202, None, {"location": poll, "retry-after": "3"})
            if url.endswith("/result"):
                return respond(200, {"id": "created"})
            return respond(200, states.pop(0), {"retry-after": "2"})

        client = fabric.FabricClient(self.az, send=send, sleep=self.sleeps.append)
        self.assertEqual(client.operation("/workspaces/w/notebooks", {}), {"id": "created"})
        self.assertEqual(self.sleeps, [3, 2])
        self.assertEqual(seen[1:], [("GET", poll), ("GET", poll), ("GET", poll + "/result")])

    def test_throttling_that_does_not_end_fails_the_call(self):
        for _ in range(fabric.ATTEMPTS):
            self.fake.fail("GET", r"/v1/capacities$", 429, {"message": "slow down"}, {"retry-after": "2"})
        with self.assertRaisesRegex(MvpError, r"^Fabric GET /v1/capacities failed: HTTP 429 slow down$"):
            fabric.capacity_guid(self.client, CAPACITY)
        self.assertEqual(self.sleeps, [2] * (fabric.ATTEMPTS - 1))

    def test_the_capacity_is_paused_again_whatever_ends_the_block(self):
        with self.assertRaisesRegex(RuntimeError, "interrupted"), \
                fabric.active_capacity(self.az, CAPACITY, sleep=self.sleeps.append, out=self.out):
            raise RuntimeError("interrupted")
        self.assertEqual((self.capacity.actions, self.capacity.state), (["resume", "suspend"], "Paused"))
        self.az.on(r"/resume\?", Fail("ERROR: (Conflict) The capacity is busy."))
        with self.assertRaisesRegex(MvpError, "busy"), \
                fabric.active_capacity(self.az, CAPACITY, sleep=self.sleeps.append, out=self.out):
            self.fail("the block runs only on an active capacity")
        self.assertEqual((self.printed.count("Fabric capacity paused."), self.capacity.state), (2, "Paused"))

    def test_a_failed_operation_reports_fabric_reason(self):
        workspace = fabric.ensure_workspace(self.client, "mvp01-passport", CAPACITY_GUID, self.out)
        self.fake.operation_state = {"status": "Failed", "error": {"errorCode": "ItemDisplayNameAlreadyInUse",
                                                                    "message": "The name is taken."}}
        with self.assertRaisesRegex(MvpError, r"Fabric operation for /workspaces/\S+/notebooks failed: "
                                              r"ItemDisplayNameAlreadyInUse: The name is taken\."):
            fabric.ensure_item(self.client, workspace, "notebooks", "bronze_load", {"parts": []})


class ItemTests(FabricCase):
    def test_the_workspace_is_created_marked_and_assigned_to_the_capacity(self):
        self.fake.assign_delay = 2
        workspace = fabric.ensure_workspace(self.client, "mvp01-passport", CAPACITY_GUID, self.out)
        details = self.fake.workspaces[workspace]
        self.assertIn(MARKER, details["description"])
        self.assertEqual(details["capacityId"], CAPACITY_GUID)
        self.assertEqual(self.sleeps, [5, 5])
        calls = len(self.fake.calls)
        self.assertEqual(fabric.ensure_workspace(self.client, "mvp01-passport", CAPACITY_GUID, self.out), workspace)
        self.assertEqual([method for method, _ in self.fake.calls[calls:]], ["GET", "GET"])

    def test_a_workspace_this_mvp_did_not_create_is_left_alone(self):
        self.fake.workspaces["w1"] = {"id": "w1", "displayName": "mvp01-passport", "description": "Team space"}
        with self.assertRaisesRegex(MvpError, "exists but this MVP did not create it"):
            fabric.ensure_workspace(self.client, "mvp01-passport", CAPACITY_GUID, self.out)
        self.fake.workspaces["w2"] = {"id": "w2", "displayName": "mvp01-passport", "description": MARKER}
        with self.assertRaisesRegex(MvpError, "More than one Fabric workspace is named mvp01-passport"):
            fabric.ensure_workspace(self.client, "mvp01-passport", CAPACITY_GUID, self.out)
        self.assertEqual({method for method, _ in self.fake.calls}, {"GET"})

    def test_the_capacity_must_be_visible_in_fabric(self):
        self.fake.capacities = [{"id": CAPACITY_GUID, "displayName": "othercapacity"}]
        with self.assertRaisesRegex(MvpError, "The Fabric capacity mvp01fabric is not visible to you"):
            fabric.capacity_guid(self.client, CAPACITY)

    def test_an_item_definition_is_sent_again_only_when_it_changed(self):
        workspace = fabric.ensure_workspace(self.client, "mvp01-passport", CAPACITY_GUID, self.out)
        first, second = {"parts": [fabric.part("a.json", b"1")]}, {"parts": [fabric.part("a.json", b"2")]}
        item = fabric.ensure_item(self.client, workspace, "notebooks", "bronze_load", first)
        calls = len(self.fake.calls)
        self.assertEqual(fabric.ensure_item(self.client, workspace, "notebooks", "bronze_load", first), item)
        self.assertEqual([method for method, _ in self.fake.calls[calls:]], ["GET"])
        self.fake.fail("PATCH", rf"/notebooks/{item}$", 500, {"Message": "An error has occurred."})
        self.fake.fail("PATCH", rf"/items/{item}$", 500, {"Message": "An error has occurred."}, {"retry-after": "7"})
        self.assertEqual(fabric.ensure_item(self.client, workspace, "notebooks", "bronze_load", second), item)
        stored = self.fake.items[(workspace, "notebooks")][item]
        self.assertEqual(stored["definition"], second)
        self.assertEqual(stored["description"],
                         f"{fabric.ITEM_DESCRIPTION} Definition {fabric.definition_digest(second)}.")
        self.assertEqual(len(self.posts(r"/updateDefinition$")), 1)
        self.assertEqual(self.sleeps[-1], 7)
        calls = len(self.fake.calls)
        fabric.ensure_item(self.client, workspace, "notebooks", "bronze_load", second)
        self.assertEqual([method for method, _ in self.fake.calls[calls:]], ["GET"])

    def test_metadata_error_after_fabric_applied_the_patch_is_verified_without_repeating_it(self):
        workspace = fabric.ensure_workspace(self.client, "mvp01-passport", CAPACITY_GUID, self.out)
        old, new = ({"parts": [fabric.part("a.json", value)]} for value in (b"old", b"new"))
        item = fabric.ensure_item(self.client, workspace, "notebooks", "reset_tables", old)

        def send(method, url, headers, data, timeout):
            result = self.fake(method, url, headers, data, timeout)
            return respond(500, {"Message": "An error has occurred."}) if method == "PATCH" else result

        self.client.send = send
        fabric.ensure_item(self.client, workspace, "notebooks", "reset_tables", new)
        item_url = f"{fabric.FABRIC}/v1/workspaces/{workspace}/items/{item}"
        self.assertEqual(self.fake.calls[-2:], [("PATCH", item_url), ("GET", item_url)])
        self.assertEqual(len(self.posts(r"/updateDefinition$")), 1)

    def test_unconfirmed_metadata_failure_stops_before_any_reset_job(self):
        self.supply()
        item = next(i for i in self.fake.collection("notebooks") if i["displayName"] == "reset_tables")
        item["description"] = fabric.ITEM_DESCRIPTION
        self.fake.calls.clear()
        for _ in range(fabric.ATTEMPTS):
            self.fake.fail("PATCH", rf"/items/{item['id']}$", 500, {"Message": "An error has occurred."})
        with self.assertRaisesRegex(MvpError, r"PATCH .* failed: HTTP 500"):
            supply.clear_fabric_data(self.config, self.az, self.outputs, **self.options())
        self.assertEqual(len(self.posts(r"/updateDefinition$")), 1)
        self.assertEqual(sum(method == "PATCH" for method, _ in self.fake.calls), fabric.ATTEMPTS)
        self.assertFalse(self.posts(r"/jobs/instances"))
        self.assertFalse(any(method == "DELETE" for method, _ in self.fake.calls))
        self.assertEqual(self.capacity.state, "Paused")

    def test_failed_reset_definition_is_not_marked_or_retried(self):
        self.supply()
        item = next(i for i in self.fake.collection("notebooks") if i["displayName"] == "reset_tables")
        item["description"] = fabric.ITEM_DESCRIPTION
        self.fake.calls.clear()
        self.fake.fail("POST", rf"/notebooks/{item['id']}/updateDefinition$", 500, {"message": "Update failed"})
        with self.assertRaisesRegex(MvpError, r"updateDefinition failed: HTTP 500 Update failed"):
            supply.clear_fabric_data(self.config, self.az, self.outputs, **self.options())
        self.assertEqual(len(self.posts(r"/updateDefinition$")), 1)
        self.assertFalse(any(method in ("PATCH", "DELETE") for method, _ in self.fake.calls))
        self.assertFalse(self.posts(r"/jobs/instances"))
        self.assertEqual(self.capacity.state, "Paused")


class DefinitionTests(unittest.TestCase):
    SETTINGS = {"workspace_id": "a0000000-0000-0000-0000-000000000001",
                "lakehouses": {name: f"b0000000-0000-0000-0000-00000000000{index}"
                               for index, name in enumerate(fabric.LAKEHOUSES)},
                "registry": {}, "code_hash": "0" * 64}

    def test_a_notebook_has_a_parameters_cell_its_default_lakehouse_and_no_package_imports(self):
        document = json.loads(decode(fabric.notebook_definition("gold_package", self.SETTINGS),
                                     "notebook-content.ipynb"))
        parameters, code = ("".join(cell["source"]) for cell in document["cells"])
        self.assertEqual(parameters, 'delivery_set = ""\nbuild_id = ""\nbuild_sequence = ""\ncompleted_utc = ""\n')
        self.assertTrue(code.startswith(f"CONFIG = {self.SETTINGS!r}\n"))
        self.assertTrue(code.endswith("main('gold_package', CONFIG, {'delivery_set': delivery_set, 'build_id': "
                                      "build_id, 'build_sequence': build_sequence, 'completed_utc': completed_utc}, "
                                      "spark)\n"))
        self.assertNotRegex(code, r"(?m)^(from \.|from data\.|import data)")
        compile(code, "notebook", "exec")
        lakehouse = document["metadata"]["dependencies"]["lakehouse"]
        self.assertEqual((lakehouse["default_lakehouse"], lakehouse["default_lakehouse_name"]),
                         (self.SETTINGS["lakehouses"]["gold"], "gold"))

    def test_a_pipeline_runs_its_notebooks_in_order_with_its_parameters(self):
        notebooks = {step: f"c0000000-0000-0000-0000-00000000000{index}"
                     for index, step in enumerate(fabric.PIPELINES["supply_flow"])}
        content = json.loads(decode(fabric.pipeline_definition("supply_flow", self.SETTINGS["workspace_id"],
                                                               notebooks), "pipeline-content.json"))
        activities = content["properties"]["activities"]
        self.assertEqual([a["typeProperties"]["notebookId"] for a in activities], list(notebooks.values()))
        self.assertEqual([[d["activity"] for d in a["dependsOn"]] for a in activities],
                         [[], ["bronze_load"], ["silver_normalise"]])
        self.assertEqual(set(content["properties"]["parameters"]),
                         {"delivery_set", "build_id", "build_sequence", "completed_utc"})
        for activity in activities:
            for name, spec in activity["typeProperties"]["parameters"].items():
                self.assertEqual(spec["value"]["value"], f"@pipeline().parameters.{name}")


class SupplyTests(FabricCase):
    def test_fabric_builds_the_same_package_as_the_local_simulator(self):
        result = self.supply()
        self.assertEqual((result["package_source"], result["reason"]), ("fabric", None))
        self.assertEqual(self.capacity.actions, ["resume", "suspend"])
        self.assertEqual(self.fake.ran, ["bronze_load", "silver_normalise", "gold_package"])
        state = simulator.load_state(self.output)
        self.assertEqual(self.gold_files(result["build_id"]),
                         read_folder(simulator.build_paths(self.output, state)["package"]))
        self.assertEqual(self.fake.runs, [("supply_flow", {
            "delivery_set": state["delivery_set"], "build_id": state["build_id"],
            "build_sequence": str(state["build_sequence"]), "completed_utc": state["completed_utc"]})])
        kinds = ("lakehouses", "notebooks", "dataPipelines")
        self.assertEqual({kind: len(self.fake.collection(kind)) for kind in kinds},
                         {"lakehouses": 5, "notebooks": 5, "dataPipelines": 3})
        self.assertTrue(all(MARKER in item["description"] for item in self.fake.collection("notebooks")))
        uploads = {}
        for method, url in self.fake.calls:
            if method == "PUT" and "/Files/deliveries/" in url:
                folder, name = url.split("?")[0].split("/Files/deliveries/")[1].rsplit("/", 1)
                uploads.setdefault(folder, []).append(name)
        self.assertEqual(sorted(uploads), sorted(state["delivery_set"].split(",")))
        self.assertEqual({names[-1] for names in uploads.values()}, {DELIVERY_MANIFEST})
        for notebook in self.fake.collection("notebooks"):
            document = json.loads(decode(notebook["definition"], "notebook-content.ipynb"))
            self.assertEqual((document["nbformat"], [cell["id"] for cell in document["cells"]]),
                             (4, ["parameters", "step"]))
            config = ast.literal_eval("".join(document["cells"][1]["source"]).split("\n", 1)[0][len("CONFIG = "):])
            self.assertEqual(config["code_hash"], code_hash())
        manifest = json.loads(self.gold_files(result["build_id"])["manifest.json"])
        self.assertEqual(manifest["rules_source_sha256"], code_hash())
        self.assertEqual(len(self.fake.table("gold", "battery_inputs")), 5)
        self.assertIn("Fabric's gold package is byte-identical to the local package.", self.printed)
        self.assertIn("Fabric capacity paused.", self.printed)

    def test_a_rerun_reuses_the_build_without_running_the_pipeline_again(self):
        first = self.supply()
        calls = len(self.fake.calls)
        second = self.supply()
        self.assertEqual((second["build_id"], second["package_source"]), (first["build_id"], "fabric"))
        self.assertEqual(len(self.fake.runs), 1)
        self.assertEqual({method for method, _ in self.fake.calls[calls:]}, {"GET"})
        self.assertIn("Fabric already built this package; the pipeline is not run again.", self.printed)

    def test_a_new_build_flags_changes_the_same_way_in_fabric_and_locally(self):
        first = self.supply()
        second = self.supply(new_build=True)
        self.assertGreater(second["build_sequence"], first["build_sequence"])
        self.assertEqual(second["package_source"], "fabric", second["reason"])
        hashes = [{row["battery_id"]: row["input_hash"] for row in map(json.loads, self.gold_files(
            build["build_id"])["batteries.jsonl"].splitlines())} for build in (first, second)]
        self.assertEqual(hashes[0], hashes[1])
        self.assertEqual(len(self.fake.table("gold", "complete_builds")), 2)

    def test_clean_fabric_data_keeps_items_and_clears_generated_files_and_table_rows(self):
        self.supply()
        workspace = next(iter(self.fake.workspaces))
        ingest = self.lakehouse_files("ingest")
        reporting = self.lakehouse_files("reporting")
        (reporting / "exports" / "EXP-20261112T080000Z").mkdir(parents=True)
        (reporting / "exports" / "EXP-20261112T080000Z" / "manifest.json").write_text("{}")
        self.assertTrue((ingest / "deliveries").exists())
        self.assertTrue(self.fake.table("gold", "battery_inputs"))

        def send(method, url, headers, data, timeout):
            if url.endswith(f"/workspaces/{workspace}/lakehouses") and self.capacity.state != "Active":
                return respond(404, {"errorCode": "CapacityNotActive", "message": "Capacity is not active"})
            return self.fake(method, url, headers, data, timeout)

        result = supply.clear_fabric_data(self.config, self.az, self.outputs, **self.options(send=send))
        self.assertEqual(result["tables_cleared"], sum(map(len, TABLES.values())))
        self.assertEqual(self.fake.table("gold", "battery_inputs"), [])
        self.assertFalse((ingest / "deliveries").exists())
        self.assertFalse((self.lakehouse_files("gold") / "packages").exists())
        self.assertFalse((reporting / "exports").exists())
        self.assertEqual(len(self.fake.collection("lakehouses")), 5)
        self.assertEqual(len(self.fake.collection("dataPipelines")), 3)
        self.assertEqual(self.capacity.actions, ["resume", "suspend", "resume", "suspend"])
        self.assertTrue(workspace)

    def test_clean_pauses_without_deleting_data_if_a_lakehouse_is_unmarked(self):
        self.supply()
        next(item for item in self.fake.collection("lakehouses") if item["displayName"] == "gold")["description"] = ""
        self.fake.calls.clear()
        self.capacity.actions.clear()
        with self.assertRaisesRegex(MvpError, "lakehouse 'gold'.*not marked"):
            supply.clear_fabric_data(self.config, self.az, self.outputs, **self.options())
        self.assertEqual(self.capacity.actions, ["resume", "suspend"])
        self.assertTrue(all(method == "GET" for method, _ in self.fake.calls))
        self.assertTrue((self.lakehouse_files("ingest") / "deliveries").exists())
        self.assertTrue(self.fake.table("gold", "battery_inputs"))

    def test_clean_fabric_data_refuses_to_delete_while_a_pipeline_is_running(self):
        self.supply()
        pipeline = next(item for item in self.fake.collection("dataPipelines")
                        if item["displayName"] == "supply_flow")
        self.fake.active_jobs[pipeline["id"]] = [{"status": "InProgress"}]
        ingest = self.lakehouse_files("ingest")
        self.assertTrue((ingest / "deliveries").exists())
        with self.assertRaisesRegex(MvpError, "active run"):
            supply.clear_fabric_data(self.config, self.az, self.outputs, **self.options())
        self.assertTrue((ingest / "deliveries").exists())
        self.assertEqual(self.capacity.actions, ["resume", "suspend", "resume", "suspend"])

    def test_a_failing_notebook_leaves_the_local_package_and_pauses_the_capacity(self):
        self.fake.broken.add("silver_normalise")
        result = self.supply()
        self.assertEqual(result["package_source"], "local")
        self.assertEqual(result["reason"], "the Fabric run failed (MvpError: The pipeline run failed: "
                                           "silver_normalise: Spark session lost. Open the run in the Fabric "
                                           "Monitor hub to see which notebook failed.)")
        self.assertEqual(self.capacity.actions, ["resume", "suspend"])
        self.assertIn("WARNING: using the local package instead of Fabric's", self.printed)

    def test_a_delivery_changed_under_the_same_id_is_refused(self):
        self.supply()
        workspace = next(iter(self.fake.workspaces))
        ingest = next(item["id"] for item in self.fake.collection("lakehouses") if item["displayName"] == "ingest")
        manifest = next((self.fake.root / workspace / ingest / "Files" / "deliveries" / "SUP-WIDEWORLD").glob(
            "*/delivery.json"))
        manifest.write_bytes(manifest.read_bytes().replace(b"SUP-WIDEWORLD", b"SUP-WIDEWORLD "))
        result = self.supply()
        self.assertEqual(result["package_source"], "local")
        self.assertRegex(result["reason"], r"The ingest lakehouse already has delivery SUP-WIDEWORLD/DLV-\S+ with "
                                           r"other content\. Run python -m dbp_mvp fabric run supply --new-build")

    def test_a_package_completed_at_another_time_matches_apart_from_the_manifest_time(self):
        run_job = fabric.run_job

        def later(client, workspace, pipeline, parameters, out=print):
            return run_job(client, workspace, pipeline, {**parameters, "completed_utc": "2030-01-01T00:00:00Z"}, out)

        with mock.patch.object(fabric, "run_job", later):
            result = self.supply()
        self.assertEqual((result["package_source"], result["reason"]), ("fabric", None))
        remote = self.gold_files(result["build_id"])
        local = read_folder(simulator.build_paths(self.output, simulator.load_state(self.output))["package"])
        self.assertEqual([name for name in local if remote[name] != local[name]], ["manifest.json"])
        manifests = [json.loads(files["manifest.json"]) for files in (remote, local)]
        self.assertEqual([key for key in manifests[1] if manifests[0][key] != manifests[1][key]], ["completed_utc"])
        self.assertIn("data files byte-identical, manifest equal apart from completed_utc", self.printed)
        resequenced = {**remote, "manifest.json": json.dumps({**manifests[0], "build_sequence": 7}).encode("utf-8")}
        self.assertEqual(supply.compare(resequenced, local),
                         "the Fabric package manifest differs from the local one in build_sequence")
        lots = {**remote, "lots.jsonl": remote["lots.jsonl"].replace(b"LOT-", b"LOT-X", 1)}
        self.assertRegex(supply.compare(lots, local), r"^the Fabric package failed its check: ")

    def test_a_delivery_damaged_in_ingest_is_rejected_and_the_local_package_is_used(self):
        upload = supply.upload_deliveries

        def damaged(lake, ingest_id, *args, **kwargs):
            sent = upload(lake, ingest_id, *args, **kwargs)
            lots = sorted((self.lakehouse_files("ingest") / "deliveries" / "SUP-NORTHWIND").glob("*/lots.json"))[0]
            lots.write_bytes(lots.read_bytes() + b" ")
            return sent

        with mock.patch.object(supply, "upload_deliveries", damaged):
            result = self.supply()
        self.assertEqual(result["package_source"], "local")
        self.assertRegex(result["reason"], r"gold_package: .*does not match its deliveries")
        rejected = [row for row in self.fake.table("bronze", "deliveries") if row["status"] != "accepted"]
        self.assertEqual([(row["party_id"], row["reason"]) for row in rejected],
                         [("SUP-NORTHWIND", "hash_mismatch:lots.json")])
        self.assertNotIn(rejected[0]["delivery_id"],
                         {row["delivery_id"] for row in self.fake.table("bronze", "raw_records")})
        self.assertFalse((self.lakehouse_files("gold") / "packages" / result["build_id"]).exists())
        self.assertEqual(self.capacity.actions, ["resume", "suspend"])

    def test_without_fabric_the_local_package_is_used_without_any_call(self):
        result = self.supply(config=make_config(deploy_fabric=False))
        self.assertEqual((result["package_source"], result["reason"]),
                         ("local", "Fabric is not deployed (deploy_fabric = false)"))
        self.assertEqual((self.az.calls, self.fake.calls), ([], []))
        self.assertIn("Estimated agent model calls for 5 batteries", self.printed)

    def test_a_capacity_that_does_not_pause_is_reported_as_still_billed(self):
        self.az.on(r"/suspend\?", Fail("ERROR: (Conflict) The capacity is busy."))
        result = self.supply()
        self.assertEqual(result["package_source"], "fabric")
        self.assertIn("WARNING: the Fabric capacity could not be paused and is still billed", self.printed)
        self.assertEqual(self.capacity.state, "Active")

    def test_check_reads_without_resuming_or_creating_anything(self):
        result = supply.check(self.config, self.az, self.outputs, **self.options())
        self.assertEqual(result, {"capacity_id": CAPACITY, "workspace_id": None})
        self.assertIn("Fabric capacity mvp01fabric: Paused", self.printed)
        self.assertIn(f"Visible to you in Fabric as capacity {CAPACITY_GUID}.", self.printed)
        self.assertIn("Workspace mvp01-passport: not created yet", self.printed)
        self.assertEqual((self.capacity.actions, {method for method, _ in self.fake.calls}), ([], {"GET"}))
        self.supply()
        workspace = supply.check(self.config, self.az, self.outputs, **self.options())["workspace_id"]
        self.assertIn(f"Workspace mvp01-passport: {workspace} (created by this MVP).", self.printed)
        with self.assertRaisesRegex(MvpError, "Fabric is not deployed"):
            supply.check(make_config(deploy_fabric=False), self.az, self.outputs, **self.options())


class NotebookTests(FabricCase):
    """The generated bronze_load notebook applies the shared delivery checks: a rejection is recorded with its reason
    and loads nothing."""

    def test_each_rejected_delivery_is_recorded_in_bronze_without_records(self):
        items = supply.deploy_items(self.client, self.config.names.fabric_workspace, CAPACITY, self.out)
        workspace = items["workspace_id"]
        notebook = self.fake.items[(workspace, "notebooks")][items["notebooks"]["bronze_load"]]
        party, delivery, good = next(entry for entry in simulator.make_deliveries("MAIN", 1)
                                     if entry[1] == "DLV-MAIN-1-SUP-NORTHWIND-LOTS")
        manifest = json.loads(good[DELIVERY_MANIFEST])

        def resign(**changes):
            return {**good, DELIVERY_MANIFEST: (canonical({**manifest, **changes}) + "\n").encode("utf-8")}

        miscounted = [{**entry, "records": entry["records"] + 1} for entry in manifest["files"]]
        cases = [("missing_manifest", party, {"lots.json": good["lots.json"]}),
                 ("missing_file:lots.json", party, {DELIVERY_MANIFEST: good[DELIVERY_MANIFEST]}),
                 ("hash_mismatch:lots.json", party, {**good, "lots.json": good["lots.json"] + b" "}),
                 ("count_mismatch:lots.json", party, resign(files=miscounted)),
                 ("unknown_party:SUP-UNKNOWN", "SUP-UNKNOWN", resign(party_id="SUP-UNKNOWN")),
                 ("kind_not_allowed:transport", party, resign(kind="transport")),
                 (None, party, good)]
        for reason, sender, files in cases:
            with self.subTest(reason=reason):
                folder = self.lakehouse_files("ingest") / "deliveries" / sender / delivery
                shutil.rmtree(folder, ignore_errors=True)
                folder.mkdir(parents=True)
                for name, data in files.items():
                    (folder / name).write_bytes(data)
                self.fake.run_notebook(workspace, notebook, {"delivery_set": f"{sender}/{delivery}"})
                [row] = self.fake.table("bronze", "deliveries")
                loaded = [row for row in self.fake.table("bronze", "raw_records") if row["delivery_id"] == delivery]
                self.assertEqual((row["party_id"], row["status"], row["reason"]),
                                 (sender, "rejected" if reason else "accepted", reason))
                self.assertEqual(len(loaded), 0 if reason else manifest["files"][0]["records"])


class ReportTests(FabricCase):
    def test_report_publish_loads_the_status_and_updates_the_views(self):
        self.supply()
        result = self.publish()
        self.assertEqual(result["export_id"], "EXP-20261112T080000Z")  # the app's export ID, not a new one
        self.assertEqual((result["rows"], result["lot_claims"], result["views"]), (5, 2, True))
        self.assertEqual(self.fake.runs[-1], ("status_flow", {"export_id": result["export_id"]}))
        uploads = [url.rsplit("/", 1)[-1].split("?")[0] for method, url in self.fake.calls
                   if method == "PUT" and "/Files/exports/" in url]
        self.assertEqual(uploads, [EXPORT_FILE, LOT_CLAIMS_FILE, "manifest.json"])
        self.assertEqual([(row["plant_id"], row["produced"], row["published"], row["coverage_pct"])
                          for row in self.fake.table("reporting", "measures_by_plant")],
                         [("PLANT-DE", 1, 0, 0.0), ("PLANT-PL", 2, 0, 0.0), ("PLANT-SE", 2, 1, 50.0)])
        [success] = self.fake.table("reporting", "success_measures")
        self.assertEqual((success["produced"], success["published"], success["coverage_pct"],
                          success["verified_footprint_batteries"], success["complete_lots"],
                          success["supplier_completeness_pct"], success["not_in_export"]), (5, 1, 20.0, 1, 1, 50.0, 0))
        self.assertEqual([row for row in self.fake.table("reporting", "passport_status_current")],
                         [{"export_id": result["export_id"], "generated_utc": "2026-11-12T08:00:00Z", **row}
                          for row in status_rows()])  # the full v2 rows, no projection
        self.assertEqual([row["lot_id"] for row in self.fake.table("reporting", "lot_claim_status_current")],
                         ["LOT-LI-A", "LOT-LI-B"])
        self.assertTrue((self.output / "exports" / result["export_id"] / LOT_CLAIMS_FILE).is_file())
        self.assertIn("    passport coverage      1 of 5 produced (20.0%)", self.lines)
        self.assertEqual(self.endpoint.opened, [(SQL_HOST, "reporting")])
        self.assertEqual([batch.count("CREATE OR ALTER VIEW") for batch in self.endpoint.batches], [1] * 5)
        self.assertEqual(len(self.posts(rf"/sqlEndpoints/{SQL_ENDPOINT}/refreshMetadata$")), 1)
        self.assertEqual(self.capacity.actions, ["resume", "suspend"] * 2)
        self.assertEqual(self.fake.collection("semanticModels"), [])

    def test_status_for_batteries_fabric_never_packaged_fails_the_run(self):
        with self.assertRaisesRegex(MvpError, r"The pipeline run failed: reporting_measures: export EXP-\S+: 5 "
                                              r"batteries are not in a completed Fabric package"):
            self.publish()
        self.assertEqual(self.capacity.actions, ["resume", "suspend"])

    def test_report_publish_needs_status_rows_from_the_app(self):
        with self.assertRaisesRegex(MvpError, "The app's status report answered 403"):
            self.publish(get=lambda url, token: (403, '{"error": "forbidden"}'))
        with self.assertRaisesRegex(MvpError, "The status export could not be made: The export has no rows"):
            self.publish(get=lambda url, token: (200, status_export(rows=[])))
        with self.assertRaisesRegex(MvpError, "not a version 2 export with an export ID"):
            self.publish(get=lambda url, token: (200, json.dumps({"rows": status_rows()})))
        with self.assertRaisesRegex(MvpError, "not a version 2 export with an export ID"):
            self.publish(get=lambda url, token: (200, status_export(export_id="EXP-1")))
        self.assertEqual((self.capacity.actions, self.fake.calls), ([], []))

    def test_a_view_failure_is_only_a_warning(self):
        self.supply()
        self.endpoint.error = RuntimeError("Login failed for user '<token-identified principal>'.")
        self.assertFalse(self.publish()["views"])
        self.assertIn("WARNING: Creating the reporting views failed in the Fabric SQL analytics endpoint: Login "
                      "failed", self.printed)
        self.assertIn("run python -m dbp_mvp fabric run status to retry the views.", self.printed)
        self.assertEqual(self.capacity.actions[-1], "suspend")

    def test_run_status_rebuilds_from_the_newest_export_and_checks_the_id_first(self):
        with self.assertRaisesRegex(MvpError, "'EXP-1' is not an export ID like EXP-20261112T101500Z"):
            supply.run_status(self.config, self.az, self.outputs, export_id="EXP-1", connect=self.endpoint,
                              **self.options())
        self.assertEqual((self.az.calls, self.fake.calls), ([], []))
        self.supply()
        export_id = self.publish()["export_id"]
        self.assertEqual(supply.run_status(self.config, self.az, self.outputs, connect=self.endpoint,
                                           **self.options()), {"export_id": export_id, "views": True})
        self.assertEqual([name for name, _ in self.fake.runs], ["supply_flow", "status_flow", "status_flow"])
        with self.assertRaisesRegex(MvpError, "no complete export EXP-20261112T101500Z"):
            supply.run_status(self.config, self.az, self.outputs, export_id="EXP-20261112T101500Z",
                              connect=self.endpoint, **self.options())

    def test_the_semantic_model_needs_an_export_and_is_sent_once(self):
        with self.assertRaisesRegex(MvpError, "no status export yet"):
            supply.publish_model(self.config, self.az, self.outputs, **self.options())
        self.assertEqual(self.capacity.actions, ["resume", "suspend"])
        self.supply()
        self.publish()
        self.fake.sql_statuses = ["InProgress"]
        result = supply.publish_model(self.config, self.az, self.outputs, **self.options())
        model = self.fake.items[(result["workspace_id"], "semanticModels")][result["model_id"]]
        self.assertEqual(model["displayName"], powerbi.MODEL)
        self.assertEqual(model["definition"]["format"], "TMDL")
        paths = {part["path"] for part in model["definition"]["parts"]}
        self.assertIn("definition.pbism", paths)
        self.assertEqual({path for path in paths if path.startswith("definition/tables/")},
                         {f"definition/tables/{name}.tmdl" for name in REPORT_TABLES})
        text = "".join(decode(model["definition"], path).decode("utf-8") for path in paths)
        self.assertIn(SQL_HOST, text)
        self.assertIn(SQL_ENDPOINT, text)
        self.assertNotIn("{{", text)
        report = self.fake.items[(result["workspace_id"], "reports")][result["report_id"]]
        self.assertEqual((report["displayName"], report["definition"]["format"]), (powerbi.REPORT, "PBIR"))
        binding = json.loads(decode(report["definition"], "definition.pbir"))
        self.assertEqual(binding["datasetReference"],
                         {"byConnection": {"connectionString": f"semanticmodelid={result['model_id']}"}})
        self.assertIn(15, self.sleeps)
        self.assertIn(powerbi.NOTE, self.lines)
        supply.publish_model(self.config, self.az, self.outputs, **self.options())
        self.assertEqual(self.posts(r"/(semanticModels|reports)/\S+/updateDefinition$"), [])
        self.assertEqual(len(self.fake.collection("reports")), 1)

    def test_with_powerbi_on_report_publish_also_sends_the_model_and_report(self):
        config = make_config(powerbi=True)
        self.supply(config=config)
        self.assertTrue(self.publish(config=config)["views"])
        self.assertEqual([item["displayName"] for item in self.fake.collection("semanticModels")], [powerbi.MODEL])
        self.assertEqual([item["displayName"] for item in self.fake.collection("reports")], [powerbi.REPORT])


REPORT_TABLES = ("success_measures", "measures_by_plant", "supplier_completeness", "passport_status_current",
                 "lot_claim_status_current")
REPORT_PAGES = ["overview", "readiness", "suppliers", "footprint", "finance", "trust"]


class ReportingDefinitionTests(unittest.TestCase):
    """The views, the semantic model and the report refer only to reporting tables and their columns."""

    def test_each_view_selects_only_columns_of_the_reporting_tables(self):
        text = (fabric.HERE / "sql" / "views.sql").read_text(encoding="utf-8")
        views = [batch for batch in text.split("\nGO") if "CREATE OR ALTER VIEW" in batch]
        self.assertEqual([re.search(r"VIEW dbo\.(\w+)", view).group(1) for view in views],
                         ["v_passport_status_current", "v_lot_claim_status", "v_measures_by_plant",
                          "v_supplier_completeness", "v_success_measures"])
        for view in views:
            body = re.sub(r"'[^']*'", "", view.split(" AS\n", 1)[1])
            sources = re.findall(r"FROM dbo\.(\w+)", body)
            self.assertTrue(sources and set(sources) <= set(TABLES["reporting"]), view)
            known = {name for source in sources for name, _ in TABLES["reporting"][source]}
            aliases = set(re.findall(r"AS (\w+)", body))
            names = set(re.findall(r"\b[a-z_][a-z0-9_]*\b", body)) - set(sources) - {"dbo"}
            self.assertEqual(names - known - aliases, set(), view.splitlines()[0])

    def test_the_model_tables_match_the_reporting_tables(self):
        folder = powerbi.FOLDER / "definition" / "tables"
        self.assertEqual(sorted(path.stem for path in folder.glob("*.tmdl")), sorted(REPORT_TABLES))
        for name in REPORT_TABLES:
            text = (folder / f"{name}.tmdl").read_text(encoding="utf-8")
            self.assertEqual(re.findall(r"^\tcolumn (\w+)$", text, re.MULTILINE),
                             [column for column, _ in TABLES["reporting"][name]])
            self.assertIn(f"entityName: {name}\n", text)
            for table, column in re.findall(r"\b(\w+)\[(\w+)\]", text):
                self.assertEqual(table, name)
                self.assertIn(column, dict(TABLES["reporting"][name]))
        self.assertRegex((folder / "success_measures.tmdl").read_text(encoding="utf-8"),
                         r"(?s)'Passport coverage'.*'Verified footprint batteries'.*'Supplier completeness'"
                         r".*'Second life value EUR illustrative'")

    def test_a_measure_that_ends_in_zero_is_blank_without_an_export(self):
        for path in (powerbi.FOLDER / "definition" / "tables").glob("*.tmdl"):
            text = path.read_text(encoding="utf-8")
            for block in re.split(r"(?m)^\t(?=measure )", text)[1:]:
                if "+ 0" in block:
                    self.assertIn("BLANK()", block, f"{path.stem}: {block.splitlines()[0]}")

    def test_the_report_has_six_audience_pages_on_reporting_tables(self):
        folder = powerbi.REPORT_FOLDER
        model = {path.stem: path.read_text(encoding="utf-8") for path in
                 (powerbi.FOLDER / "definition" / "tables").glob("*.tmdl")}
        pages = json.loads((folder / "definition" / "pages" / "pages.json").read_text(encoding="utf-8"))
        self.assertEqual((pages["pageOrder"], pages["activePageName"]), (REPORT_PAGES, "overview"))
        self.assertEqual(sorted(path.name for path in (folder / "definition" / "pages").iterdir()),
                         sorted(REPORT_PAGES + ["pages.json"]))
        for page in REPORT_PAGES:
            layout = json.loads((folder / "definition" / "pages" / page / "page.json").read_text(encoding="utf-8"))
            self.assertEqual((layout["name"], layout["width"], layout["height"]), (page, 1600, 900))
            visuals = [json.loads(path.read_text(encoding="utf-8")) for path in
                       sorted((folder / "definition" / "pages" / page / "visuals").glob("*/visual.json"))]
            self.assertTrue(1 <= sum("query" in v["visual"] for v in visuals) <= 6, page)
            boxes = []
            for visual in visuals:
                name = f"{page}/{visual['name']}"
                position = visual["position"]
                box = (position["x"], position["y"], position["x"] + position["width"],
                       position["y"] + position["height"])
                self.assertTrue(0 <= box[0] < box[2] <= 1600 and 0 <= box[1] < box[3] <= 900, name)
                if visual["visual"]["visualType"] == "shape":
                    continue
                for other in boxes:
                    self.assertTrue(box[2] <= other[0] or other[2] <= box[0] or box[3] <= other[1]
                                    or other[3] <= box[1], name)
                boxes.append(box)
                self.check_visual(name, visual, model)
        binding = json.loads((folder / "definition.pbir").read_text(encoding="utf-8"))
        self.assertEqual(binding["datasetReference"]["byConnection"]["connectionString"],
                         "semanticmodelid={{semantic_model_id}}")
        with self.assertRaisesRegex(MvpError, "semantic model ID"):
            powerbi.report_definition("not-an-id")

    def check_visual(self, name, visual, model):
        content = visual["visual"]
        if content["visualType"] == "textbox":
            self.assertNotIn("query", content, name)
            self.assertIsInstance(content["objects"]["general"][0]["properties"]["paragraphs"], list)
            return
        if content["visualType"] == "actionButton":
            link = content["visualContainerObjects"]["visualLink"][0]["properties"]
            self.assertEqual(link["type"]["expr"]["Literal"]["Value"], "'PageNavigation'", name)
            self.assertIn(link["navigationSection"]["expr"]["Literal"]["Value"][1:-1], REPORT_PAGES, name)
            return
        title = content["visualContainerObjects"]["title"][0]["properties"]["text"]["expr"]
        self.assertRegex(title["Literal"]["Value"], r"synthetic|illustrative|assumption", name)
        self.assertNotIn("'", title["Literal"]["Value"][1:-1], name)
        for role in content["query"]["queryState"].values():
            for projection in role["projections"]:
                [(kind, field)] = projection["field"].items()
                table = field["Expression"]["SourceRef"]["Entity"]
                self.assertIn(table, REPORT_TABLES, name)
                if kind == "Column":
                    self.assertIn(field["Property"], dict(TABLES["reporting"][table]), name)
                else:
                    self.assertIn(f"measure '{field['Property']}' = ", model[table], name)


class SeedTests(FabricCase):
    def test_the_app_imports_the_package_and_the_evidence_once(self):
        result = self.seed()
        self.assertEqual((result["import_outcome"], result["evidence"], result["claims"], result["rebuilt"]),
                         ("imported", 3, 14, False))
        self.assertEqual(self.app.uploaded[-1], f"{result['build_id']}/manifest.json")
        self.assertEqual(len(self.app.service.list_evidence()), 17)  # three documents, 14 claims
        again = self.seed()
        self.assertEqual(again["build_id"], result["build_id"])
        self.assertEqual(len(self.app.uploaded), 5)  # four data files, then the manifest
        self.assertEqual(len(self.app.service.list_evidence()), 17)
        self.assertEqual(len(self.app.connector.messages()), 14)
        self.assertIn("Evidence: 3 documents (3 already uploaded).", self.printed)
        self.assertEqual(sum(event["action"] == "package_imported" for event in self.app.service.audit()), 1)

    def test_a_newer_package_in_the_app_leads_to_one_new_build(self):
        newer = supply.prepare(self.config, new_build=True, min_sequence=10 ** 8, output=self.other)
        for name, data in read_folder(simulator.build_paths(self.other, newer)["package"]).items():
            self.app.blobs.put("intake", f"{newer['build_id']}/{name}", data)
        self.assertEqual(self.app.service.import_package(newer["build_id"], ACTOR, "import-newer-0001")[0], 200)
        result = self.seed()
        self.assertEqual((result["import_outcome"], result["rebuilt"], result["build_sequence"]),
                         ("imported", True, 10 ** 8 + 1))
        self.assertIn(f"The app answered stale_package; preparing a new build after {newer['build_id']}.",
                      self.printed)

    def test_a_missing_app_role_says_how_to_retry(self):
        def refuse(url, body, token):
            return 403, json.dumps({"error": "forbidden", "message": "Passport.Operator is required."})

        with self.assertRaisesRegex(MvpError, r"The package import of BUILD-MAIN-[0-9A-F]+ answered 403: forbidden "
                                              r"Passport\.Operator is required\. Your token has no Passport app role "
                                              r"yet; .* rerun with --from B6\.$"):
            self.seed(post=refuse)

    def test_an_intake_blob_with_other_content_is_not_overwritten(self):
        state = supply.prepare(self.config, output=self.output)
        self.app.blobs.put("intake", f"{state['build_id']}/batches.jsonl", b"[]")
        with self.assertRaisesRegex(MvpError, rf"intake/{state['build_id']}/batches\.jsonl already exists with "
                                              "other content"):
            self.seed()
        self.assertEqual(self.app.uploaded, [])
        self.assertEqual(self.app.blobs.get("intake", f"{state['build_id']}/batches.jsonl"), b"[]")

    def test_the_app_status_report_fits_the_fabric_export_and_the_measure_tables_match_the_app(self):
        self.supply()
        self.seed()
        state = {"battery_id": "B-1", "model_id": "M", "plant_id": "P", "batch_id": "BATCH", "chemistry": "NMC",
                 "rated_kwh": 1.0, "eligibility": "eligible", "hold_reason": None, "lifecycle_status": "active",
                 "committed_version": 0, "footprint_per_kwh": None}
        self.assertEqual(tuple(domain.report_row(state, None, None, {})), STATUS_COLUMNS)
        lot = {"lot_id": "LOT-1", "supplier_id": "SUP-1", "material": "lithium", "present_fields": "",
               "required_fields": ""}
        self.assertEqual(tuple(domain.lot_claim_row(lot)), LOT_CLAIM_COLUMNS)
        report = self.app.service.report_export()
        self.assertEqual(len(report["rows"]), 5)
        result = self.publish(get=self.app.get)
        self.assertRegex(result["export_id"], EXPORT_ID)
        self.assertEqual((result["rows"], result["lot_claims"]), (5, len(report["lot_claims"])))
        expected = measure_tables(report["rows"], report["lot_claims"],
                                  {"export_id": result["export_id"], "generated_utc": report["generated_utc"]})
        for table, rows in expected.items():
            self.assertEqual(self.fake.table("reporting", table), json.loads(json.dumps(rows)), table)
        app = metrics.measures(report["rows"], report["lot_claims"])
        snapshot = exec_views.series_snapshot(self.app.service)
        [success] = self.fake.table("reporting", "success_measures")
        self.assertEqual((success["coverage_pct"], success["verified_footprint_batteries"],
                          success["supplier_completeness_pct"], success["second_life_count"],
                          success["second_life_value_eur_illustrative"]),
                         (snapshot["coverage_pct"], snapshot["verified_footprint_batteries"],
                          snapshot["supplier_completeness_pct"], snapshot["second_life_count"],
                          snapshot["second_life_value_eur_illustrative"]))
        self.assertEqual([{key: row[key] for key in app["plants"][0]}
                          for row in self.fake.table("reporting", "measures_by_plant")], snapshot["plants"])
        self.assertEqual([row["plant_id"] for row in self.fake.table("reporting", "measures_by_plant")],
                         ["PLANT-DE", "PLANT-PL", "PLANT-SE"])


class MultipartTests(unittest.TestCase):
    def test_the_evidence_upload_is_a_well_formed_multipart_form(self):
        captured = {}

        def fake_open(request, timeout):
            captured["request"] = request
            return 201, '{"evidence_id": "EV-1"}'

        with mock.patch.object(smoke, "_open", fake_open):
            answer = smoke.http_post_multipart(f"{APP_URL}/evidence", {"kind": "supplier_claim", "lot_id": "LOT-LI-A",
                                                                       "command_id": "seed-evidence-0123"},
                                               ("file", "claim.json", "application/json", b'{"a": 1}\r\n'), "tok")
        self.assertEqual(answer, (201, '{"evidence_id": "EV-1"}'))
        request = captured["request"]
        self.assertEqual(request.get_header("Authorization"), "Bearer tok")
        header = f"Content-Type: {request.get_header('Content-type')}\r\n\r\n".encode()
        message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(header + request.data)
        parts = {part.get_param("name", header="content-disposition"): part for part in message.iter_parts()}
        self.assertEqual({name: parts[name].get_content() for name in ("kind", "lot_id", "command_id")},
                         {"kind": "supplier_claim", "lot_id": "LOT-LI-A", "command_id": "seed-evidence-0123"})
        self.assertEqual(parts["file"].get_filename(), "claim.json")
        self.assertEqual(parts["file"].get_content_type(), "application/json")
        self.assertEqual(parts["file"].get_payload(decode=True), b'{"a": 1}\r\n')


if __name__ == "__main__":
    unittest.main()
