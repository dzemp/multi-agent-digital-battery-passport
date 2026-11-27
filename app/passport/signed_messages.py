"""Verify Ed25519-signed partner messages: supplier claims (dbp-supplier-claim-2) and partner events
(dbp-partner-event-1).

An envelope is {payload, signature{alg, key_id, value}}; the signature covers the canonical payload (sorted keys,
compact separators, UTF-8). Trust comes from the issuer's registered public key, never from whoever sent the
request. Every refusal carries a coded reason. Replay (a reused message ID) is checked by the connector, which holds
the message log.
"""

import base64
import binascii
import math
import re
from datetime import datetime, timedelta

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import domain, parties

CLAIM_SCHEMA = "dbp-supplier-claim-2"
EVENT_SCHEMA = "dbp-partner-event-1"
CLAIM_TYPE = "supplier_claim"
MESSAGE_TYPES = (CLAIM_TYPE, *parties.EVENT_TYPES)
COMMON = {"schema", "issuer", "audience", "message_id", "issued_utc", "expires_utc", "data_label"}
CLAIM_FIELDS = COMMON | {"claim_id", "claim_version", "lot_id", "supplier_id", "material", "mass_kg", "origin_site",
                         "region", "pcf_kgco2e_per_kg", "recycled_share_pct", "na_reasons"}
MESSAGE_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
SUBJECT = re.compile(r"^[A-Z0-9-]{1,50}$")
CLOCK_SKEW = timedelta(minutes=5)
MAX_PAYLOAD_BYTES = 8000
# reason code -> HTTP status of the refusal
REASONS = {"bad_envelope": 422, "bad_payload": 422, "too_large": 413, "unknown_key": 403, "issuer_mismatch": 403,
           "bad_signature": 403, "wrong_audience": 403, "expired": 422, "not_yet_valid": 422, "revoked": 403,
           "lot_not_allowed": 403, "event_type_not_allowed": 403, "no_agreement": 403, "unknown_battery": 404,
           "message_conflict": 409}


class Rejected(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message, self.status = code, message, REASONS[code]


def parse_utc(value):
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError) as error:
        raise Rejected("bad_payload", "Times must be ISO-8601 UTC timestamps.") from error
    if moment.utcoffset() is None:
        raise Rejected("bad_payload", "Times must include a timezone.")
    return moment


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def message_type(payload):
    return CLAIM_TYPE if payload.get("schema") == CLAIM_SCHEMA else payload.get("event_type")


def subject(payload):
    return payload.get("lot_id") if payload.get("schema") == CLAIM_SCHEMA else payload.get("battery_id")


def check_payload(payload):
    """The payload's shape for its schema; raises Rejected("bad_payload") with what is wrong."""
    if not isinstance(payload, dict) or not COMMON <= set(payload):
        raise Rejected("bad_payload", "The payload lacks the common message fields.")
    if payload["data_label"] != "synthetic" or not isinstance(payload["issuer"], str) or \
            not isinstance(payload["message_id"], str) or not MESSAGE_ID.fullmatch(payload["message_id"]):
        raise Rejected("bad_payload", "Only synthetic messages with an 8-64 character message ID are accepted.")
    if len(domain.canonical(payload)) > MAX_PAYLOAD_BYTES:
        raise Rejected("too_large", f"A payload may have at most {MAX_PAYLOAD_BYTES} canonical bytes.")
    if not domain.canonical(payload).isascii():
        raise Rejected("bad_payload", "The payload must be ASCII text (the message log stores it as varchar).")
    for key in ("issued_utc", "expires_utc"):
        parse_utc(payload[key])
    if payload["schema"] == CLAIM_SCHEMA:
        if set(payload) != CLAIM_FIELDS:
            raise Rejected("bad_payload", f"The claim does not match {CLAIM_SCHEMA}.")
        if not isinstance(payload["lot_id"], str) or not SUBJECT.fullmatch(payload["lot_id"]) or \
                payload["claim_id"] != f"CLAIM-{payload['lot_id']}" or type(payload["claim_version"]) is not int or \
                payload["claim_version"] < 1 or not isinstance(payload["material"], str) or \
                not all(number(payload[k]) for k in ("mass_kg", "pcf_kgco2e_per_kg", "recycled_share_pct")) or \
                not (payload["origin_site"] is None or isinstance(payload["origin_site"], str)) or \
                not isinstance(payload["na_reasons"], dict):
            raise Rejected("bad_payload", "A claim field is missing or has an invalid value.")
    elif payload["schema"] == EVENT_SCHEMA:
        if payload.get("event_type") not in parties.EVENT_TYPES or not isinstance(payload.get("battery_id"), str) or \
                not SUBJECT.fullmatch(payload["battery_id"]):
            raise Rejected("bad_payload", "The event needs a known event type and a battery ID.")
        if payload["event_type"] == "record_soh" and (
                not number(payload.get("soh_pct")) or not 0 <= payload["soh_pct"] <= 100):
            raise Rejected("bad_payload", "A state-of-health event needs soh_pct from 0 to 100.")
        if payload["event_type"] == "record_soh":
            parse_utc(payload.get("measured_utc"))
    else:
        raise Rejected("bad_payload", f"Only {CLAIM_SCHEMA} and {EVENT_SCHEMA} messages are accepted.")
    return payload


def verify(envelope, registry, now):
    """(payload, party entry) for a message whose signature, issuer, audience, validity, key status and permissions
    hold. registry: party entries as parties.check_registry returns them; now: an aware datetime."""
    if not isinstance(envelope, dict) or set(envelope) != {"payload", "signature"}:
        raise Rejected("bad_envelope", "Send exactly a payload and a signature.")
    payload, signature = check_payload(envelope["payload"]), envelope["signature"]
    if not isinstance(signature, dict) or set(signature) != {"alg", "key_id", "value"} or \
            signature["alg"] != parties.ALGORITHM or not isinstance(signature["key_id"], str):
        raise Rejected("bad_envelope", "The signature must be Ed25519 with a key ID and a value.")
    entry = next((e for e in registry if e["key_id"] == signature["key_id"]), None)
    if entry is None:
        raise Rejected("unknown_key", "The signing key is not in the party registry.")
    if payload["issuer"] != entry["party_id"]:
        raise Rejected("issuer_mismatch", "The issuer does not own the signing key.")
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(entry["public_key"])).verify(
            base64.b64decode(signature["value"], validate=True), domain.canonical(payload))
    except (InvalidSignature, ValueError, TypeError, binascii.Error) as error:
        raise Rejected("bad_signature", "The signature does not match the payload.") from error
    if payload["audience"] != parties.AUDIENCE:
        raise Rejected("wrong_audience", f"The message is not addressed to {parties.AUDIENCE}.")
    if now > parse_utc(payload["expires_utc"]):
        raise Rejected("expired", "The message has expired.")
    if parse_utc(payload["issued_utc"]) > now + CLOCK_SKEW:
        raise Rejected("not_yet_valid", "The message is issued in the future.")
    if entry["revoked"]:
        raise Rejected("revoked", "The issuer's key is revoked in the party registry.")
    if payload["schema"] == CLAIM_SCHEMA:
        if payload["supplier_id"] != entry["party_id"]:
            raise Rejected("issuer_mismatch", "A party may only sign claims for its own lots.")
        if not parties.lot_allowed(entry, payload["lot_id"]):
            raise Rejected("lot_not_allowed", "The issuer is not registered for this lot.")
    elif payload["event_type"] not in entry["event_types"]:
        raise Rejected("event_type_not_allowed", "The issuer is not registered for this event type.")
    return payload, entry
