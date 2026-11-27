"""The party registry: which fictional partner may sign which messages. Public keys only, never private material.

The deployment writes the registry (schema dbp-party-registry-1) into the DBP_PARTY_REGISTRY_B64 app setting. The app
copies it into the updatable ledger table Parties, so a later change such as a revoked key keeps its history.
"""

import base64
import binascii
import re

from . import domain

SCHEMA = "dbp-party-registry-1"
AUDIENCE = "CONTOSO-BATTERY"
ALGORITHM = "Ed25519"
ROLES = ("supplier", "recycler", "carrier", "plant", "oem", "second_life_operator")
EVENT_TYPES = ("record_soh", "record_ownership_change", "request_repurpose", "record_end_of_life",
               "record_recycling_closure")
PARTY_ID = re.compile(r"^[A-Z0-9][A-Z0-9-]{1,49}$")
LOT_ID = re.compile(r"^[A-Z0-9-]{1,50}$")
TAG = re.compile(r"^(?P<base>.+)-[A-Z0-9]{1,6}$")
FIELDS = ("party_id", "name", "role", "key_id", "public_key", "lot_ids", "event_types", "revoked")


def check_registry(document):
    """The registry's party entries, normalized. Raises ValueError for anything this app would not trust."""
    if not document:
        return []
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        raise ValueError(f"DBP_PARTY_REGISTRY_B64 must hold a {SCHEMA} document.")
    if document.get("audience") != AUDIENCE:
        raise ValueError(f"The party registry must name the audience {AUDIENCE}.")
    entries, seen = [], set()
    for item in document.get("parties") or []:
        if not isinstance(item, dict):
            raise ValueError("Every registry party must be an object.")
        party_id = item.get("party_id")
        if not isinstance(party_id, str) or not PARTY_ID.fullmatch(party_id) or party_id in seen:
            raise ValueError(f"Registry party {party_id!r} is missing, malformed or listed twice.")
        seen.add(party_id)
        if item.get("role") not in ROLES or item.get("alg") != ALGORITHM:
            raise ValueError(f"{party_id}: unknown role or a key algorithm other than {ALGORITHM}.")
        if any(k in item for k in ("private_key", "d", "seed")):
            raise ValueError(f"{party_id}: the registry must not carry private key material.")
        try:
            raw = base64.b64decode(item.get("public_key") or "", validate=True)
        except (binascii.Error, ValueError, TypeError) as error:
            raise ValueError(f"{party_id}: the public key is not Base64.") from error
        if len(raw) != 32:
            raise ValueError(f"{party_id}: an Ed25519 public key has 32 bytes.")
        lots, events = item.get("lot_ids") or [], item.get("event_types") or []
        if not all(isinstance(lot, str) and LOT_ID.fullmatch(lot) for lot in lots) or \
                not all(event in EVENT_TYPES for event in events):
            raise ValueError(f"{party_id}: malformed lot IDs or unknown event types.")
        entries.append({"party_id": party_id, "name": str(item.get("name") or party_id)[:200],
                        "role": item["role"], "key_id": str(item.get("key_id") or f"{party_id}-KEY-1")[:100],
                        "public_key": item["public_key"], "lot_ids": sorted(set(lots)),
                        "event_types": sorted(set(events)), "revoked": item.get("revoked") is True})
    return entries


def registry_sha256(document):
    return domain.sha256(domain.canonical(document or {}))


def party_row(entry, registry_hash, now):
    return {"party_id": entry["party_id"], "name": entry["name"], "role": entry["role"], "key_id": entry["key_id"],
            "public_key": entry["public_key"], "lot_ids": ",".join(entry["lot_ids"]),
            "event_types": ",".join(entry["event_types"]), "revoked": 1 if entry["revoked"] else 0,
            "registry_sha256": registry_hash, "updated_utc": now}


def entry_of(row):
    """A Parties row as a registry entry."""
    return {"party_id": row["party_id"], "name": row["name"], "role": row["role"], "key_id": row["key_id"],
            "public_key": row["public_key"], "lot_ids": domain.split_ids(row["lot_ids"]),
            "event_types": domain.split_ids(row["event_types"]), "revoked": bool(row["revoked"])}


def lot_allowed(entry, lot_id):
    """A party may claim its registered lots, also in a tagged copy of the series (LOT-LI-A-T1 for LOT-LI-A)."""
    if lot_id in entry["lot_ids"]:
        return True
    match = TAG.fullmatch(lot_id or "")
    return bool(match) and match.group("base") in entry["lot_ids"]


def public_view(row):
    """A Parties row for GET /parties: public keys and permissions; the app never holds private keys."""
    return {**entry_of(row), "updated_utc": row["updated_utc"], "row_version": row["row_version"]}
