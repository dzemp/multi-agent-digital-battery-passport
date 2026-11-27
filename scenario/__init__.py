"""The passport scenario: four acts that take one synthetic series through the passport app.

python -m dbp_mvp run <act> drives the deployed app on the developer VM. run local and run preview drive an
in-memory copy of the app with a scripted double in place of the agents: no Azure, no model and not AI.
Synthetic data only; not a legal passport.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))
