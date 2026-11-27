"""Shared rules for the synthetic aftersales_flow. This module is also embedded in its Fabric notebooks.

Only the gold envelope goes to the app. The sidecar manifest, original receipts, row lineage and quarantine
stay in the lakehouses. A delivery denotes an immutable snapshot, not a production rebuild.
"""

import hashlib
import json
import re
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation


AFTER_KEYS = {"packs": "battery_id", "readings": "reading_id", "campaigns": "campaign_id"}
AFTER_FIELDS = {"packs": "battery_id batch_id oem_id in_service_date term_version warranty_months warranty_cycles",
                "readings": "reading_id battery_id measured_utc actual_cycles cycle_basis soh_pct",
                "campaigns": "campaign_id kind status effective_date batch_ids battery_ids"}
AFTER_OWNERS = {"packs": "synthetic-oem-service", "readings": "synthetic-bms", "campaigns": "synthetic-oem-quality"}
AFTER_STEPS = {"aftersales_bronze": "ingest", "aftersales_silver": "silver", "aftersales_gold": "gold"}
AFTER_ID = re.compile(r"AS-[A-Z0-9]{1,6}-[0-9A-F]{12}-[0-9A-F]{12}")
AFTER_RULES_VERSION = "aftersales-rules-1"


def after_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def after_hash(data):
    return hashlib.sha256(data).hexdigest()


def after_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value.strip()):
        raise ValueError("invalid identifier")
    return value.strip().upper()


def after_time(value):
    pattern = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,3})?(?:Z|[+-]\d\d:\d\d)"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError("expected a dated timestamp with time zone and at most millisecond precision")
    instant = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    return instant.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def after_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d\d-\d\d", value):
        raise ValueError("expected an ISO date")
    return date.fromisoformat(value).isoformat()


def after_int(value, minimum):
    if isinstance(value, str) and re.fullmatch(r"\d{1,9}", value.strip()):
        value = int(value)
    if type(value) is not int or not minimum <= value <= 2_147_483_647:
        raise ValueError("invalid whole number")
    return value


def _after_number(value, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("expected an observed number")
    try:
        n = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid observed number") from exc
    if not n.is_finite() or not 0 <= n <= maximum or n.as_tuple().exponent < -6:
        raise ValueError("observation outside supported range or precision")
    return int(n) if n == n.to_integral_value() else float(n)


def after_delivery(source):
    build = source["build_id"]
    match = re.fullmatch(r"BUILD-([A-Z0-9]{1,6})-([0-9A-F]{12})", build)
    if not match or source["series_id"] != "SERIES-MAIN" + ("" if match[1] == "MAIN" else "-" + match[1]):
        raise ValueError("production build and series disagree")
    suffix = after_hash(after_bytes([AFTER_RULES_VERSION, source]))[:12].upper()
    return f"AS-{match[1]}-{match[2]}-{suffix}"


def after_folder(delivery):
    if not AFTER_ID.fullmatch(delivery):
        raise ValueError("invalid aftersales delivery ID")
    return f"aftersales/{delivery}"


def _after_row(entity, row, catalog, received):
    if not isinstance(row, dict) or set(row) != set(AFTER_FIELDS[entity].split()):
        raise ValueError("columns differ from the source contract")
    r = dict(row)
    for key in r:
        if key.endswith("_id"):
            r[key] = after_id(r[key])
    if entity in ("packs", "readings") and r["battery_id"] not in catalog:
        raise ValueError("unknown production battery")
    if entity == "packs":
        if r["batch_id"] != catalog[r["battery_id"]]:
            raise ValueError("batch does not match production")
        if r["oem_id"] not in ("OEM-ADVENTUREWORKS", "OEM-FABRIKAM"):
            raise ValueError("unknown fleet organization")
        if r["in_service_date"] is not None:
            r["in_service_date"] = after_date(r["in_service_date"])
            if r["in_service_date"] > received[:10]:
                raise ValueError("future in-service date")
        for key in ("warranty_months", "warranty_cycles"):
            if r[key] is not None:
                r[key] = after_int(r[key], 1)
        if (r["warranty_months"] or 0) > 1200:
            raise ValueError("unsupported warranty duration")
        if r["in_service_date"] and r["warranty_months"]:
            start = date.fromisoformat(r["in_service_date"])
            if start.year * 12 + start.month - 1 + r["warranty_months"] >= 120000:
                raise ValueError("warranty end date exceeds calendar range")
        if r["warranty_months"] is None and r["warranty_cycles"] is None:
            if r["term_version"] is not None:
                raise ValueError("term version without limits")
        else:
            r["term_version"] = after_id(r["term_version"])
    elif entity == "readings":
        r["measured_utc"] = after_time(r["measured_utc"])
        if r["measured_utc"] > received:
            raise ValueError("measurement is later than receipt")
        r["actual_cycles"] = _after_number(r["actual_cycles"], 999_999_999_999)
        if str(r["cycle_basis"]).strip().lower() not in ("efc", "equivalent_full_cycles"):
            raise ValueError("unsupported cycle basis")
        r["cycle_basis"] = "equivalent_full_cycles"
        if r["soh_pct"] is not None:
            r["soh_pct"] = _after_number(r["soh_pct"], 100)
    else:
        r["kind"], r["status"] = (str(r[k]).strip().lower() for k in ("kind", "status"))
        if r["kind"] not in ("recall", "service_campaign") or r["status"] not in ("open", "closed"):
            raise ValueError("invalid campaign kind or status")
        r["effective_date"] = after_date(r["effective_date"])
        for key, allowed in (("batch_ids", set(catalog.values())), ("battery_ids", catalog)):
            if not isinstance(r[key], list):
                raise ValueError("campaign target must be a list")
            r[key] = sorted({after_id(v) for v in r[key]})
            if any(v not in allowed for v in r[key]):
                raise ValueError("unknown production campaign target")
        if not r["batch_ids"] and not r["battery_ids"]:
            raise ValueError("campaign has no targets")
    return r


def after_bronze(source, delivery):
    if source.get("schema") != "dbp-aftersales-source-1" or after_delivery(source) != delivery:
        raise ValueError("source does not match expected aftersales delivery")
    as_of = after_time(source["as_of_utc"])
    catalog = source["production_catalog"]
    if not isinstance(catalog, dict) or not catalog or any(
            after_id(k) != k or after_id(v) != v for k, v in catalog.items()):
        raise ValueError("invalid synthetic production catalog")
    if not isinstance(source["deliveries"], list) or not source["deliveries"]:
        raise ValueError("missing source receipts")
    rows = []
    for pos, receipt in enumerate(source["deliveries"], 1):
        received = after_time(receipt["received_utc"])
        if received > as_of or not isinstance(receipt["rows"], list):
            raise ValueError("invalid source receipt timestamp or rows")
        raw = after_bytes(receipt)
        rows.append({"delivery_id": delivery, "receipt": pos,
                     "source_delivery_id": after_id(receipt["source_delivery_id"]),
                     "source_system": receipt["source_system"], "received_utc": received, "entity": receipt["entity"],
                     "receipt_json": raw.decode(), "receipt_sha256": after_hash(raw)})
    header = {k: v for k, v in source.items() if k != "deliveries"}
    return [{"delivery_id": delivery, "header_json": after_bytes(header).decode()}], rows


def after_silver(header, receipts, delivery):
    ordered = sorted(receipts, key=lambda r: r["receipt"])
    source = {**header, "deliveries": [json.loads(r["receipt_json"]) for r in ordered]}
    if after_delivery(source) != delivery:
        raise ValueError("Bronze receipts do not match requested delivery")
    catalog, results, identities, variants = header["production_catalog"], [], defaultdict(list), defaultdict(set)
    for receipt, shipment in zip(ordered, source["deliveries"], strict=True):
        payload = {k: v for k, v in shipment.items() if k != "received_utc"}
        variants[receipt["source_delivery_id"]].add(after_bytes(payload))
    for receipt, shipment in zip(ordered, source["deliveries"], strict=True):
        entity, sid = shipment["entity"], receipt["source_delivery_id"]
        for line, raw in enumerate(shipment["rows"], 1):
            result = {"delivery_id": delivery, "receipt": receipt["receipt"], "line": line, "source_delivery_id": sid,
                      "entity": entity, "source_key": None, "status": "quarantine", "reason": None, "record_json": None}
            results.append(result)
            try:
                if len(variants[sid]) != 1:
                    raise ValueError("conflicting source delivery ID")
                if entity not in AFTER_KEYS or AFTER_OWNERS[entity] != receipt["source_system"]:
                    raise ValueError("unrecognized entity or source system")
                result["source_key"] = after_id(raw[AFTER_KEYS[entity]])
                normalized = _after_row(entity, raw, catalog, receipt["received_utc"])
                result.update(record_json=after_bytes(normalized).decode(), status="candidate")
            except (KeyError, ValueError, TypeError, OverflowError) as error:
                result["reason"] = str(error)
            if result["source_key"] is not None:
                identities[entity, result["source_key"]].append(result)
    for group in identities.values():
        if len({r["record_json"] for r in group}) > 1:
            for r in group:
                r.update(status="quarantine", reason="conflicting records for this identity")
        elif group[0]["status"] == "candidate":
            for index, r in enumerate(group):
                r["status"] = "duplicate" if index else "accepted"
    packs = {r["source_key"] for r in results if r["entity"] == "packs" and r["status"] == "accepted"}
    batches = {catalog[battery] for battery in packs}
    history = defaultdict(list)
    for r in results:
        if r["status"] not in ("accepted", "duplicate"):
            continue
        record = json.loads(r["record_json"])
        if ((r["entity"] == "readings" and record["battery_id"] not in packs)
                or (r["entity"] == "campaigns" and (not set(record["battery_ids"]).issubset(packs)
                                                    or not set(record["batch_ids"]).issubset(batches)))):
            r.update(status="quarantine", reason="no accepted pack for campaign or reading reference")
        elif r["status"] == "accepted" and r["entity"] == "readings":
            history[record["battery_id"]].append(record)
    for observations in history.values():
        ordered = sorted(observations, key=lambda obs: obs["measured_utc"])
        # The count is cumulative. Do not guess which conflicting observation or device clock to trust.
        if any(a["measured_utc"] == b["measured_utc"] or a["actual_cycles"] > b["actual_cycles"]
               for a, b in zip(ordered, ordered[1:], strict=False)):
            for obs in ordered:
                for r in identities["readings", obs["reading_id"]]:
                    r.update(status="quarantine", reason="ambiguous timestamp or decreasing cumulative cycles")
    return results


def after_gold(header, receipts, silver, delivery):
    records = {entity: [] for entity in AFTER_KEYS}
    counts = {"accepted": 0, "duplicate": 0, "quarantine": 0}
    for row in silver:
        counts[row["status"]] += 1
        if row["status"] == "accepted":
            records[row["entity"]].append(json.loads(row["record_json"]))
    for entity, rows in records.items():
        rows.sort(key=lambda r: r[AFTER_KEYS[entity]])
    if not records["packs"]:
        raise ValueError("no accepted production packs for a Gold delivery; inspect Silver quarantine")
    receipt_rows = [{"receipt": r["receipt"], "source_system": r["source_system"],
                     "source_delivery_id": r["source_delivery_id"], "received_utc": r["received_utc"],
                     "rows": len(json.loads(r["receipt_json"])["rows"]), "sha256": r["receipt_sha256"]}
                    for r in sorted(receipts, key=lambda r: r["receipt"])]
    if sum(r["rows"] for r in receipt_rows) != sum(counts.values()):
        raise ValueError("Silver did not account for every Bronze row")
    envelope = {"schema": "dbp-aftersales-1", "series_id": header["series_id"], "delivery_id": delivery,
                "as_of_utc": after_time(header["as_of_utc"]), "records": records,
                "sha256": after_hash(after_bytes(records))}
    file = after_bytes(envelope)
    manifest = {"schema": "dbp-aftersales-manifest-1", "delivery_id": delivery, "series_id": header["series_id"],
                "build_id": header["build_id"], "as_of_utc": envelope["as_of_utc"], "receipts": receipt_rows,
                "row_outcomes": counts, "row_counts": {k: len(v) for k, v in records.items()},
                "files": {"aftersales.json": after_hash(file)}, "rules_version": AFTER_RULES_VERSION,
                "data_label": "synthetic"}
    return {"aftersales.json": file, "manifest.json": after_bytes(manifest)}


def after_expected(source):
    """Independent, in-memory comparison for the bounded synthetic fixture, never an alternative cloud import."""
    delivery = after_delivery(source)
    headers, receipts = after_bronze(source, delivery)
    header = json.loads(headers[0]["header_json"])
    return after_gold(header, receipts, after_silver(header, receipts, delivery), delivery)
