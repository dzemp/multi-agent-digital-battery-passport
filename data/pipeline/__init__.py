"""Pipeline rules shared by the local simulator and the Fabric notebooks.

The notebooks run the same source text: embedded_source() joins the modules in dependency order and drops the
package-relative imports, which is why every top-level name must be unique across the modules.
"""

import hashlib
from pathlib import Path

SOURCES = ("rules.py", "deliveries.py", "flows.py")
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
PACKAGE_IMPORTS = ("from .", "from data.", "import data")


def source_text(name):
    return _text(HERE / name)


def _text(path):
    return Path(path).read_text(encoding="utf-8").replace("\r\n", "\n")


def code_hash():
    """sha256 over the rule sources with LF line endings, so a checkout's line-ending setting does not matter."""
    hasher = hashlib.sha256()
    for name in SOURCES:
        hasher.update(name.encode("utf-8") + b"\0" + source_text(name).encode("utf-8") + b"\0")
    return hasher.hexdigest()


def embedded_source(extra=()):
    """The rule modules, then any extra files of this MVP (such as the notebook steps), as one script without the
    imports between them."""
    parts = []
    for path in [HERE / name for name in SOURCES] + [Path(path).resolve() for path in extra]:
        label = path.relative_to(ROOT).as_posix()
        lines = []
        for line in _text(path).split("\n"):
            if line.startswith(PACKAGE_IMPORTS):
                if "(" in line:
                    raise ValueError(f"{label}: keep package imports on one line so they can be removed")
                continue
            lines.append(line)
        parts.append(f"# ---- {label} ----\n" + "\n".join(lines).strip() + "\n")
    return "\n\n".join(parts)
