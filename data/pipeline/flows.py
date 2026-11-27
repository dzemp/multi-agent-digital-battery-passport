"""The supply and status flows over a lakehouse-like store: ingest files -> bronze -> silver -> gold package, and the
app's report export v2 -> reporting tables and the measure tables of data/metrics.py.

Standard library only. The Fabric notebooks embed this text after rules.py and deliveries.py, with data/metrics.py,
and pass a Spark-backed store; the simulator and the tests pass FolderStore. Both run exactly these functions.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from ..metrics import measures, produced, second_life
from .deliveries import DELIVERY_MANIFEST, check_delivery
from .rules import build_package, build_suffix, canonical, derive, digest, identifier, lot_completeness, utc
from .rules import merge_records, resolve

# The app's report rows (passport.domain.report_row and lot_claim_row), in order; a test ties them together.
STATUS_TYPES = (("battery_id", "string"), ("model_id", "string"), ("plant_id", "string"), ("batch_id", "string"),
                ("chemistry", "string"), ("rated_kwh", "double"), ("eligibility", "string"),
                ("hold_reason", "string"), ("lifecycle_status", "string"), ("committed_version", "long"),
                ("committed_utc", "string"), ("snapshot_state", "string"), ("footprint_per_kwh", "double"),
                ("footprint_verified", "long"), ("recycled_co_pct", "double"), ("recycled_li_pct", "double"),
                ("recycled_ni_pct", "double"), ("successor_id", "string"),
                ("second_life_value_eur_illustrative", "double"), ("registry_status", "string"),
                ("data_label", "string"))
STATUS_COLUMNS = tuple(name for name, _ in STATUS_TYPES)
LOT_CLAIM_TYPES = (("lot_id", "string"), ("supplier_id", "string"), ("material", "string"),
                   ("claim_status", "string"), ("case_state", "string"), ("fields_complete", "long"),
                   ("data_label", "string"))
LOT_CLAIM_COLUMNS = tuple(name for name, _ in LOT_CLAIM_TYPES)
STAMP_TYPES = (("export_id", "string"), ("generated_utc", "string"))
TABLES = {
    "bronze": {
        "deliveries": (("delivery_id", "string"), ("party_id", "string"), ("kind", "string"), ("format", "string"),
                       ("unit", "string"), ("status", "string"), ("reason", "string"), ("received_utc", "string"),
                       ("manifest_sha256", "string"), ("manifest_json", "string")),
        "raw_records": (("delivery_id", "string"), ("party_id", "string"), ("file", "string"), ("line_no", "long"),
                        ("record_json", "string"), ("record_sha256", "string")),
    },
    "silver": {
        "records": (("build_id", "string"), ("position", "long"), ("delivery_id", "string"), ("party_id", "string"),
                    ("entity", "string"), ("source_key", "string"), ("incoming_version", "long"),
                    ("previous_version", "long"), ("record_hash", "string"), ("outcome", "string"),
                    ("record_json", "string")),
        "record_versions": (("build_id", "string"), ("entity", "string"), ("source_key", "string"),
                            ("version", "long"), ("status", "string"), ("record_hash", "string")),
        "issues": (("build_id", "string"), ("entity", "string"), ("source_key", "string"),
                   ("source_version", "long"), ("reason", "string"), ("owner", "string")),
        "lots": (("build_id", "string"), ("lot_id", "string"), ("version", "long"), ("supplier_id", "string"),
                 ("material", "string"), ("region", "string"), ("origin_site", "string"),
                 ("source_quantity", "double"), ("source_unit", "string"), ("mass_kg", "double"),
                 ("recycled_share_pct", "double"), ("pcf_kgco2e_per_kg", "double"), ("claim_id", "string"),
                 ("claim_sha256", "string"), ("na_reasons", "string"), ("party", "string")),
        "lot_completeness": (("build_id", "string"), ("lot_id", "string"), ("supplier_id", "string"),
                             ("material", "string"), ("required_fields", "string"), ("present_fields", "string"),
                             ("claim_ref_present", "boolean")),
        "batches": (("build_id", "string"), ("batch_id", "string"), ("version", "long"), ("plant_id", "string"),
                    ("method_version", "string"), ("factor_set_version", "string")),
        "builds": (("build_id", "string"), ("battery_id", "string"), ("version", "long"), ("batch_id", "string"),
                   ("plant_id", "string"), ("model_id", "string"), ("chemistry", "string"), ("rated_kwh", "double"),
                   ("expected_cycles", "long")),
        "consumption": (("build_id", "string"), ("consumption_id", "string"), ("version", "long"),
                        ("battery_id", "string"), ("lot_id", "string"), ("mass_kg", "double")),
        "energy": (("build_id", "string"), ("energy_id", "string"), ("version", "long"), ("plant_id", "string"),
                   ("batch_id", "string"), ("kwh", "double"), ("period_start", "string"), ("period_end", "string")),
        "transport": (("build_id", "string"), ("transport_id", "string"), ("version", "long"), ("lot_id", "string"),
                      ("origin_site", "string"), ("destination_plant_id", "string"), ("shipped_utc", "string"),
                      ("delivered_utc", "string"), ("carrier_id", "string")),
    },
    "gold": {
        "battery_inputs": (("build_id", "string"), ("battery_id", "string"), ("series_id", "string"),
                           ("batch_id", "string"), ("model_id", "string"), ("plant_id", "string"),
                           ("chemistry", "string"), ("rated_kwh", "double"), ("allocated_kwh", "double"),
                           ("grid_factor_id", "string"), ("readiness", "string"), ("reasons", "string"),
                           ("input_hash", "string")),
        "battery_composition": (("build_id", "string"), ("battery_id", "string"), ("material", "string"),
                                ("lot_id", "string"), ("mass_kg", "double")),
        "complete_builds": (("build_id", "string"), ("build_sequence", "long"), ("manifest_sha256", "string"),
                            ("battery_count", "long"), ("completed_utc", "string")),
    },
    "reporting": {
        "passport_status_history": STAMP_TYPES + STATUS_TYPES,
        "passport_status_current": STAMP_TYPES + STATUS_TYPES,
        "lot_claim_status_history": STAMP_TYPES + LOT_CLAIM_TYPES,
        "lot_claim_status_current": STAMP_TYPES + LOT_CLAIM_TYPES,
        "measures_by_plant": STAMP_TYPES + (
            ("plant_id", "string"), ("produced", "long"), ("published", "long"), ("coverage_pct", "double"),
            ("verified_footprint_batteries", "long"), ("avg_footprint_per_kwh", "double"),
            ("second_life_count", "long"), ("second_life_value_eur_illustrative", "double"),
            ("data_label", "string")),
        "supplier_completeness": STAMP_TYPES + (
            ("supplier_id", "string"), ("material", "string"), ("lots", "long"), ("complete", "long"),
            ("complete_pct", "double"), ("data_label", "string")),
        "success_measures": STAMP_TYPES + (
            ("produced", "long"), ("published", "long"), ("coverage_pct", "double"),
            ("verified_footprint_batteries", "long"), ("verified_pct", "double"),
            ("avg_kg_co2e_per_kwh", "double"), ("lots", "long"), ("complete_lots", "long"),
            ("supplier_completeness_pct", "double"), ("second_life_count", "long"),
            ("second_life_value_eur_illustrative", "double"), ("not_in_export", "long"), ("data_label", "string")),
    },
}
EXPORT_ID = re.compile(r"EXP-[0-9]{8}T[0-9]{6}Z")
EXPORT_FILE = "passport_status.jsonl"
LOT_CLAIMS_FILE = "lot_claims.jsonl"
EXPORT_SCHEMA = "2"
EXPORT_KEYS = {"schema_version", "export_id", "generated_utc", "columns", "lot_claim_columns", "files", "data_label"}
EXPORT_PARTS = ((EXPORT_FILE, STATUS_COLUMNS), (LOT_CLAIMS_FILE, LOT_CLAIM_COLUMNS))


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def shape(lakehouse, table, rows):
    """Rows with exactly the table's columns and types; lists and objects become canonical JSON text."""
    columns = TABLES[lakehouse][table]
    names = {name for name, _ in columns}
    shaped = []
    for row in rows:
        if set(row) != names:
            raise ValueError(f"{lakehouse}.{table}: columns differ ({sorted(set(row) ^ names)})")
        values = {}
        for name, kind in columns:
            value = row[name]
            if isinstance(value, (list, dict)):
                value = canonical(value)
            if value is None:
                pass
            elif kind == "string" and not isinstance(value, str):
                raise TypeError(f"{lakehouse}.{table}.{name}: expected text, got {type(value).__name__}")
            elif kind == "long":
                if isinstance(value, bool) or not isinstance(value, int):
                    raise TypeError(f"{lakehouse}.{table}.{name}: expected a whole number")
            elif kind == "double":
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise TypeError(f"{lakehouse}.{table}.{name}: expected a number")
                value = float(value)
            elif kind == "boolean" and not isinstance(value, bool):
                raise TypeError(f"{lakehouse}.{table}.{name}: expected true or false")
            values[name] = value
        shaped.append(values)
    return shaped


class FolderStore:
    """A lakehouse stand-in on disk: <root>/<lakehouse>/Tables/<table>.json and <root>/<lakehouse>/Files/<path>."""

    def __init__(self, root):
        self.root = Path(root)

    def _file(self, lakehouse, path):
        parts = path.split("/")
        if (lakehouse not in TABLES and lakehouse != "ingest") or any(p in ("", ".", "..") for p in parts):
            raise ValueError(f"Invalid store path: {lakehouse}/{path}")
        return self.root.joinpath(lakehouse, "Files", *parts)

    def read_table(self, lakehouse, table):
        path = self.root / lakehouse / "Tables" / f"{table}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

    def replace_rows(self, lakehouse, table, rows):
        path = self.root / lakehouse / "Tables" / f"{table}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(shape(lakehouse, table, rows), indent=1, sort_keys=True), encoding="utf-8")

    def read_file(self, lakehouse, path):
        target = self._file(lakehouse, path)
        return target.read_bytes() if target.is_file() else None

    def write_file(self, lakehouse, path, data):
        target = self._file(lakehouse, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def list_dir(self, lakehouse, path):
        target = self._file(lakehouse, path)
        return sorted(item.name for item in target.iterdir()) if target.is_dir() else []


def _replace_build(store, lakehouse, table, build_id, rows):
    kept = [row for row in store.read_table(lakehouse, table) if row["build_id"] != build_id]
    store.replace_rows(lakehouse, table, kept + list(rows))


def clear_tables(store):
    """Replace the current contents of every MVP-owned Delta table while preserving its schema."""
    for lakehouse, tables in TABLES.items():
        for table in tables:
            store.replace_rows(lakehouse, table, [])
    return {"tables": sum(len(tables) for tables in TABLES.values())}


def bronze_load(store, delivery_set, registry, received_utc):
    """Check each delivery in ingest once. An accepted delivery is never reloaded; a rejected one is checked again."""
    utc(received_utc)
    known = {row["delivery_id"]: row for row in store.read_table("bronze", "deliveries")}
    raw = store.read_table("bronze", "raw_records")
    results, changed = [], False
    for party_id, delivery_id in delivery_set:
        folder = f"deliveries/{party_id}/{delivery_id}"
        files = {name: store.read_file("ingest", f"{folder}/{name}") for name in store.list_dir("ingest", folder)}
        files = {name: data for name, data in files.items() if data is not None}
        sha = digest(files[DELIVERY_MANIFEST]) if DELIVERY_MANIFEST in files else None
        previous = known.get(delivery_id)
        if previous and previous["status"] == "accepted":
            if previous["manifest_sha256"] != sha:
                raise ValueError(f"delivery_changed:{delivery_id} was accepted with another delivery.json; a "
                                 "delivery is never replaced. Send a new delivery with a new ID.")
            results.append({"delivery_id": delivery_id, "status": "accepted", "loaded": False})
            continue
        check = check_delivery(party_id, delivery_id, files, registry)
        manifest = check["manifest"] or {}
        known[delivery_id] = {
            "delivery_id": delivery_id, "party_id": party_id, "kind": manifest.get("kind"),
            "format": manifest.get("format"), "unit": manifest.get("unit"), "status": check["status"],
            "reason": check["reason"], "received_utc": received_utc, "manifest_sha256": sha,
            "manifest_json": canonical(manifest) if manifest else None}
        raw = [row for row in raw if row["delivery_id"] != delivery_id]
        for name, line_no, record in check["records"]:
            text = canonical(record)
            raw.append({"delivery_id": delivery_id, "party_id": party_id, "file": name, "line_no": line_no,
                        "record_json": text, "record_sha256": digest(text.encode("utf-8"))})
        changed = True
        results.append({"delivery_id": delivery_id, "status": check["status"], "reason": check["reason"],
                        "loaded": True, "records": len(check["records"])})
    if changed:
        store.replace_rows("bronze", "deliveries", sorted(known.values(), key=lambda row: row["delivery_id"]))
        store.replace_rows("bronze", "raw_records",
                           sorted(raw, key=lambda row: (row["delivery_id"], row["file"], row["line_no"])))
    return results


def silver_normalise(store, delivery_set, build_id, registry):
    """Version outcomes, current records and issues for one build, from the accepted deliveries in set order."""
    identifier(build_id)
    deliveries = {row["delivery_id"]: row for row in store.read_table("bronze", "deliveries")}
    raw = store.read_table("bronze", "raw_records")
    incoming = []
    for party_id, delivery_id in delivery_set:
        row = deliveries.get(delivery_id)
        if row is None or row["party_id"] != party_id:
            raise ValueError(f"{party_id}/{delivery_id} is not in bronze; run bronze_load first.")
        if row["status"] != "accepted":
            continue
        for item in sorted((r for r in raw if r["delivery_id"] == delivery_id),
                           key=lambda r: (r["file"], r["line_no"])):
            incoming.append((delivery_id, party_id, item["record_json"]))
    records = [json.loads(text) for _, _, text in incoming]
    state, log, _ = merge_records({}, records)
    current, issues, vector = resolve(records, registry)
    log_rows = [{**entry, "build_id": build_id, "delivery_id": incoming[entry["position"]][0],
                 "party_id": incoming[entry["position"]][1], "record_json": incoming[entry["position"]][2]}
                for entry in log]
    issue_keys = {(item["entity"], item["source_key"]) for item in issues}
    versions = []
    for name, version in sorted(vector.items()):
        entity, key = name.split(":", 1)
        payloads = state[(entity, key)]["payloads"]
        versions.append({"build_id": build_id, "entity": entity, "source_key": key, "version": version,
                         "status": "issue" if (entity, key) in issue_keys else "current",
                         "record_hash": next(iter(payloads)) if len(payloads) == 1 else None})
    tables = {"records": log_rows, "record_versions": versions,
              "issues": [{"build_id": build_id, **item} for item in issues],
              "lot_completeness": [{"build_id": build_id, **row} for row in lot_completeness(current)],
              **_silver_entities(current, build_id)}
    for table, rows in tables.items():
        _replace_build(store, "silver", table, build_id, rows)
    return {"build_id": build_id, "records": len(records), "current": len(current), "issues": len(issues),
            "outcomes": {outcome: sum(1 for e in log if e["outcome"] == outcome)
                         for outcome in sorted({e["outcome"] for e in log})}}


def _silver_entities(current, build_id):
    """Silver tables of the current valid records, one per kind of delivery content, quantities in kg and kWh."""
    def rows(entity, make):
        return [{"build_id": build_id, "version": r["version"], **make(key, r)}
                for (kind, key), r in sorted(current.items()) if kind == entity]

    return {
        "lots": rows("lot", lambda key, r: {
            "lot_id": key, "supplier_id": r["supplier_id"], "material": r["material"], "region": r["region"],
            "origin_site": r.get("origin_site"), "source_quantity": float(r["quantity"]), "source_unit": r["unit"],
            "mass_kg": r["quantity_kg"], "recycled_share_pct": r["recycled_share_pct"],
            "pcf_kgco2e_per_kg": r["pcf_kgco2e_per_kg"], "claim_id": r.get("claim_id"),
            "claim_sha256": r.get("claim_sha256"), "na_reasons": r["na_reasons"], "party": r["party"]}),
        "transport": rows("transport", lambda key, r: {
            "transport_id": key, "lot_id": r["lot_id"], "origin_site": r["origin_site"],
            "destination_plant_id": r["destination_plant_id"], "shipped_utc": r["shipped_utc"],
            "delivered_utc": r["delivered_utc"], "carrier_id": r["party"]}),
        "batches": rows("batch", lambda key, r: {
            "batch_id": key, "plant_id": r["plant_id"], "method_version": r["method_version"],
            "factor_set_version": r["factor_set_version"]}),
        "builds": rows("battery", lambda key, r: {
            "battery_id": key, "batch_id": r["batch_id"], "plant_id": r["plant_id"], "model_id": r["model_id"],
            "chemistry": r["chemistry"], "rated_kwh": r["rated_kwh"], "expected_cycles": r["expected_cycles"]}),
        "consumption": rows("consumption", lambda key, r: {
            "consumption_id": key, "battery_id": r["battery_id"], "lot_id": r["lot_id"],
            "mass_kg": r["quantity_kg"]}),
        "energy": rows("batch_energy", lambda key, r: {
            "energy_id": key, "plant_id": r["party"], "batch_id": r["batch_id"], "kwh": r["kwh"],
            "period_start": r["interval_start"], "period_end": r["interval_end"]}),
    }


def _record_build(store, manifest, manifest_sha256, batteries, composition):
    build_id = manifest["build_id"]
    fields = ("battery_id", "series_id", "batch_id", "model_id", "plant_id", "chemistry", "rated_kwh",
              "allocated_kwh", "grid_factor_id", "readiness", "reasons", "input_hash")
    _replace_build(store, "gold", "battery_inputs", build_id,
                   [{"build_id": build_id, **{field: b[field] for field in fields}} for b in batteries])
    _replace_build(store, "gold", "battery_composition", build_id,
                   [{"build_id": build_id, **{field: c[field] for field in ("battery_id", "material", "lot_id",
                                                                             "mass_kg")}} for c in composition])
    _replace_build(store, "gold", "complete_builds", build_id, [{
        "build_id": build_id, "build_sequence": manifest["build_sequence"], "manifest_sha256": manifest_sha256,
        "battery_count": len(batteries), "completed_utc": manifest["completed_utc"]}])


def _package_sources(manifests):
    return max(m["created_utc"] for m in manifests), [entry["sha256"] for m in manifests for entry in m["files"]]


def _jsonl_rows(data):
    return [json.loads(line) for line in data.decode("utf-8").splitlines() if line]


def _check_build_id(build_id, delivery_hashes):
    """A build ID ends with the content suffix of its accepted deliveries and the rules version."""
    identifier(build_id)
    suffix = build_suffix(delivery_hashes)
    if not build_id.endswith("-" + suffix):
        raise ValueError(f"Build ID {build_id} does not match its deliveries and rules (expected suffix {suffix}).")


def gold_package(store, delivery_set, build_id, build_sequence, registry, code_hash, completed_utc, factors):
    """Write Files/packages/<build_id>/ manifest-last. A completed package is never rewritten."""
    identifier(build_id)
    folder = f"packages/{build_id}"
    existing = store.read_file("gold", f"{folder}/manifest.json")
    if existing is not None:
        manifest = json.loads(existing)
        batteries = _jsonl_rows(store.read_file("gold", f"{folder}/batteries.jsonl"))
        composition = _jsonl_rows(store.read_file("gold", f"{folder}/composition.jsonl"))
        _record_build(store, manifest, digest(existing), batteries, composition)
        return {"build_id": build_id, "status": "exists", "manifest_sha256": digest(existing)}
    deliveries = {row["delivery_id"]: row for row in store.read_table("bronze", "deliveries")}
    accepted = [deliveries[d] for _, d in delivery_set if d in deliveries and deliveries[d]["status"] == "accepted"]
    manifests = [json.loads(row["manifest_json"]) for row in accepted]
    silver = sorted((r for r in store.read_table("silver", "records") if r["build_id"] == build_id),
                    key=lambda r: r["position"])
    if not manifests or not silver:
        raise ValueError(f"No accepted records for {build_id}; run bronze_load and silver_normalise first.")
    _check_build_id(build_id, [row["manifest_sha256"] for row in accepted])
    as_of, source_hashes = _package_sources(manifests)
    result = derive([json.loads(r["record_json"]) for r in silver], as_of, registry, factors)
    files, manifest = build_package(result, build_id, build_sequence, code_hash, source_hashes, completed_utc,
                                    factors)
    for name, data in files.items():
        store.write_file("gold", f"{folder}/{name}", data)
    manifest_bytes = (canonical(manifest) + "\n").encode("utf-8")
    store.write_file("gold", f"{folder}/manifest.json", manifest_bytes)
    _record_build(store, manifest, digest(manifest_bytes), result["batteries"], result["composition"])
    return {"build_id": build_id, "status": "written", "manifest_sha256": digest(manifest_bytes),
            "totals": manifest["totals"]}


def local_package(deliveries, registry, build_id, build_sequence, code_hash, completed_utc, factors):
    """The same package in one pass, without a store: deliveries are [(party_id, delivery_id, {name: bytes})]."""
    manifests, records, hashes = [], [], []
    for party_id, delivery_id, files in deliveries:
        check = check_delivery(party_id, delivery_id, files, registry)
        if check["status"] != "accepted":
            raise ValueError(f"{party_id}/{delivery_id} was rejected: {check['reason']}")
        manifests.append(check["manifest"])
        hashes.append(check["manifest_sha256"])
        records += [record for _, _, record in sorted(check["records"], key=lambda item: item[:2])]
    _check_build_id(build_id, hashes)
    as_of, source_hashes = _package_sources(manifests)
    files, manifest = build_package(derive(records, as_of, registry, factors), build_id, build_sequence, code_hash,
                                    source_hashes, completed_utc, factors)
    return {**files, "manifest.json": (canonical(manifest) + "\n").encode("utf-8")}


def _jsonl(rows, columns, what):
    lines = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(columns):
            raise ValueError(f"A {what} row must have exactly the columns {', '.join(columns)}.")
        lines.append(json.dumps({column: row[column] for column in columns}, separators=(",", ":"), allow_nan=False))
    return ("\n".join(lines) + "\n").encode("utf-8") if lines else b""


def make_export(rows, lot_claims, export_id, generated_utc):
    """The app's report export v2 as Files/exports/<export_id>/: passport_status.jsonl, lot_claims.jsonl, then
    manifest.json with both files' hashes and counts."""
    if not isinstance(export_id, str) or not EXPORT_ID.fullmatch(export_id):
        raise ValueError(f"Invalid export ID: {export_id!r}")
    utc(generated_utc)
    if not rows:
        raise ValueError("The export has no rows.")
    files = {EXPORT_FILE: _jsonl(rows, STATUS_COLUMNS, "passport status"),
             LOT_CLAIMS_FILE: _jsonl(lot_claims, LOT_CLAIM_COLUMNS, "lot claim")}
    manifest = {"schema_version": EXPORT_SCHEMA, "export_id": export_id, "generated_utc": generated_utc,
                "columns": list(STATUS_COLUMNS), "lot_claim_columns": list(LOT_CLAIM_COLUMNS),
                "data_label": "synthetic",
                "files": [{"name": name, "sha256": digest(files[name]), "record_count": len(rows_)}
                          for (name, _), rows_ in zip(EXPORT_PARTS, (rows, lot_claims), strict=True)]}
    return {**files, "manifest.json": (canonical(manifest) + "\n").encode("utf-8")}


def read_export(files, export_id):
    """Verify an export folder and return (manifest, rows, lot_claims). Raises ValueError for anything unexpected."""
    if files.get("manifest.json") is None:
        raise ValueError(f"export {export_id}: no manifest.json; the upload did not finish.")
    try:
        return _read_export(files, export_id)
    except (AttributeError, KeyError, TypeError, IndexError, UnicodeDecodeError) as error:
        raise ValueError(f"export {export_id}: unreadable ({type(error).__name__}).") from error


def _read_export(files, export_id):
    manifest = json.loads(files["manifest.json"].decode("utf-8"))
    if isinstance(manifest, dict) and manifest.get("schema_version") != EXPORT_SCHEMA:
        raise ValueError(f"export {export_id}: schema version {manifest.get('schema_version')!r} is not read any "
                         f"more; the reporting tables need schema {EXPORT_SCHEMA}. Publish a new export with "
                         "python -m dbp_mvp report publish.")
    if (not isinstance(manifest, dict) or set(manifest) != EXPORT_KEYS or manifest["export_id"] != export_id
            or manifest["columns"] != list(STATUS_COLUMNS)
            or manifest["lot_claim_columns"] != list(LOT_CLAIM_COLUMNS)):
        raise ValueError(f"export {export_id}: the manifest does not follow schema {EXPORT_SCHEMA}.")
    utc(manifest["generated_utc"])
    if [entry.get("name") for entry in manifest["files"]] != [name for name, _ in EXPORT_PARTS]:
        raise ValueError(f"export {export_id}: expected exactly {EXPORT_FILE} and {LOT_CLAIMS_FILE}.")
    parts = []
    for (name, columns), entry in zip(EXPORT_PARTS, manifest["files"], strict=True):
        data = files.get(name)
        if data is None or digest(data) != entry["sha256"]:
            raise ValueError(f"export {export_id}: {name} is missing or changed.")
        rows = [json.loads(line) for line in data.decode("utf-8").splitlines() if line]
        if len(rows) != entry["record_count"]:
            raise ValueError(f"export {export_id}: the record count of {name} differs from the manifest.")
        for row in rows:
            if not isinstance(row, dict) or list(row) != list(columns):
                raise ValueError(f"export {export_id}: a row of {name} does not have exactly its columns in order.")
        parts.append(rows)
    if not parts[0]:
        raise ValueError(f"export {export_id}: {EXPORT_FILE} has no rows.")
    return manifest, parts[0], parts[1]


def _packaged_batteries(store):
    """Battery ID -> plant ID of every battery in a completed gold package (its latest build)."""
    builds = {row["build_id"]: row["build_sequence"] for row in store.read_table("gold", "complete_builds")}
    latest = {}
    for row in store.read_table("gold", "battery_inputs"):
        if row["build_id"] in builds and (row["battery_id"] not in latest
                                          or builds[row["build_id"]] > latest[row["battery_id"]][0]):
            latest[row["battery_id"]] = (builds[row["build_id"]], row["plant_id"])
    return {battery_id: plant for battery_id, (_, plant) in latest.items()}


def _with_history(store, table, export_id, stamped):
    """Replace the export's rows in a history table: a replayed export ID changes nothing."""
    history = [row for row in store.read_table("reporting", table) if row["export_id"] != export_id] + stamped
    history.sort(key=lambda r: (r["generated_utc"], r["export_id"], canonical(r)))
    store.replace_rows("reporting", table, history)
    return history


def measure_tables(rows, lot_claims, stamp, not_in_export=0):
    """success_measures, measures_by_plant and supplier_completeness rows from data/metrics.py."""
    result = measures(rows, lot_claims)
    by_plant = {}
    for row in produced(rows):
        by_plant.setdefault(row.get("plant_id") or "unknown", []).append(row)
    plants = [{**stamp, **plant, "second_life_count": second_life(by_plant[plant["plant_id"]])["repurposed"],
               "second_life_value_eur_illustrative":
                   second_life(by_plant[plant["plant_id"]])["value_eur_illustrative"], "data_label": "synthetic"}
              for plant in result["plants"]]
    suppliers = [{**stamp, **supplier, "data_label": "synthetic"}
                 for supplier in result["supplier_completeness"]["by_supplier"]]
    cover, footprint = result["coverage"], result["verified_footprint"]
    lots, life = result["supplier_completeness"], result["second_life"]
    success = [{**stamp, "produced": cover["produced"], "published": cover["published"],
                "coverage_pct": cover["coverage_pct"], "verified_footprint_batteries": footprint["verified"],
                "verified_pct": footprint["verified_pct"], "avg_kg_co2e_per_kwh": footprint["avg_kg_co2e_per_kwh"],
                "lots": lots["lots"], "complete_lots": lots["complete"],
                "supplier_completeness_pct": lots["complete_pct"], "second_life_count": life["repurposed"],
                "second_life_value_eur_illustrative": life["value_eur_illustrative"],
                "not_in_export": not_in_export, "data_label": "synthetic"}]
    return {"success_measures": success, "measures_by_plant": plants, "supplier_completeness": suppliers}


def reporting_measures(store, export_id):
    """Load an export v2 into the history (once per export ID), make the newest export the current status and
    compute the measure tables from it with data/metrics.py, the app's own measure definitions."""
    folder = f"exports/{export_id}"
    files = {name: store.read_file("reporting", f"{folder}/{name}")
             for name in ("manifest.json", EXPORT_FILE, LOT_CLAIMS_FILE)}
    manifest, rows, lot_claims = read_export(files, export_id)
    packaged = _packaged_batteries(store)
    unknown = sorted({row["battery_id"] for row in produced(rows)} - set(packaged))
    if unknown:
        raise ValueError(f"export {export_id}: {len(unknown)} batteries are not in a completed Fabric package "
                         f"({', '.join(unknown[:5])}). Run python -m dbp_mvp fabric run supply for their series, "
                         "then publish the report again.")
    stamp = {"export_id": export_id, "generated_utc": manifest["generated_utc"]}
    history = _with_history(store, "passport_status_history", export_id, [{**stamp, **row} for row in rows])
    lot_history = _with_history(store, "lot_claim_status_history", export_id,
                                [{**stamp, **row} for row in lot_claims])
    newest = max(history, key=lambda r: (r["generated_utc"], r["export_id"]))
    newest_stamp = {"export_id": newest["export_id"], "generated_utc": newest["generated_utc"]}
    current = sorted((r for r in history if r["export_id"] == newest["export_id"]), key=lambda r: r["battery_id"])
    current_lots = sorted((r for r in lot_history if r["export_id"] == newest["export_id"]),
                          key=lambda r: r["lot_id"])
    exported = {row["battery_id"] for row in current}
    missing = sorted(set(packaged) - exported)
    tables = measure_tables([{c: r[c] for c in STATUS_COLUMNS} for r in current],
                            [{c: r[c] for c in LOT_CLAIM_COLUMNS} for r in current_lots], newest_stamp, len(missing))
    store.replace_rows("reporting", "passport_status_current", current)
    store.replace_rows("reporting", "lot_claim_status_current", current_lots)
    for table, table_rows in tables.items():
        store.replace_rows("reporting", table, table_rows)
    return {"export_id": export_id, "current_export_id": newest["export_id"], "rows": len(rows),
            "lot_claims": len(lot_claims), "current": len(current), "not_in_export": missing[:20],
            "not_in_export_count": len(missing), **tables}
