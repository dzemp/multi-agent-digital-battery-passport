"""Phase progress in output/state.json: which phases finished, with which inputs, and their non-secret outputs."""

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import MvpError
from .config import OUTPUT

VERSION = 1
SECRET_KEY = re.compile(r"password|secret", re.IGNORECASE)


class StateError(MvpError):
    pass


def fingerprint(value):
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def files_fingerprint(folder):
    folder = Path(folder)
    digest = hashlib.sha256()
    for path in sorted(p for p in folder.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        digest.update(path.relative_to(folder).as_posix().encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _guard(value, where="outputs"):
    if isinstance(value, dict):
        for key, item in value.items():
            if SECRET_KEY.search(str(key)):
                raise StateError(f"refusing to store {where}.{key}: secrets are never written to state")
            _guard(item, f"{where}.{key}")
    elif isinstance(value, list):
        for item in value:
            _guard(item, where)


class State:
    def __init__(self, path=None):
        self.path = Path(path) if path else OUTPUT / "state.json"
        self.data = {"version": VERSION, "identity": None, "phases": {}}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise StateError(f"{self.path} is unreadable ({error}). Delete it to start over.") from error
            if data.get("version") != VERSION or not isinstance(data.get("phases"), dict):
                raise StateError(f"{self.path} has an unknown format. Delete it to start over.")
            self.data = data

    def bind(self, identity):
        """Refuse to mix environments: state belongs to one subscription, prefix and region."""
        current = self.data.get("identity")
        if current and current != identity:
            raise StateError(
                f"{self.path} belongs to prefix '{current.get('prefix')}' in subscription "
                f"{current.get('subscription_id')} ({current.get('location')}). Run teardown for that environment "
                "first, or move the state file away if that environment no longer exists.")
        if not current:
            self.data["identity"] = dict(identity)
            self.save()

    def done(self, phase, inputs_fingerprint=None):
        entry = self.data["phases"].get(phase)
        return bool(entry) and (inputs_fingerprint is None or entry.get("fingerprint") == inputs_fingerprint)

    def outputs(self, phase):
        return dict(self.data["phases"].get(phase, {}).get("outputs", {}))

    def record(self, phase, inputs_fingerprint, outputs=None):
        outputs = dict(outputs or {})
        _guard(outputs)
        self.data["phases"][phase] = {"fingerprint": inputs_fingerprint,
                                      "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                      "outputs": outputs}
        self.save()

    def amend(self, phase, outputs):
        """Merge outputs into a finished phase, keeping its fingerprint (a later phase changed what it describes)."""
        entry = self.data["phases"].get(phase)
        if not entry:
            raise StateError(f"cannot amend {phase}: it has not finished")
        outputs = dict(outputs)
        _guard(outputs)
        entry["outputs"] = {**entry.get("outputs", {}), **outputs}
        self.save()

    def counter(self, name):
        return int(self.data.get("counters", {}).get(name, 0))

    def bump(self, name):
        """Increase a counter that survives --from, so a retried check uses a new task ID."""
        self.data.setdefault("counters", {})[name] = self.counter(name) + 1
        self.save()
        return self.counter(name)

    def forget_from(self, phase, order):
        """Forget a phase and every later phase, so the next run repeats them (the --from option)."""
        if phase not in order:
            raise StateError(f"unknown phase {phase}; choose one of {', '.join(order)}")
        for later in order[order.index(phase):]:
            self.data["phases"].pop(later, None)
        self.save()

    def clear(self):
        self.data = {"version": VERSION, "identity": None, "phases": {}}
        if self.path.exists():
            self.path.unlink()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, indent=2, sort_keys=True)
                stream.write("\n")
            os.replace(temporary, self.path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
