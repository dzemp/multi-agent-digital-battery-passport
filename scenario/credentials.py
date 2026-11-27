"""The credential_negatives scenario: signed partner messages that the app's mock connector must refuse.

Each check sends one synthetic message that is tampered, signed by the wrong party, misaddressed, expired, from a
revoked or unknown party, a conflicting reuse of a message ID, or outside what the issuer is registered for, and
compares the connector's answer with the expected refusal. The last check resends a genuine claim unchanged and
expects the stored result back. Rejected messages are logged by the connector but never claim their message ID.

Locally (python -m dbp_mvp scenario credential_negatives) everything runs against an in-memory app with keys
generated at runtime and a synthetic revoked test party added to its registry. With --apply the same messages go to
the deployed app, whose registry has no revoked party, so that check is reported as not exercised.
"""

import json
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from data import signed_messages, simulator

from .acts import BANNER, FAIL, NoAnswer, PASS, Register, body_of, git_revision, utc_now
from .series import Series

ACT = "credential_negatives"
NOT_EXERCISED = "not exercised"
REVOKED_PARTY, REVOKED_LOT = "SUP-REVOKED-TEST", "LOT-REVOKED-TEST"
UNKNOWN_PARTY = "SUP-UNKNOWN-TEST"
REPLAY_CHECK = "exact replay of a genuine claim returns the stored result"


def revoked_entry(key):
    """A clearly labelled synthetic test party whose key is revoked; it exists only in the local registry."""
    return {"party_id": REVOKED_PARTY, "name": "Synthetic revoked test party", "role": "supplier",
            "key_id": signed_messages.key_id(REVOKED_PARTY), "alg": signed_messages.ALGORITHM,
            "public_key": signed_messages.public_key_b64(key), "lot_ids": [REVOKED_LOT], "event_types": [],
            "revoked": True}


def checks(series, keys, revoked_key=None):
    """[(check, expected answer, envelope or None)]; None means the check cannot be exercised here."""
    lot, cobalt, battery = series.lots[0], series.case_lot, series.batteries[2]
    genuine = series.message(simulator.claim_message_id(lot))
    claim, supplier = genuine["payload"], genuine["payload"]["issuer"]
    other = next(party for party in sorted(keys) if party != supplier)

    def message(name, subject=lot, **changes):
        return {**claim, "message_id": f"MSG-CRED-{name}-{subject}", **changes}

    tampered = {**genuine, "payload": message("TAMPERED", mass_kg=claim["mass_kg"] + 1)}
    wrong_key = signed_messages.sign(message("KEY"), keys[other])
    wrong_key["signature"]["key_id"] = signed_messages.key_id(other)
    foreign_lot = series.message(simulator.claim_message_id(cobalt))["payload"]
    other_lot = {**foreign_lot, "issuer": supplier, "supplier_id": supplier,
                 "message_id": f"MSG-CRED-LOT-{cobalt}"}
    event = {**signed_messages.event_payload(supplier, "record_soh", battery, claim["issued_utc"], soh_pct=80,
                                             measured_utc=claim["issued_utc"]),
             "message_id": f"MSG-CRED-EVENT-{battery}"}
    conflict = {**claim, "mass_kg": claim["mass_kg"] + 1}
    unknown_key = Ed25519PrivateKey.generate()
    revoked = None
    if revoked_key is not None:
        revoked = signed_messages.sign(message("REVOKED", REVOKED_LOT, issuer=REVOKED_PARTY,
                                               supplier_id=REVOKED_PARTY, lot_id=REVOKED_LOT,
                                               claim_id=f"CLAIM-{REVOKED_LOT}"), revoked_key)
    return [("claim altered after signing (tampered copy)", "403 bad_signature", tampered),
            ("claim signed with another party's key", "403 issuer_mismatch", wrong_key),
            ("claim addressed to another receiver", "403 wrong_audience",
             signed_messages.sign(message("AUDIENCE", audience="OTHER-RECEIVER"), keys[supplier])),
            ("claim past its expiry", "422 expired",
             signed_messages.sign(message("EXPIRED", issued_utc="2020-01-01T00:00:00Z",
                                          expires_utc="2021-01-01T00:00:00Z"), keys[supplier])),
            ("claim from a party whose key is revoked", "403 revoked", revoked),
            ("known message ID with a different, validly signed payload", "409 message_conflict",
             signed_messages.sign(conflict, keys[supplier])),
            ("supplier claims a lot it is not registered for", "403 lot_not_allowed",
             signed_messages.sign(other_lot, keys[supplier])),
            ("supplier signs a lifecycle event", "403 event_type_not_allowed",
             signed_messages.sign(event, keys[supplier])),
            ("claim from a party not in the registry", "403 unknown_key",
             signed_messages.sign(message("UNKNOWN", issuer=UNKNOWN_PARTY, supplier_id=UNKNOWN_PARTY),
                                  unknown_key)),
            (REPLAY_CHECK, "201 verified (replayed)", genuine)]


def answer(http, envelope):
    """The connector's answer as '<HTTP status> <result or reason code>', with ' (replayed)' for a stored result."""
    try:
        response = http.request("POST", "/connector/v1/messages", json={"envelope": envelope})
    except NoAnswer as error:
        return f"no answer ({error})"
    body = body_of(response)
    result = body.get("result")
    code = result if result in ("routed", "verified") else body.get("reason_code") or body.get("error") or result
    return f"{response.status_code} {code}{' (replayed)' if body.get('replayed') else ''}"


def run(http, series, keys, mode, folder, revoked_key=None, out=print):
    """Send every check through the connector, print expected and observed answers and write the report. Returns
    the report; its verdict is FAIL when any exercised check did not get the expected answer."""
    rows = []
    for name, expected, envelope in checks(series, keys, revoked_key):
        if envelope is None:
            observed, result = "the deployed party registry has no revoked party", NOT_EXERCISED
        else:
            observed = answer(http, envelope)
            result = PASS if observed == expected else FAIL
        rows.append({"check": name, "expected": expected, "observed": observed, "result": result})
        out(f"  {result:<13} {name}: expected {expected}, observed {observed}")
    verdict = FAIL if any(row["result"] == FAIL for row in rows) else PASS
    passed = sum(row["result"] == PASS for row in rows)
    report = {"banner": BANNER, "scenario": ACT, "date_utc": utc_now(), **git_revision(),
              "series": series.name, "mode": mode,
              "evidence_level": "local in-memory app" if mode == "local" else "tenant observed",
              "connector": "mock", "checks": rows, "passed": passed,
              "not_exercised": sum(row["result"] == NOT_EXERCISED for row in rows), "verdict": verdict}
    write(report, Path(folder))
    return report


def write(report, folder):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    lines = [f"# Credential negatives ({report['verdict']})", "", f"> {report['banner']}", "",
             f"- Date (UTC): {report['date_utc']}",
             f"- Commit: `{report['commit']}`{' (uncommitted changes present)' if report['dirty'] else ''}",
             f"- Series: {report['series']}", f"- Mode: {report['mode']} (connector: mock)",
             f"- Evidence level: **{report['evidence_level']}**", "",
             "| Check | Expected | Observed | Result |", "|---|---|---|---|"]
    lines += [f"| {r['check']} | {r['expected']} | {r['observed']} | {r['result']} |" for r in report["checks"]]
    (folder / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_local(folder, out=print):
    """The checks against an in-memory app seeded with the main series; keys are generated in a temporary folder."""
    from .scripted import LocalApp, ScriptedDouble

    with tempfile.TemporaryDirectory(prefix="dbp-credentials-", ignore_cleanup_errors=True) as tmp:
        series = Series(output=Path(tmp) / "output")
        app = LocalApp(tmp, ScriptedDouble())
        try:
            app.seed(app.runner(series, Register(series, ACT, "local", out_dir=Path(tmp) / "runs"),
                                out=lambda *_: None))
            revoked_key = Ed25519PrivateKey.generate()
            registry = series.registry()
            registry["parties"].append(revoked_entry(revoked_key))
            app.client.app.state.connector.sync(registry)
            return run(app.staff, series, simulator.signing_keys(series.output), "local", folder, revoked_key, out)
        finally:
            app.close()
