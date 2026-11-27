"""Import a complete Fabric Gold series delivery. No production or lifecycle row is modified."""

import hashlib
import json
import re
from datetime import date, datetime
from decimal import Decimal

from .auth import FLEET_ROLES
from .service import CommandError
from .store import ConflictError, format_utc

ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
DELIVERY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
EFC = "equivalent_full_cycles"


def invalid(message):
    raise CommandError(422, "invalid_aftersales", message)


def conflict(message):
    raise CommandError(409, "aftersales_conflict", message)


def fields(row, names):
    if not isinstance(row, dict) or set(row) != set(names.split()):
        invalid(f"Expected fields: {names}.")


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        invalid("Expected a bounded identifier.")
    return value


def day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        invalid("Expected an ISO calendar date.")
    try:
        return date.fromisoformat(value)
    except ValueError:
        invalid("Invalid calendar date.")


def timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)", value):
        invalid("Expected a dated UTC timestamp.")
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        invalid("Invalid UTC timestamp.")
    if instant.microsecond % 1000:
        invalid("UTC timestamp exceeds millisecond precision.")
    return format_utc(instant)


def number(value, maximum):
    if type(value) not in (int, float) or not 0 <= value <= maximum or Decimal(str(value)).as_tuple().exponent < -6:
        invalid("Reading outside its supported range or precision.")


def rows(records, name, key):
    result = {}
    if not isinstance(records[name], list):
        invalid(f"{name} must be an array.")
    for row in records[name]:
        value = identifier(row.get(key) if isinstance(row, dict) else None)
        if value in result:
            invalid(f"Duplicate {key}.")
        result[value] = row
    return result


def latest_receipt(tx, series):
    return max(tx.select("AftersalesReceipts", series_id=series), key=lambda r: r["as_of_utc"], default=None)


def read_envelope(blob, delivery_id):
    def unique_object(pairs):
        data = dict(pairs)
        if len(data) != len(pairs):
            invalid("Duplicate JSON object key.")
        return data

    try:
        data = json.loads(blob.decode("utf-8"), object_pairs_hook=unique_object,
                          parse_constant=lambda _: invalid("Non-finite JSON number."))
        fields(data, "schema series_id delivery_id as_of_utc records sha256")
        if data["schema"] != "dbp-aftersales-1" or data["delivery_id"] != delivery_id:
            invalid("Invalid schema or delivery ID.")
        fields(data["records"], "packs readings campaigns")
        canonical = json.dumps(data["records"], sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                               allow_nan=False).encode("utf-8")
        if hashlib.sha256(canonical).hexdigest() != data["sha256"]:
            invalid("The records digest does not match the manifest.")
    except (ValueError, RecursionError):
        invalid("Invalid UTF-8 JSON envelope.")
    identifier(data["series_id"])
    data["as_of_utc"] = timestamp(data["as_of_utc"])
    return data


def import_delivery(service, delivery_id, actor):
    try:
        service.require_schema()
        if not DELIVERY_ID.fullmatch(delivery_id):
            invalid("Invalid intake delivery ID.")
        blob = service.blobs.get("intake", f"aftersales/{delivery_id}/aftersales.json")
        if blob is None:
            raise CommandError(404, "aftersales_not_found", "No Gold delivery at this intake path.")
        if len(blob) > service.max_upload_bytes:
            raise CommandError(413, "aftersales_too_large", "Delivery exceeds the configured intake limit.")
        data = read_envelope(blob, delivery_id)
        receipt = {k: data[k] for k in ("delivery_id", "series_id", "as_of_utc", "sha256")}
        with service.store.transaction() as tx:
            prior = tx.get("AftersalesReceipts", delivery_id)
            if prior:
                if any(prior[k] != v for k, v in receipt.items()):
                    conflict("Delivery ID already used for other content.")
                return 200, {**receipt, "replayed": True}
            previous = latest_receipt(tx, data["series_id"])
            if previous and previous["as_of_utc"] >= data["as_of_utc"]:
                conflict("Series delivery does not advance the last accepted as-of time.")
            write_records(tx, data)
            tx.insert("AftersalesReceipts", {**receipt, "imported_utc": service.clock(), "imported_by": actor})
        return 201, {**receipt, "replayed": False}
    except CommandError as exc:
        return exc.status, exc.body
    except ConflictError:
        return 409, {"error": "aftersales_conflict", "message": "An imported key conflicts with a stored row."}


def write_records(tx, data):
    records = data["records"]
    delivery, series, as_of = data["delivery_id"], data["series_id"], data["as_of_utc"]
    packs = rows(records, "packs", "battery_id")
    # A delivery is a complete snapshot, not a patch that implicitly clears terms or campaigns.
    expected = {p["battery_id"] for p in tx.select("PackageBatteries", series_id=series)}
    if not packs or set(packs) != expected:
        invalid("A complete snapshot must identify every original pack in the imported series.")
    for battery, p in packs.items():
        fields(p, "battery_id oem_id batch_id in_service_date term_version warranty_months warranty_cycles")
        if p["oem_id"] not in FLEET_ROLES.values():
            invalid("Unknown OEM.")
        state = tx.get("PassportState", battery)
        produced = tx.get("PackageBatteries", state["build_id"], battery) if state else None
        if (not produced or produced["battery_id"] != battery or produced["series_id"] != series
                or produced["batch_id"] != identifier(p["batch_id"])
                or tx.select("PassportLinks", battery_id=battery, link_type="predecessor")):
            invalid("Pack, production series or original batch does not match the imported production package.")
        pack = {"battery_id": battery, "series_id": series, "oem_id": p["oem_id"], "batch_id": p["batch_id"]}
        saved = tx.get("OEMPhysicalPacks", battery)
        if saved and saved != pack:
            conflict("Physical pack cannot change OEM, batch or series.")
        if not saved:
            tx.insert("OEMPhysicalPacks", pack)
        start = p["in_service_date"]
        if start is not None and day(start).isoformat() > as_of[:10]:
            invalid("In-service date is after this delivery.")
        for key, maximum in (("warranty_months", 1200), ("warranty_cycles", 2147483647)):
            value = p[key]
            if value is not None and (type(value) is not int or not 0 < value <= maximum):
                invalid(f"Invalid {key}.")
        if p["term_version"] is not None or any(p[k] is not None for k in ("warranty_months", "warranty_cycles")):
            identifier(p["term_version"])
        for old in tx.select("WarrantyTerms", battery_id=battery):
            if old["in_service_date"] is not None and old["in_service_date"] != start:
                conflict("In-service date conflicts with a prior delivery.")
            if p["term_version"] and old["term_version"] == p["term_version"] and any(
                    p[k] != old[k] for k in ("warranty_months", "warranty_cycles")):
                conflict("Warranty limits changed without a new term version.")
        if start and p["warranty_months"]:
            start_day = day(start)
            if start_day.year * 12 + start_day.month - 1 + p["warranty_months"] >= 120000:
                invalid("Warranty end date exceeds the calendar range.")
        tx.insert("WarrantyTerms", {"delivery_id": delivery, **{k: p[k] for k in (
            "battery_id", "in_service_date", "term_version", "warranty_months", "warranty_cycles")}})

    readings = rows(records, "readings", "reading_id")
    for reading in readings.values():
        fields(reading, "reading_id battery_id measured_utc actual_cycles cycle_basis soh_pct")
        if identifier(reading["battery_id"]) not in packs or reading["cycle_basis"] != EFC:
            invalid("Unknown reading pack or unsupported cycle basis.")
        number(reading["actual_cycles"], 999999999999)
        if reading["soh_pct"] is not None:
            number(reading["soh_pct"], 100)
        row = {**reading, "measured_utc": timestamp(reading["measured_utc"])}
        if row["measured_utc"] > as_of:
            invalid("Reading is later than the feed as-of time.")
        prior = tx.get("ServiceReadings", row["reading_id"])
        if prior:
            if any(prior[k] != v for k, v in row.items()):
                conflict("A reading ID cannot change its observation.")
        else:
            tx.insert("ServiceReadings", {**row, "delivery_id": delivery})
    for battery in {row["battery_id"] for row in readings.values()}:
        history = sorted(tx.select("ServiceReadings", battery_id=battery), key=lambda r: r["measured_utc"])
        if any(a["measured_utc"] == b["measured_utc"] or a["actual_cycles"] > b["actual_cycles"]
               for a, b in zip(history, history[1:], strict=False)):
            conflict("Duplicate measurement time or decreasing cumulative EFC.")

    batches = {p["batch_id"] for p in packs.values()}
    for c in rows(records, "campaigns", "campaign_id").values():
        fields(c, "campaign_id kind status effective_date batch_ids battery_ids")
        if c["kind"] not in ("recall", "service_campaign") or c["status"] not in ("open", "closed"):
            invalid("Invalid campaign kind or status.")
        day(c["effective_date"])
        for key, known in (("battery_ids", packs), ("batch_ids", batches)):
            if not isinstance(c[key], list) or any(identifier(v) not in known for v in c[key]):
                invalid("Campaign target not present in this delivery.")
        matched = set(c["battery_ids"]) | {b for b, p in packs.items() if p["batch_id"] in c["batch_ids"]}
        if not matched:
            invalid("Campaign has no targets.")
        tx.insert("ServiceCampaigns", {"delivery_id": delivery, **{k: c[k] for k in (
            "campaign_id", "kind", "status", "effective_date")}})
        for battery in sorted(matched):
            tx.insert("CampaignPacks",
                      {"delivery_id": delivery, "campaign_id": c["campaign_id"], "battery_id": battery})
