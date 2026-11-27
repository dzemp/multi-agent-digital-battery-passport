"""Party deliveries: a folder of record files plus delivery.json, which is written last.

Standard library only; the Fabric notebooks embed this text after rules.py, so it may only import from that module.
"""

import csv
import io
import json
import re

from .rules import CONTRACTS, OPTIONAL_FIELDS, canonical, decode_document, digest, utc

DELIVERY_MANIFEST = "delivery.json"
DELIVERY_KEYS = {"delivery_id", "party_id", "kind", "format", "unit", "files", "created_utc"}
DELIVERY_FILE_KEYS = {"name", "sha256", "records"}
DELIVERY_ITEM = re.compile(r"([A-Z0-9-]{1,40})/(DLV-[A-Z0-9-]{1,90})")
FILE_NAME = re.compile(r"[a-z0-9_-]{1,60}\.(json|csv)")
KIND_ENTITIES = {"lots": ("lot",), "transport": ("transport",), "batches": ("batch",), "builds": ("series", "battery"),
                 "consumption": ("consumption",), "energy": ("batch_energy",)}


class DeliveryRejected(ValueError):
    def __init__(self, code, detail=""):
        super().__init__(f"{code}:{detail}" if detail else code)
        self.reason = f"{code}:{detail}" if detail else code


def encode_document(name, records):
    """JSON arrays use the canonical form. CSV is for flat records: entity, key, version, then the fields; an absent
    field is an empty cell and not-applicable reasons are 'field=reason;field=reason' text."""
    if not name.endswith(".csv"):
        return (canonical(records) + "\n").encode("utf-8")
    entities = {record["entity"] for record in records}
    if len(entities) != 1:
        raise ValueError(f"{name}: a CSV file holds one entity")
    entity = entities.pop()
    columns = ["entity", "key", "version"] + sorted(CONTRACTS[entity] | set(OPTIONAL_FIELDS.get(entity, ())))
    columns += ["na_reasons"] if entity in OPTIONAL_FIELDS else []
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for record in records:
        row = {column: record.get(column, "") for column in columns}
        if isinstance(row.get("na_reasons"), dict):
            row["na_reasons"] = ";".join(f"{field}={reason}" for field, reason in sorted(row["na_reasons"].items()))
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8")


def make_delivery(delivery_id, party_id, profile, kind, documents, created_utc):
    """{file name: bytes} in write order: the data files, then delivery.json."""
    files, entries = {}, []
    for name, records in sorted(documents.items()):
        files[name] = encode_document(name, records)
        entries.append({"name": name, "sha256": digest(files[name]), "records": len(records)})
    manifest = {"delivery_id": delivery_id, "party_id": party_id, "kind": kind, "format": profile["format"],
                "unit": profile["unit"], "files": entries, "created_utc": created_utc}
    files[DELIVERY_MANIFEST] = (canonical(manifest) + "\n").encode("utf-8")
    return files


def parse_delivery_set(text):
    """'PARTY/DLV-...,PARTY/DLV-...' -> [(party_id, delivery_id)] in order, without repeats."""
    items = [item.strip() for item in (text or "").split(",") if item.strip()]
    if not items:
        raise ValueError("The delivery set is empty.")
    result = []
    for item in items:
        match = DELIVERY_ITEM.fullmatch(item)
        if not match:
            raise ValueError(f"Invalid delivery set item: {item!r}")
        if match.groups() in result or any(match.group(2) == known[1] for known in result):
            raise ValueError(f"Delivery listed twice: {item}")
        result.append(match.groups())
    return result


def _manifest(files, party_id, delivery_id):
    if DELIVERY_MANIFEST not in files:
        raise DeliveryRejected("missing_manifest")
    try:
        manifest = json.loads(files[DELIVERY_MANIFEST].decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise DeliveryRejected("bad_manifest", type(error).__name__) from error
    if not isinstance(manifest, dict) or set(manifest) != DELIVERY_KEYS:
        raise DeliveryRejected("bad_manifest", "keys")
    entries = manifest["files"]
    if (not isinstance(entries, list) or not entries
            or not all(isinstance(entry, dict) and set(entry) == DELIVERY_FILE_KEYS for entry in entries)):
        raise DeliveryRejected("bad_manifest", "files")
    names = [entry["name"] for entry in entries]
    if len(names) != len(set(names)) or not all(isinstance(n, str) and FILE_NAME.fullmatch(n) for n in names):
        raise DeliveryRejected("bad_manifest", "file names")
    if manifest["delivery_id"] != delivery_id:
        raise DeliveryRejected("bad_manifest", "delivery_id")
    try:
        utc(manifest["created_utc"])
    except ValueError as error:
        raise DeliveryRejected("bad_manifest", "created_utc") from error
    if manifest["party_id"] != party_id:
        raise DeliveryRejected("party_mismatch", str(manifest["party_id"])[:40])
    return manifest


def _check(party_id, delivery_id, files, registry):
    manifest = _manifest(files, party_id, delivery_id)
    profile = registry.get(party_id)
    if profile is None:
        raise DeliveryRejected("unknown_party", party_id)
    kind = manifest["kind"]
    if kind not in KIND_ENTITIES or kind not in profile["kinds"]:
        raise DeliveryRejected("kind_not_allowed", str(kind)[:40])
    if manifest["format"] != profile["format"] or manifest["unit"] != profile["unit"]:
        raise DeliveryRejected("profile_mismatch", f"{manifest['format']}/{manifest['unit']}"[:60])
    entries = {entry["name"]: entry for entry in manifest["files"]}
    for name in entries:
        if not name.endswith("." + profile["format"]):
            raise DeliveryRejected("profile_mismatch", name)
    present = set(files) - {DELIVERY_MANIFEST}
    missing, unlisted = sorted(set(entries) - present), sorted(present - set(entries))
    if missing:
        raise DeliveryRejected("missing_file", missing[0])
    if unlisted:
        raise DeliveryRejected("unlisted_file", unlisted[0][:60])
    records = []
    for name, entry in entries.items():
        if digest(files[name]) != entry["sha256"]:
            raise DeliveryRejected("hash_mismatch", name)
        try:
            decoded = decode_document(name, files[name].decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise DeliveryRejected("parse_failure", f"{name}:{str(error)[:80]}") from error
        if len(decoded) != entry["records"]:
            raise DeliveryRejected("count_mismatch", name)
        first_line = 2 if name.endswith(".csv") else 1
        for index, record in enumerate(decoded):
            if record["entity"] not in KIND_ENTITIES[kind]:
                raise DeliveryRejected("entity_not_allowed", f"{name}:{index + first_line}")
            if record.get("party") != party_id:
                raise DeliveryRejected("record_party_mismatch", f"{name}:{index + first_line}")
            records.append((name, index + first_line, record))
    return manifest, records


def check_delivery(party_id, delivery_id, files, registry):
    """Accept or reject one whole delivery. Records come back as (file, line number, record)."""
    manifest_sha256 = digest(files[DELIVERY_MANIFEST]) if DELIVERY_MANIFEST in files else None
    try:
        manifest, records = _check(party_id, delivery_id, files, registry)
    except DeliveryRejected as rejected:
        return {"status": "rejected", "reason": rejected.reason, "manifest": None,
                "manifest_sha256": manifest_sha256, "records": []}
    return {"status": "accepted", "reason": None, "manifest": manifest, "manifest_sha256": manifest_sha256,
            "records": records}
