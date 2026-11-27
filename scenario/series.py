"""One series of the scenario: its IDs, evidence, signed partner messages and connector agreements, all from the
synthetic data generator.

The main series (no tag) uses plain IDs such as B-001 and LOT-LI-A; a tag gives a separate copy such as B-001-T1.
B-001 and B-002 use lot LOT-LI-A, whose signed supplier claim names Mine North while its transport record names Mine
South. Every other battery uses lot LOT-LI-B, whose documents agree. B-004 also uses cobalt lot LOT-CO-B, about whose
mine an adverse report arrives; the Supplier agent escalates it to a Compliance case. Every lot's signed claim
arrives through the app's mock connector; the Supplier agent checks each critical lot once. Added batteries B-101
onwards are mostly clean, but about a fifth carry a seeded data issue: another mismatched lithium lot, another
adverse report, an overdrawn nickel lot or a batch without an energy record. Everything is fictional.
"""

import hashlib
import json
from dataclasses import dataclass

from data import simulator

from . import ROOT

OUTPUT = ROOT / "output"


@dataclass(frozen=True)
class Evidence:
    kind: str
    lot: str
    name: str
    data: bytes

    @property
    def command_id(self):
        """The same ID as the deployment's evidence upload, so the same document replays wherever it was sent."""
        key = f"{self.name}|{self.kind}|{self.lot}|{hashlib.sha256(self.data).hexdigest()}"
        return "seed-evidence-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


class Series:
    """The IDs of a series of 5 + extra batteries. Raises ValueError for a tag or size the generator refuses.

    About a fifth of the added batteries carry a seeded data issue (simulator.issue_plan), so a larger series
    exercises more reviews, compliance cases and pipeline blocks than the story alone. Such a series is varied."""

    def __init__(self, tag=None, extra=0, output=OUTPUT):
        simulator.check_options(extra, tag)
        self.tag, self.extra, self.output = tag, extra, output
        self.name = simulator.series_name(tag)
        self.series_id = simulator.suffixed("SERIES-MAIN", tag)
        self.batteries = tuple(simulator.suffixed(f"B-{n}", tag) for n in simulator.battery_numbers(extra))
        self.lots = tuple(simulator.critical_lots(tag, extra))
        # B-001/B-002 wait for the lithium sourcing review; B-004 waits for the LOT-CO-B compliance case.
        self.case_lot = simulator.audit_upload(tag)[2]
        self.held, self.compliance_held = self.batteries[:2], self.batteries[3:4]
        self.issues = {simulator.suffixed(f"B-{n}", tag): kind
                       for n, kind in simulator.issue_plan(extra, tag).items()}
        self.varied = bool(self.issues)
        self.issue_lots = {kind: simulator.suffixed(lot, tag)
                           for kind, lot in simulator.issue_lots(extra, tag).items()}

        def with_kinds(kinds):
            return tuple(b for b, kind in self.issues.items() if kind in kinds)

        # The pipeline blocks these before any agent; a human decision releases the next; the last stay held.
        self.blocked = with_kinds(simulator.BLOCKED_KINDS)
        self.released_issue = with_kinds(simulator.RELEASED_KINDS)
        self.open_held = with_kinds(simulator.OPEN_KINDS)
        waiting = set(self.compliance_held) | set(self.blocked) | set(self.released_issue) | set(self.open_held)
        self.clean = tuple(b for b in self.batteries[2:] if b not in waiting)
        self.final_published = tuple(b for b in self.batteries if b not in self.blocked + self.open_held)

        def lots_of(kinds):
            return tuple(lot for kind, lot in self.issue_lots.items() if kind in kinds)

        self.review_lots = (self.lots[0],) + lots_of(("mismatch_corrected", "mismatch_open"))
        self.case_lots = (self.case_lot,) + lots_of(("adverse_audited", "adverse_open"))
        self.corrected = tuple(simulator.suffixed(lot, tag) for lot in simulator.corrected_lots(extra, tag))
        self.audited_lots = tuple(lot for _, _, lot in simulator.audit_uploads(tag, extra))
        self.open_lots = lots_of(simulator.OPEN_KINDS)

    @property
    def subjects(self):
        return set(self.batteries) | set(self.lots)

    def issue_of(self, subject):
        """The seeded issue kind of an added battery or of an issue lot, else None."""
        if subject in self.issues:
            return self.issues[subject]
        return next((kind for kind, lot in self.issue_lots.items() if lot == subject), None)

    def _evidence(self, uploads):
        documents = simulator.evidence_documents(self.tag, self.extra)
        return [Evidence(kind, lot, name, simulator.evidence_bytes(documents[name])) for name, kind, lot in uploads]

    def opening_evidence(self):
        """The transport records of the lithium lots and the adverse reports about cobalt lots, as the deployment
        uploads them (with the audit that already answers an adverse report)."""
        return self._evidence(simulator.evidence_uploads(self.tag, self.extra))

    def audit_evidence(self, lot=None):
        """The synthetic site audit report that Compliance uploads during the review act: LOT-CO-B's, or the one for
        another audited lot."""
        lot = lot or self.case_lot
        return self._evidence([u for u in simulator.audit_uploads(self.tag, self.extra) if u[2] == lot])[0]

    @property
    def folder(self):
        return simulator.series_folder(self.output, self.name)

    def registry(self):
        """The prepared series' party registry (public keys only)."""
        return json.loads((self.folder / "party-registry.json").read_text(encoding="utf-8"))

    def agreements(self):
        return simulator.connector_agreements(self.registry())

    def message(self, message_id):
        """The signed envelope the generator wrote for this message ID."""
        return json.loads((self.folder / "messages" / f"{message_id}.json").read_text(encoding="utf-8"))

    def claims(self):
        """Every lot's signed claim version 1, delivered after the package import."""
        return [self.message(message_id) for message_id in simulator.opening_messages(self.tag, self.extra)]

    def correction(self, lot=None):
        """A supplier's signed claim version 2, which only a human review uses: LOT-LI-A's, or another corrected
        lot's."""
        messages = simulator.correction_messages(self.tag, self.extra)
        return self.message(messages[lot or self.lots[0]])

    def soh_message(self):
        """The OEM's signed state-of-health event for B-003."""
        return self.message(simulator.soh_message(self.tag))

    def story_messages(self):
        """The signed partner lifecycle events of the story in time order: B-003 goes to a second life, B-005 is
        recycled."""
        return [self.message(payload["message_id"]) for payload in simulator.event_payloads(self.tag)]

    @property
    def successor(self):
        """The second-life passport the story creates for B-003."""
        return simulator.suffixed("B-003-SL1", self.tag)