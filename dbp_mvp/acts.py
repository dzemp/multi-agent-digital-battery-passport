"""python -m dbp_mvp run <act>: the scenario's acts against the deployed passport app, and the local run.

story, prepare, review, complete, aftercare and status run on the developer VM after deploy, signed in as the
operator who deployed (az login). story plays the whole story unattended while people make the human decisions in the
app; the other acts are its single steps. Without --tag they use the main series that deploy stage B6 imported; a tag
imports a separate copy from the local synthetic package. local and preview run anywhere with the app's packages
installed; they use an in-memory app and a scripted double, not Azure and not AI.
"""

import json
import sys
from dataclasses import replace

from data import simulator
from scenario.acts import FAIL, NoAnswer, PASS, Register, Runner, STOPPED, Stop

from . import MvpError, deploy, preflight, smoke, supply

ACTS = ("story", "prepare", "review", "complete", "aftercare", "status")
LOCAL = ("local", "preview")
# How long run story waits for the human decisions in the app unless --wait says otherwise.
STORY_WAIT = 14400
PERSONAS = ("presenter", "reviewer", "operator", "compliance", "admin", "oem", "fabrikam", "recycler", "authority",
            "policy", "no-role", "anonymous")
EXIT = {PASS: 0, FAIL: 1, STOPPED: 3}
PUBLISH_HINT = ("Fabric is deployed: run python -m dbp_mvp report publish to refresh the reporting lakehouse; its SQL "
                "endpoint views and the Power BI report read from it. The acts never publish the report themselves.")


class Response:
    """The parts of an HTTP answer the scenario reads."""

    def __init__(self, status_code, text):
        self.status_code, self.text = status_code, text or ""

    @property
    def content(self):
        return self.text.encode("utf-8")

    def json(self):
        return json.loads(self.text)


class Token:
    """The operator's token for the app, fetched once and again after the app answers 401."""

    def __init__(self, az, app_id):
        self.az, self.app_id, self.value = az, app_id, None

    def __call__(self, refresh=False):
        if refresh or self.value is None:
            self.value = smoke.app_token(self.az, self.app_id)
        return self.value


class Http:
    """The deployed app over HTTPS with the operator's token, or anonymously without one."""

    def __init__(self, base_url, token=None, runtime=None):
        self.base, self.token, self.runtime = base_url.rstrip("/"), token, runtime or deploy.Runtime()

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)

    def request(self, method, path, json=None, data=None, files=None):
        if method not in ("GET", "POST") or (method == "POST" and self.token is None):
            raise ValueError(f"unsupported request {method} {path}")
        status, text = self._send(method, path, json, data, files, self.token() if self.token else None)
        if status == 401 and self.token:
            status, text = self._send(method, path, json, data, files, self.token(refresh=True))
        if status is None:
            raise NoAnswer(text)
        return Response(status, text)

    def _send(self, method, path, body, data, files, token):
        url, r = self.base + path, self.runtime
        if method == "GET":
            return r.http_get_auth(url, token) if token else r.http_get(url)
        if files:
            (field, (name, content, content_type)), = files.items()
            return r.http_post_multipart(url, data or {}, (field, name, content_type, content), token)
        return r.http_post(url, body if body is not None else {}, token)


def _series(config, tag, output):
    from scenario.series import Series
    prepared = simulator.load_state(output, tag)
    try:
        return Series(tag, prepared["batteries"] if prepared else config.extra_batteries, output=output)
    except ValueError as error:
        raise MvpError(f"--tag {tag}: {error}") from None


def _main_seed(config, state, app_id, app_url, runtime):
    """The main series is imported by deploy stage B6; the scenario only checks that the import is current."""
    def seed(runner):
        prepared = simulator.load_state(runtime.output)
        try:
            current = bool(prepared and prepared["batteries"] == config.extra_batteries and state.done(
                "B6", deploy.seed_fingerprint(config, app_id, app_url, prepared, runtime.output)))
        except OSError:
            current = False
        if not current:
            raise Stop(STOPPED, "the main series is not imported with the current settings. Run python -m dbp_mvp "
                                "deploy (stage B6 imports it), or use --tag for a separate series.")
        runner.out(f"  package {prepared['build_id']} and its evidence were imported by deploy stage B6.")
    return seed


def _tag_seed(config, az, app_id, app_url, outputs, tag, runtime):
    """A tagged series: its local package imported and its evidence uploaded, as stage B6 does for the main one."""
    def seed(runner):
        existing = simulator.load_state(runtime.output, tag)
        if existing and existing["batteries"] != config.extra_batteries:
            count = len(simulator.STORY) + existing["batteries"]
            raise Stop(STOPPED, f"series {tag} has {count} batteries, but extra_batteries is "
                                f"now {config.extra_batteries}. Use a new --tag.")
        try:
            with runtime.container(outputs["appStorageUrl"], supply.INTAKE) as container:
                supply.seed_app(config, az, app_url, app_id, container, runtime.http_post, runtime.http_post_multipart,
                                runtime.sleep, runtime.output, runner.out, tag=tag)
                prepared = simulator.load_state(runtime.output, tag)
                deploy.seed_aftersales(config, az, outputs, app_url, app_id, prepared, container, runtime, runner.out)
        except MvpError as error:
            raise Stop(FAIL, str(error)) from None
    return seed


def _workbook(config, az, outputs):
    """The executive workbook's portal link from the infrastructure outputs, or why it is not known."""
    ids = outputs.get("workbookIds") or []
    link = deploy.workbook_links(preflight.session_check(config, az)["tenant_id"], ids).get("executive") \
        if ids else None
    if not link:
        return ("executive workbook: link not known (no workbook in the infrastructure outputs); see the deploy "
                "summary or the app's Settings page.")
    return ("executive workbook (the same measures from Application Insights snapshots, written at most every 5 "
            f"minutes): {link}")


def run(config, az, state, act, tag=None, reason=None, yes=False, wait=0, lifecycle=True, runtime=None, out=print,
        confirm=input, interactive=None, batteries=None):
    """One act against the deployed app. Returns PASS, FAIL or STOPPED; every act except status writes a register."""
    runtime = runtime or deploy.Runtime()
    app_id, app_url = deploy._deployed_app(state)
    outputs = deploy._command(config, az, state)
    if act != "status" and not state.outputs("B4").get("agent_mode"):
        raise MvpError("The agents are not deployed. Run python -m dbp_mvp deploy on the developer VM first.")
    interactive = sys.stdin.isatty() if interactive is None else interactive
    if tag:
        # Each series keeps its chosen size. Later acts do not need to repeat --batteries.
        try:
            simulator.check_options(batteries if batteries is not None else config.extra_batteries, tag)
        except ValueError as error:
            raise MvpError(str(error)) from None
        saved = simulator.load_state(runtime.output, tag)
        count = batteries if batteries is not None else (saved["batteries"] if saved else config.extra_batteries)
        config = replace(config, extra_batteries=count)
    if act == "review" and not yes and not interactive:
        out("STOPPED: review records a human decision; run it in a terminal to confirm, or add --yes.")
        return STOPPED
    series = _series(config, tag, runtime.output)
    http = Http(app_url, Token(az, app_id), runtime)
    seed = _main_seed(config, state, app_id, app_url, runtime) if tag is None else \
        _tag_seed(config, az, app_id, app_url, outputs, tag, runtime)
    register = Register(series, act, "tenant", prices=config.prices, out_dir=runtime.output / "runs")
    runner = Runner(http, series, register, seed=seed, anonymous=Http(app_url, None, runtime), confirm=confirm,
                    sleep=runtime.sleep, out=out, app_url=app_url,
                    workbook=_workbook(config, az, outputs) if act in ("aftercare", "story") else None)
    if act == "status":
        try:
            runner.show_status()
        except Stop as stop:
            raise MvpError(stop.message) from None
        return PASS

    def anchor():
        out("  anchoring the SQL ledger digest...")
        try:
            deploy.ledger(config, az, "anchor", runtime=runtime, out=out)
        except Exception as error:  # the story's outcome does not depend on the anchor
            out(f"  the ledger anchor did not complete ({error}); run python -m dbp_mvp ledger anchor later.")

    options = {"review": {"reason": reason, "yes": yes} if reason else {"yes": yes}, "complete": {"wait": wait},
               "aftercare": {"lifecycle": lifecycle, "wait": wait},
               "story": {"wait": wait or STORY_WAIT, "anchor": anchor}}.get(act, {})
    verdict = runner.execute(act, **options)
    if verdict == PASS and config.deploy_fabric and (act in ("prepare", "complete", "story") or
                                                     (act == "aftercare" and lifecycle)):
        out(PUBLISH_HINT)
    return verdict


def local(act, tag=None, batteries=0, port=None, persona="presenter", out=print):
    """run local or run preview: the in-memory app with the scripted double. Returns PASS, FAIL or STOPPED."""
    try:
        from scenario import preview, scripted
    except ImportError as error:
        raise MvpError(f"run {act} needs the app's packages ({error.name} is missing). Install them with "
                       "python -m pip install -r requirements.txt.") from None
    from scenario.series import Series
    try:
        Series(tag, batteries)
    except ValueError as error:
        raise MvpError(str(error)) from None
    if act == "preview":
        return PASS if preview.preview(tag, batteries, port or preview.PORT, persona, out=out) == 0 else STOPPED
    results = scripted.play(tag, batteries, out=out)
    return next((verdict for verdict, _ in results.values() if verdict != PASS), PASS)
