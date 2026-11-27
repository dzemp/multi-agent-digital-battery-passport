"""Command line: python -m dbp_mvp <command>. Run python -m dbp_mvp --help for the list."""

import argparse
import sys

from . import MvpError, acts, bootstrap, deploy, devbox, fabric, identity, preflight, teardown
from .az import Azure, AzError
from .config import CONFIG, load
from .state import State

MIN_PYTHON = (3, 14)
MAX_WAIT = 86400
SCENARIOS = ("evaluation", "credential_negatives")
# Options of python -m dbp_mvp run, and the acts each belongs to.
RUN_OPTIONS = {"reason": ("review",), "yes": ("review",), "wait": ("complete", "aftercare", "story"),
               "skip_lifecycle": ("aftercare",),
               "batteries": (*acts.LOCAL, "prepare", "story"), "port": ("preview",), "persona": ("preview",)}


def _parser():
    parser = argparse.ArgumentParser(prog="python -m dbp_mvp",
                                     description="Deploy and remove the synthetic battery passport MVP.")
    parser.add_argument("--config", default=str(CONFIG), help="settings file (default: config/mvp.toml)")
    commands = parser.add_subparsers(dest="command", required=True)

    setup = commands.add_parser("bootstrap", help="stage A on your laptop: Azure resources and the developer VM")
    setup.add_argument("--from", dest="start", choices=bootstrap.ORDER, help="repeat this phase and later ones")
    setup.add_argument("--what-if", action="store_true", help="preview the infrastructure changes only")
    setup.add_argument("--yes", action="store_true", help="skip the deployment confirmation")

    release = commands.add_parser("deploy", help="stage B on the developer VM: sign-in, database, app, agents, "
                                                 "synthetic data, checks")
    release.add_argument("--from", dest="start", choices=deploy.ORDER, help="repeat this phase and later ones")
    source = release.add_mutually_exclusive_group()
    source.add_argument("--skip-build", action="store_true", help="reuse the image of the last deployment")
    source.add_argument("--image", help="deploy this image from the MVP registry instead of building one")

    app = commands.add_parser("app", help="update the existing passport application's image or model prices")
    actions = app.add_subparsers(dest="action", required=True)
    actions.add_parser("redeploy", help="on the developer VM: build and update only the Container App image")
    prices = actions.add_parser("prices", help="on laptop or VM: apply model token prices without a build or Stage B")
    prices.add_argument("--file", dest="prices_file",
                        help="JSON file relative to the current directory; overrides prices_file/inline prices "
                             "for this command only (default: prices from config/mvp.toml)")

    ledger = commands.add_parser("ledger", help="Azure SQL ledger digests (on the developer VM)")
    ledger.add_argument("action", choices=("anchor", "verify"),
                        help="anchor: store a new digest and verify; verify: check against the stored digests")

    data = commands.add_parser("data", help="the passport data (on the developer VM)")
    data.add_argument("action", choices=("reset", "clean"),
                      help="reset: replace SQL and reseed; clean: clear generated data across the app and Fabric, "
                           "replace the task hub and generate a fresh run while keeping infrastructure")
    data.add_argument("--yes", action="store_true", help="skip typing the destructive-operation confirmation")

    scenario = commands.add_parser("scenario", help="optional scenarios: evaluation of the agent instructions, "
                                                    "refused signed messages")
    scenario.add_argument("name", choices=SCENARIOS, help="the scenario to run")
    scenario.add_argument("--apply", action="store_true",
                          help="use the deployed app (developer VM; needs <name> = true under [scenarios]): "
                               "evaluation calls the deployed models (billable), credential_negatives sends the "
                               "messages to the deployed connector; without it both run locally")
    scenario.add_argument("--repeats", type=int, choices=range(1, 6), metavar="1-5",
                          help="evaluation: runs of every case with --apply (default 1)")
    scenario.add_argument("--assignment", choices=("role_fit", "compact_all", "frontier_all"),
                          help="evaluation: restrict the billable comparison to one assignment")
    scenario.add_argument("--shield", action="store_true",
                          help="evaluation with --apply: screen the Prompt Shields probes instead of the models")

    play = commands.add_parser("run", help="the passport scenario: its acts on the deployed app (developer VM), or "
                                           "a local run with a scripted double (not AI)")
    play.add_argument("act", choices=acts.ACTS + acts.LOCAL,
                      help="story plays the whole story on the deployed app while people decide in the app; "
                           "prepare, review, complete, aftercare and status are its single acts; local and preview "
                           "run in memory")
    play.add_argument("--tag", help="a separate series whose IDs end in -TAG (1-6 capital letters or digits); "
                                    "default: the main series")
    play.add_argument("--reason", help="review: the human Reviewer's reason (5-400 characters)")
    play.add_argument("--yes", action="store_true", help="review: record the decision without typing RESOLVE")
    play.add_argument("--wait", type=int, metavar="SECONDS",
                      help="complete, aftercare or story: wait for human decisions in the app; aftercare with a "
                           "positive wait never submits second-life approval; story waits 4 hours by default")
    play.add_argument("--skip-lifecycle", action="store_true", help="aftercare: leave out the Lifecycle agent")
    play.add_argument("--batteries", type=int, metavar="0-50",
                      help="0-50 packs beyond the five story batteries; local/preview or first tagged prepare/story")
    play.add_argument("--port", type=int, help="preview: port on 127.0.0.1 (default 8009)")
    play.add_argument("--as", dest="persona", choices=acts.PERSONAS, help="preview: the signed-in persona "
                                                                         "(default presenter, with all human roles)")

    lake = commands.add_parser("fabric", help="Fabric capacity, workspace and pipelines (on the developer VM)")
    flows = lake.add_subparsers(dest="action", required=True)
    flows.add_parser("check", help="read-only: the capacity, its Fabric ID and the workspace")
    flow = flows.add_parser("run", help="resume the capacity, run one pipeline, pause it again")
    flow.add_argument("flow", choices=("supply", "aftersales", "status"),
                      help="supply: build production data; aftersales: build and import MAIN service/recall data; "
                           "status: rebuild reporting tables")
    flow.add_argument("--new-build", action="store_true", help="supply: prepare a new build of the synthetic series")
    flow.add_argument("--export-id", help="status: rebuild from this export (default: the newest)")

    report = commands.add_parser("report", help="the app's passport status in Fabric (on the developer VM)")
    report.add_argument("action", choices=("publish",),
                        help="publish: export the status from the app into the reporting lakehouse")

    grant = commands.add_parser("roles", help="the MVP's app roles of a user, e.g. viewer roles for test users")
    grant.add_argument("action", choices=("assign", "remove", "list"),
                       help="assign or remove the named roles, or list the roles held")
    grant.add_argument("role", nargs="*", metavar="ROLE",
                       help=f"assign, remove: one or more of {', '.join(identity.DECLARED_ROLES)}")
    grant.add_argument("--user", help="user principal name or object ID (default: the signed-in user)")

    commands.add_parser("powerbi", help="publish the Power BI semantic model and its report on the reporting "
                                        "lakehouse (on the developer VM)")

    commands.add_parser("status", help="completed phases, VM and Fabric capacity state")

    vm = commands.add_parser("vm", help="developer VM: start, stop, access, reset-password")
    actions = vm.add_subparsers(dest="action", required=True)
    actions.add_parser("start", help="start the VM")
    actions.add_parser("stop", help="stop and deallocate the VM")
    access = actions.add_parser("access", help="allow RDP from an address (default: client_ip)")
    access.add_argument("--ip", help="public IPv4 address to allow")
    actions.add_parser("reset-password", help="set and show a new administrator password")

    remove = commands.add_parser("teardown", help="delete everything this MVP created")
    remove.add_argument("--yes", action="store_true", help="skip typing the resource group name")
    remove.add_argument("--keep-app-registration", action="store_true", help="keep the Entra app registration")
    return parser


def _status(config, az, state, out=print):
    out(f"Environment {config.prefix} in {config.location} (subscription {config.subscription_id})")
    out("  Phases recorded on this machine (stage A runs on the laptop, stage B on the developer VM):")
    phases = state.data.get("phases", {})
    for phase in bootstrap.ORDER + deploy.ORDER:
        entry = phases.get(phase)
        out(f"  {phase}  {'done ' + entry['completed_at'] if entry else 'not done'}")
    group = config.names.resource_group
    if not az("group", "exists", "--name", group):
        out(f"  resource group {group} does not exist")
        return
    try:
        out(f"  VM {config.names.vm}: {devbox.power_state(az, config)}")
    except AzError as error:
        if not error.not_found:
            raise
        out(f"  VM {config.names.vm}: not created")
    for capacity in az("resource", "list", "--resource-group", group, "--resource-type",
                       "Microsoft.Fabric/capacities", "--query", "[].id") or []:
        out(f"  Fabric capacity {capacity.rsplit('/', 1)[-1]}: {fabric.capacity_state(az, capacity)}")


def _check_run(args):
    """Every run option belongs to particular acts; one given to another act is refused rather than ignored."""
    for option, allowed in RUN_OPTIONS.items():
        value = getattr(args, option)
        if value not in (None, False) and args.act not in allowed:
            flag = "--as" if option == "persona" else "--" + option.replace("_", "-")
            names = [f"run {act}" for act in allowed]
            listed = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
            raise MvpError(f"{flag} belongs to {listed}.")
    if args.batteries is not None and args.act not in acts.LOCAL and not args.tag:
        raise MvpError("--batteries requires --tag on the deployed app; MAIN uses extra_batteries in config.")
    if args.batteries is not None and not 0 <= args.batteries <= 50:
        raise MvpError("--batteries must be a whole number from 0 to 50.")
    if args.wait is not None and not 0 <= args.wait <= MAX_WAIT:
        raise MvpError(f"--wait must be between 0 and {MAX_WAIT} seconds.")
    if args.port is not None and not 1024 <= args.port <= 65535:
        raise MvpError("--port must be between 1024 and 65535.")


def _scenario(args, config, az, state):
    if args.name == "evaluation":
        options = {"apply": args.apply, "repeats": args.repeats or 1}
        if args.assignment is not None:
            options["assignment"] = args.assignment
        if args.shield:
            options["shield"] = True
        deploy.evaluation(config, az, **options)
    else:
        deploy.credential_negatives(config, az, state, apply=args.apply)


def main(argv=None):
    if sys.version_info < MIN_PYTHON:
        print(f"error: Python {'.'.join(map(str, MIN_PYTHON))} or later is required.", file=sys.stderr)
        return 1
    args = _parser().parse_args(argv)
    try:
        if args.command == "run":
            _check_run(args)
            if args.act in acts.LOCAL:
                return acts.EXIT[acts.local(args.act, args.tag, args.batteries or 0, args.port,
                                            args.persona or "presenter")]
        if args.command == "scenario":
            if args.repeats is not None and args.name != "evaluation":
                raise MvpError("--repeats belongs to scenario evaluation.")
            if args.assignment is not None and (args.name != "evaluation" or not args.apply):
                raise MvpError("--assignment belongs to scenario evaluation with --apply.")
            if args.shield and (args.name != "evaluation" or not args.apply or args.assignment or args.repeats):
                raise MvpError("--shield belongs to scenario evaluation with --apply, without --assignment or "
                               "--repeats.")
            if not args.apply:
                _scenario(args, None, None, None)
                return 0
        price_update = args.command == "app" and args.action == "prices"
        config = load(args.config, prices_file=args.prices_file) if price_update else load(args.config)
        az = Azure(config.subscription_id)
        if price_update:
            deploy.app_prices(config, az)
            return 0
        state = State()
        if args.command == "bootstrap":
            bootstrap.run(config, az, state, start=args.start, what_if=args.what_if, yes=args.yes)
        elif args.command == "deploy":
            deploy.run(config, az, state, start=args.start, skip_build=args.skip_build, image=args.image)
        elif args.command == "app":
            deploy.app_redeploy(config, az, state)
        elif args.command == "ledger":
            deploy.ledger(config, az, args.action)
        elif args.command == "data":
            if args.action == "clean":
                deploy.data_clean(config, az, state, yes=args.yes)
            else:
                deploy.data_reset(config, az, state, yes=args.yes)
        elif args.command == "scenario":
            _scenario(args, config, az, state)
        elif args.command == "run":
            return acts.EXIT[acts.run(config, az, state, args.act, tag=args.tag, reason=args.reason, yes=args.yes,
                                      wait=args.wait or 0, lifecycle=not args.skip_lifecycle, batteries=args.batteries)]
        elif args.command == "fabric" and args.action == "check":
            deploy.fabric_check(config, az, state)
        elif args.command == "fabric":
            deploy.fabric_run(config, az, state, args.flow, new_build=args.new_build, export_id=args.export_id)
        elif args.command == "report":
            deploy.report_publish(config, az, state)
        elif args.command == "roles":
            deploy.roles(config, az, state, args.action, args.role, args.user)
        elif args.command == "powerbi":
            deploy.powerbi_publish(config, az, state)
        elif args.command == "teardown":
            teardown.run(config, az, state, yes=args.yes, keep_app_registration=args.keep_app_registration)
        else:
            preflight.session_check(config, az)
            if args.command == "status":
                _status(config, az, state)
            else:
                {"start": lambda: devbox.start(az, config), "stop": lambda: devbox.stop(az, config),
                 "access": lambda: devbox.access(az, config, args.ip),
                 "reset-password": lambda: devbox.reset_password(az, config)}[args.action]()
    except MvpError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        if args.command == "app" and args.action == "prices":
            print("\nInterrupted. Rerun the same app prices command to verify or retry the price-only update; "
                  "no build or data reset is performed.", file=sys.stderr)
        elif args.command == "app":
            print("\nInterrupted. Rerun app redeploy to build and deploy a fresh image; no data reset is performed.",
                  file=sys.stderr)
        else:
            print("\nInterrupted. Rerun the same command to continue; finished phases are skipped.", file=sys.stderr)
        return 130
    return 0
