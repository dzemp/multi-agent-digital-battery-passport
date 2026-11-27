"""Azure CLI wrapper. Every Azure call goes through Azure.__call__ (or text/stream/rest) and Azure._execute.

On Windows, az is az.cmd and runs through cmd.exe, so arguments with cmd metacharacters are refused rather than
quoted. Secrets and JSON bodies travel in temporary files referenced as @path, never on the command line.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from . import MvpError

UNSAFE = set('&|<>^%"\r\n')
MAX_COMMAND = 8000
NOT_FOUND = re.compile(r"NotFound|not found|could not be found|\b404\b", re.IGNORECASE)


class AzError(MvpError):
    def __init__(self, args, code, stderr):
        self.args_ = list(args)
        self.code = code
        self.stderr = (stderr or "").strip()
        tail = self.stderr[-1500:] or "(no error output)"
        super().__init__(f"az {' '.join(self.args_[:3])} ... failed (exit {code}):\n{tail}")

    @property
    def not_found(self):
        # Storage can report a missing container under this generic code rather than ContainerNotFound.
        missing_container = ("ContainerOperationFailure" in self.stderr
                             and "The specified container does not exist." in self.stderr)
        return missing_container or bool(NOT_FOUND.search(self.stderr))


class Azure:
    def __init__(self, subscription_id=None):
        self.subscription_id = subscription_id

    def _executable(self):
        executable = shutil.which("az")
        if not executable:
            raise MvpError("Azure CLI (az) not found on PATH. Install it from https://aka.ms/installazurecliwindowsx64")
        return executable

    def _command(self, args, parse):
        args = [str(arg) for arg in args]
        for arg in args:
            if UNSAFE & set(arg):
                raise ValueError(f"refusing an az argument containing a shell metacharacter: {arg[:60]!r}")
        command = [*args, "--only-show-errors"]
        if parse:
            command += ["--output", "json"]
        if len(" ".join(command)) > MAX_COMMAND:
            raise ValueError("az command line too long; pass large values through a file")
        return command

    def _execute(self, command, capture=True):
        """Run az; returns (exit code, stdout, stderr). Tests override this method."""
        result = subprocess.run([self._executable(), *command], capture_output=capture, text=True,
                                encoding="utf-8", errors="replace", check=False)
        return result.returncode, result.stdout or "", result.stderr or ""

    def _run(self, args, parse):
        command = self._command(args, parse)
        code, out, err = self._execute(command)
        if code != 0:
            raise AzError(args, code, err)
        return out

    def __call__(self, *args):
        out = self._run(args, parse=True).strip()
        if not out:
            return None
        try:
            return json.loads(out)
        except ValueError as error:
            raise MvpError(f"az {' '.join(map(str, args[:3]))} returned output that is not JSON") from error

    def text(self, *args):
        return self._run(args, parse=False).strip()

    def find(self, *args):
        """Like __call__, but None when the resource does not exist."""
        try:
            return self(*args)
        except AzError as error:
            if error.not_found:
                return None
            raise

    def stream(self, *args):
        """Show az output directly (what-if); raises on failure."""
        command = self._command(args, parse=False)
        code, _, err = self._execute(command, capture=False)
        if code != 0:
            raise AzError(args, code, err)

    def rest(self, method, url, body=None, resource=None, parameters=None):
        args = ["rest", "--method", method, "--url", url]
        if resource:
            args += ["--resource", resource]
        if parameters:
            args += ["--url-parameters", *[f"{key}={value}" for key, value in parameters.items()]]
        if body is None:
            return self(*args)
        with temporary_file(json.dumps(body)) as path:
            return self(*args, "--headers", "Content-Type=application/json", "--body", f"@{path}")

    @contextmanager
    def parameter_file(self, values):
        """An ARM deployment parameter file as @path; deleted afterwards because it may hold the VM password."""
        document = {"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
                    "contentVersion": "1.0.0.0",
                    "parameters": {key: {"value": value} for key, value in values.items()}}
        with temporary_file(json.dumps(document, indent=2)) as path:
            yield f"@{path}"


@contextmanager
def temporary_file(content, suffix=".json"):
    """A private temporary file with exactly this content (no trailing newline), removed on exit."""
    handle, name = tempfile.mkstemp(prefix="dbp-mvp-", suffix=suffix)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
        yield Path(name)
    finally:
        Path(name).unlink(missing_ok=True)
