"""Orchestrator for the battery passport MVP: python -m dbp_mvp <command>.

Stage A runs on the operator's laptop (bootstrap, VM commands, teardown); stage B runs on the developer VM.
Synthetic data only; not production.
"""


class MvpError(RuntimeError):
    """An expected failure with an actionable message; printed without a traceback."""
