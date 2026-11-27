"""Timed progress for long Azure calls, without printing arguments, results or secrets."""

import sys
import threading
import time
from contextlib import contextmanager


def note(text):
    print(f"[wait ] {text}", flush=True)


@contextmanager
def stage(label, heartbeat_seconds=30):
    print(f"\n[start] {label}", flush=True)
    started = time.monotonic()
    stopped = threading.Event()

    def heartbeat():
        while not stopped.wait(heartbeat_seconds):
            print(f"[wait ] {label}: still running ({int(time.monotonic() - started)} s).", flush=True)

    worker = threading.Thread(target=heartbeat, name="dbp-mvp-progress", daemon=True)
    worker.start()
    try:
        yield
    except BaseException:
        print(f"[FAIL ] {label}.", file=sys.stderr, flush=True)
        raise
    else:
        print(f"[done ] {label} ({int(time.monotonic() - started)} s).", flush=True)
    finally:
        stopped.set()
        worker.join()
