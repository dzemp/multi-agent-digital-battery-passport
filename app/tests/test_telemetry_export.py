"""Exercise configured app telemetry through the installed SDK and a loopback HTTP collector, never Azure."""

import gzip
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import helpers

APP_ROOT = Path(__file__).resolve().parents[1]
PRIVATE = "private-reviewer-or-evidence-must-not-be-exported"


class Collector(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if self.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        envelopes = json.loads(body)
        self.server.envelopes.extend(envelopes)
        reply = json.dumps({"itemsReceived": len(envelopes), "itemsAccepted": len(envelopes), "errors": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *_):
        pass


def export_probe():
    from fastapi.testclient import TestClient
    from opentelemetry import trace
    from opentelemetry._logs import get_logger_provider
    from opentelemetry.metrics import get_meter_provider

    from passport.blobs import LocalBlobs
    from passport.config import Settings
    from passport.service import PassportService
    from passport.store import MemoryStore
    from passport.web import create_app

    with tempfile.TemporaryDirectory() as root:
        service = PassportService(MemoryStore(), LocalBlobs(root))
        helpers.put_package(service.blobs, "BUILD-03", helpers.standard_package())
        assert service.import_package("BUILD-03", "operator (local:test)", "import-export-test")[0] == 200
        service.drain_outbox()
        app = create_app(Settings(auth_mode="local", worker=False), service)
        assert app.state.telemetry.enabled
        with TestClient(app) as client:
            response = client.get("/exec", headers={"x-local-user": PRIVATE, "x-local-roles": ""})
            assert response.status_code == 403
            app.state.events.snapshots(force=True)
            # Business events must survive an unsampled request and still carry its correlation ID.
            with trace.get_tracer("export-test").start_as_current_span("unsampled") as span:
                assert not span.get_span_context().trace_flags.sampled
                app.state.telemetry.emit("agent.run", role="carbon", model="test-model",
                                         input_tokens=30, output_tokens=10, latency_ms=12,
                                         estimated_cost_usd=0.003, price_version="test-only")
                app.state.telemetry.emit("agent.run", role="lifecycle", model="test-model",
                                         input_tokens=None, output_tokens=None, estimated_cost_usd=None)
            assert app.state.telemetry.emit("hold.resolved", reason=PRIVATE) is None

        assert get_logger_provider().force_flush(timeout_millis=10000)
        get_logger_provider().shutdown()
        trace.get_tracer_provider().shutdown()
        get_meter_provider().shutdown()


class ConfiguredExportTests(unittest.TestCase):
    def test_normal_app_install_exports_minimized_custom_events_to_loopback(self):
        # A child keeps the process-global OTel providers/instrumentation out of the other app tests.
        with HTTPServer(("127.0.0.1", 0), Collector) as server, tempfile.TemporaryDirectory() as tmp:
            server.envelopes = []
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            endpoint = f"http://127.0.0.1:{server.server_port}"
            env = {k: v for k, v in os.environ.items()
                   if k.upper() in ("PATH", "SYSTEMROOT", "WINDIR", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOME")}
            env.update({
                "TEMP": tmp, "TMP": tmp, "TMPDIR": tmp,
                "PYTHONPATH": str(APP_ROOT), "PYTHONDONTWRITEBYTECODE": "1",
                "NO_PROXY": "*",
                "APPLICATIONINSIGHTS_CONNECTION_STRING":
                    f"InstrumentationKey=00000000-0000-0000-0000-000000000001;IngestionEndpoint={endpoint}",
                "APPLICATIONINSIGHTS_STATSBEAT_DISABLED_ALL": "true",
                "APPLICATIONINSIGHTS_SDKSTATS_DISABLED": "true",
                "OTEL_TRACES_SAMPLER": "always_off",
            })
            try:
                result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--export-probe"],
                                        cwd=APP_ROOT, env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            finally:
                server.shutdown()
                thread.join(timeout=5)

        events = [e for e in server.envelopes if e["data"]["baseType"] == "EventData"]
        rows = [e["data"]["baseData"] for e in events]
        self.assertCountEqual([row["name"] for row in rows],
                              ["access.denied", "passport.series.snapshot", "passport.plant.snapshot",
                               "passport.plant.snapshot", "agent.run", "agent.run"])
        self.assertNotIn(PRIVATE, json.dumps(server.envelopes))
        self.assertEqual(len({row["properties"]["event_id"] for row in rows}), 6)
        for event, row in zip(events, rows, strict=True):
            self.assertEqual(event["tags"]["ai.cloud.role"], "passport-app")
            self.assertEqual(row["properties"]["dbp.schema_version"], "2")
            self.assertNotIn("microsoft.custom_measurements", row["properties"])
        denied = next(row for row in rows if row["name"] == "access.denied")
        self.assertEqual(denied["properties"]["route_template"], "/exec")
        self.assertEqual(denied["properties"]["actor_role"], "no_role")
        snapshot = next(row for row in rows if row["name"] == "passport.series.snapshot")
        self.assertEqual(snapshot["properties"]["series_id"], "SERIES-V3")
        self.assertEqual(snapshot["measurements"]["total_batteries"], 3)
        self.assertEqual(snapshot["measurements"]["published_valid"], 3)
        self.assertEqual(snapshot["measurements"]["stuck_batteries"], 0)
        self.assertEqual(snapshot["measurements"]["coverage_pct"], 100)
        plants = [row for row in rows if row["name"] == "passport.plant.snapshot"]
        self.assertTrue(all(row["properties"]["plant_id"] for row in plants))
        self.assertEqual(sum(row["measurements"]["produced"] for row in plants), 3)
        measured = next(row for row in rows if row["properties"].get("role") == "carbon")
        self.assertEqual(measured["measurements"],
                         {"input_tokens": 30, "output_tokens": 10, "latency_ms": 12, "estimated_cost_usd": 0.003})
        unknown = next(row for row in rows if row["properties"].get("role") == "lifecycle")
        self.assertEqual(unknown.get("measurements", {}), {})
        for event in events:
            if event["data"]["baseData"]["name"] == "agent.run":
                self.assertNotEqual(event["tags"]["ai.operation.id"], "0" * 32)


if __name__ == "__main__":
    if sys.argv[1:] == ["--export-probe"]:
        export_probe()
    else:
        unittest.main()
