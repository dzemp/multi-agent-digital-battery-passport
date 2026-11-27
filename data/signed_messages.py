"""Ed25519-signed partner messages: supplier and recycler lot claims and partner lifecycle events.

The signature covers the canonical payload (sorted keys, compact separators, UTF-8). Each signing party has one key,
generated at runtime under the git-ignored output folder and reused on later runs; private keys are never written
to tracked files. The pipeline rules never sign or verify: a lot record only carries claim_id and claim_sha256, the
SHA-256 of the canonical claim payload, so a package does not depend on the runtime keys.

cryptography is imported only where keys are used: the laptop's command line (stage A) imports this module through
the simulator but needs no installed packages.
"""

import base64
import hashlib
import json
from pathlib import Path

CLAIM_SCHEMA = "dbp-supplier-claim-2"
EVENT_SCHEMA = "dbp-partner-event-1"
REGISTRY_SCHEMA = "dbp-party-registry-1"
ALGORITHM = "Ed25519"
AUDIENCE = "CONTOSO-BATTERY"
VALID_YEARS = 10
EVENT_TYPES = ("record_soh", "record_ownership_change", "request_repurpose", "record_end_of_life",
               "record_recycling_closure")
COMMON = ("schema", "issuer", "audience", "message_id", "issued_utc", "expires_utc", "data_label")


def canonical_bytes(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def payload_sha256(payload):
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def expires(issued_utc):
    """The same moment VALID_YEARS later: messages stay valid for the whole scenario."""
    return f"{int(issued_utc[:4]) + VALID_YEARS}{issued_utc[4:]}"


def _common(schema, issuer, message_id, issued_utc):
    return {"schema": schema, "issuer": issuer, "audience": AUDIENCE, "message_id": message_id,
            "issued_utc": issued_utc, "expires_utc": expires(issued_utc), "data_label": "synthetic"}


def claim_payload(lot, claim_version, issued_utc):
    """A supplier or recycler claim about one lot: lot_id, supplier_id, material, mass_kg, origin_site (None when
    na_reasons explains why it does not apply), region, pcf_kgco2e_per_kg and recycled_share_pct."""
    lot_id = lot["lot_id"]
    return {**_common(CLAIM_SCHEMA, lot["supplier_id"], f"MSG-CLAIM-{lot_id}-V{claim_version}", issued_utc),
            "claim_id": f"CLAIM-{lot_id}", "claim_version": claim_version, "lot_id": lot_id,
            "supplier_id": lot["supplier_id"], "material": lot["material"], "mass_kg": lot["mass_kg"],
            "origin_site": lot.get("origin_site"), "region": lot["region"],
            "pcf_kgco2e_per_kg": lot["pcf_kgco2e_per_kg"], "recycled_share_pct": lot["recycled_share_pct"],
            "na_reasons": dict(sorted((lot.get("na_reasons") or {}).items()))}


def event_payload(issuer, event_type, battery_id, issued_utc, **details):
    """A partner lifecycle event about one battery."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"Unknown event type: {event_type}")
    message_id = f"MSG-{event_type.upper().replace('_', '-')}-{battery_id}"
    return {**_common(EVENT_SCHEMA, issuer, message_id, issued_utc), "event_type": event_type,
            "battery_id": battery_id, **details}


def key_id(party_id):
    return f"{party_id}-KEY-1"


def party_key(party_id, folder):
    """The party's private key from <folder>/<party>.key (raw 32 bytes), created on first use."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    path = Path(folder) / f"{party_id}.key"
    if path.exists():
        return Ed25519PrivateKey.from_private_bytes(path.read_bytes())
    key = Ed25519PrivateKey.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                       serialization.NoEncryption()))
    return key


def public_key_b64(key):
    from cryptography.hazmat.primitives import serialization

    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def sign(payload, key):
    """The envelope {payload, signature} signed with the issuer's key."""
    value = base64.b64encode(key.sign(canonical_bytes(payload))).decode("ascii")
    return {"payload": payload, "signature": {"alg": ALGORITHM, "key_id": key_id(payload["issuer"]), "value": value}}


def verify(envelope, public_key):
    """True when the envelope's signature matches its payload under the base64 public key."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    signature = envelope.get("signature") or {}
    if signature.get("alg") != ALGORITHM:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key)).verify(
            base64.b64decode(signature["value"]), canonical_bytes(envelope["payload"]))
    except (InvalidSignature, KeyError, ValueError, TypeError):
        return False
    return True


def party_registry(parties, keys, lot_ids):
    """The public registry document of the signing parties: {party: lot IDs} limits what each supplier may claim."""
    entries = []
    for item in parties:
        if item.party_id not in keys:
            continue
        entry = {"party_id": item.party_id, "name": item.name, "role": item.role, "key_id": key_id(item.party_id),
                 "alg": ALGORITHM, "public_key": public_key_b64(keys[item.party_id]),
                 "lot_ids": sorted(lot_ids.get(item.party_id, ())), "event_types": list(item.events),
                 "revoked": False}
        entries.append(entry)
    return {"schema": REGISTRY_SCHEMA, "audience": AUDIENCE, "data_label": "synthetic", "parties": entries}
