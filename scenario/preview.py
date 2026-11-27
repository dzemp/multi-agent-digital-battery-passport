"""Preview the app's screens in a browser with scripted data (local, scripted double, not AI).

The prepare act runs in memory first. The same app is then served on 127.0.0.1 while the story act continues in the
background, so the decisions recorded on the hold, case and second-life screens are picked up as on the deployed app. A
browser cannot send the local identity headers, so a preview-only wrapper replaces them with one persona;
/preview/as/<persona> switches it. The wrapper is for this loopback preview only and is never part of the deployed app.
"""

import socket
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs

from passport.auth import LOCAL_ROLES_HEADER, LOCAL_USER_HEADER
from passport.experience import HUMAN_ROLES, JOURNEY_COOKIE, PERSONA_COOKIE
from passport.settings_store import LocalFile, SettingsAdmin
from passport.store import format_utc

from .acts import PASS, Register
from .series import Series
from .scripted import LocalApp, ScriptedDouble

PERSONAS = {"presenter": ("preview-presenter", ",".join(sorted({*HUMAN_ROLES, "Passport.Fleet.AdventureWorks"}))),
            "reviewer": ("preview-reviewer", "Passport.Reviewer"),
            "operator": ("preview-operator", "Passport.Operator"),
            "compliance": ("preview-compliance", "Passport.Compliance"),
            "admin": ("preview-admin", "Passport.Operator,Passport.Admin"),
            "oem": ("preview-oem", "Passport.OEM,Passport.Fleet.AdventureWorks"),
            "fabrikam": ("preview-fabrikam", "Passport.OEM,Passport.Fleet.Fabrikam"),
            "recycler": ("preview-recycler", "Passport.Recycler"),
            "authority": ("preview-authority", "Passport.Authority"),
            "policy": ("preview-policy", "Passport.PolicyApprover"),
            "no-role": ("preview-no-role", ""),
            "anonymous": None}
COOKIE = "dbp_preview_as"
IDENTITY = (LOCAL_USER_HEADER.encode(), LOCAL_ROLES_HEADER.encode())
PORT = 8009


class Personas:
    """ASGI wrapper that drops any caller-sent identity headers and adds the chosen persona's."""

    def __init__(self, app, default="presenter"):
        self.app, self.default = app, default

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope["path"].startswith("/preview/as/"):
            return await self.switch(scope, send)
        headers = [(k, v) for k, v in scope["headers"] if k.lower() not in IDENTITY]
        identity = PERSONAS[self.chosen(scope["headers"])]
        if identity:
            headers += [(IDENTITY[0], identity[0].encode()), (IDENTITY[1], identity[1].encode())]
        await self.app(dict(scope, headers=headers), receive, send)

    def chosen(self, headers):
        for key, value in headers:
            if key.lower() == b"cookie":
                try:
                    morsel = SimpleCookie(value.decode("latin-1")).get(COOKIE)
                except CookieError:
                    continue
                if morsel and morsel.value in PERSONAS:
                    return morsel.value
        return self.default

    @staticmethod
    async def switch(scope, send):
        name = scope["path"].rsplit("/", 1)[-1]
        target = parse_qs(scope.get("query_string", b"").decode("latin-1")).get("next", ["/exec"])[0]
        if not target.startswith("/") or target.startswith("//") or "\\" in target:
            target = "/exec"
        if name in PERSONAS:
            status, body = 303, b""
            headers = [(b"location", target.encode()),
                       (b"set-cookie", f"{COOKIE}={name}; Path=/; HttpOnly; SameSite=Strict".encode()),
                       (b"set-cookie", f"{PERSONA_COOKIE}=; Path=/; Max-Age=0".encode()),
                       (b"set-cookie", f"{JOURNEY_COOKIE}=; Path=/; Max-Age=0".encode())]
        else:
            status, body = 404, f"Unknown persona. Use one of: {', '.join(PERSONAS)}.".encode()
            headers = [(b"content-type", b"text/plain; charset=utf-8")]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


def live_clock():
    """Wall-clock time that still gives every write a distinct, increasing millisecond, like SQL time."""
    lock, last = threading.Lock(), [datetime.min.replace(tzinfo=timezone.utc)]

    def tick():
        with lock:
            last[0] = max(datetime.now(timezone.utc), last[0] + timedelta(milliseconds=1))
            return format_utc(last[0])
    return tick


def background(app, series, runs, wait, out, prices=None, base=""):
    """The story continues as on the deployed app: it waits for the decisions made in the browser, then the agents
    publish and aftercare runs."""
    try:
        runner = app.runner(series, Register(series, "story", "local", prices=prices, out_dir=runs), out,
                            sleep=time.sleep, app_url=base)
        if runner.execute("story", wait=wait) != PASS:
            return
        total = len(series.batteries)
        out(f"Preview: all {total} passports published and aftercare done. Refresh the screens; press Ctrl+C to "
            "stop.")
    except Exception as error:  # noqa: BLE001 - a background failure must be visible, not silent
        out(f"Preview background run stopped: {type(error).__name__}: {error}")


def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def preview(tag=None, batteries=0, port=PORT, persona="presenter", wait=3600, out=print, serve=None, prices=None):
    if persona not in PERSONAS:
        out(f"Unknown persona {persona!r}. Use one of: {', '.join(PERSONAS)}.")
        return 1
    if serve is None and port_in_use(port):
        out(f"Port {port} on 127.0.0.1 is already in use (an earlier preview?). Stop it or pass --port.")
        return 1
    with tempfile.TemporaryDirectory(prefix="dbp-preview-", ignore_cleanup_errors=True) as tmp:
        series = Series(tag, batteries, output=Path(tmp) / "output")
        runs = Path(tmp) / "runs"
        # The preview runs on fixed in-memory settings; a save lands in this temporary folder and is not re-applied.
        settings = SettingsAdmin(file=LocalFile(Path(tmp) / "settings.json"))
        app = LocalApp(tmp, ScriptedDouble(), clock=live_clock(), settings_admin=settings, prices=prices)
        try:
            register = Register(series, "prepare", "local", prices=prices, out_dir=runs)
            if app.runner(series, register, out).execute("prepare") != PASS:
                return 1
            watch = app.staff.post("/agents/watch/run", json={
                "task_id": "preview-watch", "subject": "battery-due-diligence"})
            if watch.status_code >= 400:
                out(f"Preview stopped: Watch preparation failed with HTTP {watch.status_code}: {watch.text}")
                return 1
            base = f"http://127.0.0.1:{port}"
            threading.Thread(target=background, args=(app, series, runs, wait, out, prices, base),
                             daemon=True).start()
            clean, held = series.clean[0], series.held[0]
            out(f"\nLocal preview (scripted double, not AI) for series {series.name}, signed in as '{persona}':")
            out(f"  Guided journey      {base}/journey   (View as changes roles without changing identity)")
            out(f"  Passport readiness  {base}/exec?series={series.series_id}&live=1")
            out(f"  Public passport     {base}/p/{clean}   (held: {base}/p/{held})")
            out(f"  Decision trail      {base}/trail/{clean}")
            out(f"  Settings (admin)    {base}/preview/as/admin?next=/settings")
            out(f"  Compliance cases    {base}/preview/as/compliance?next=/cases")
            out(f"  Second life         {base}/second-life   (after aftercare: {base}/p/{series.successor})")
            out(f"  Role view (OEM)     {base}/preview/as/oem?next=/p/{clean}   (also recycler, authority; field "
                f"groups as JSON: {base}/passports/{clean}/view)")
            out(f"  Authority console   {base}/preview/as/authority?next=/   (parties, connector log, quarantined "
                "events)")
            out(f"  Switch persona      {base}/preview/as/<{'|'.join(PERSONAS)}>?next=/exec")
            out("The story continues in the background, as on the deployed app: follow the Next step on Passport "
                "readiness to resolve the hold, accept the compliance case citing the site audit report and approve "
                f"{series.batteries[2]}'s second-life request; the agents continue within 10 seconds of each "
                "decision. Next/Back never submit decisions. Ctrl+C stops the preview.")
            (serve or uvicorn_serve)(Personas(app.client.app, persona), port)
            return 0
        finally:
            app.close()


def uvicorn_serve(asgi, port):
    import uvicorn
    uvicorn.run(asgi, host="127.0.0.1", port=port, log_level="warning")
