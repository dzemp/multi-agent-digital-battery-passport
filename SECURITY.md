# Security policy

This is a reference MVP that runs on synthetic data. It is maintained on a best-effort basis, without a response-time commitment. Still, please report security problems privately so that people who deployed it can act first.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: open the repository's **Security** tab and choose **Report a vulnerability**. Do not open a public issue, pull request or discussion for a suspected vulnerability.

Please include:

- the affected file, template or command, and the commit you looked at;
- how to reproduce the problem, ideally with the local tests or `run local`;
- what an attacker could do, and under which preconditions;
- a suggested fix, if you have one.

## Scope

In scope is everything in this repository: the command-line tool, the Bicep templates, the passport app, the hosted agents, the Fabric notebooks and pipelines, and the CI workflow. Examples:

- the deployment is less isolated than the [architecture guide](docs/architecture.md) describes;
- a role check in the app can be bypassed, or an agent identity can reach another agent's tools;
- a secret is written to disk, to a log, to the evidence registers or to a command line;
- the developer VM setup installs a downloaded tool without a valid trusted signature.

Out of scope:

- vulnerabilities in Azure, Microsoft Fabric, Microsoft Foundry or other products themselves. Report those to the vendor, for Microsoft through the [Microsoft Security Response Center](https://msrc.microsoft.com/report);
- the [known limits](app/README.md#9-known-limits) of this MVP, unless the actual behavior is worse than documented.

## Supported versions

Only the latest commit on the default branch receives fixes.

## If you deployed it

- Use synthetic data only. The MVP is not built to protect real personal or commercial data.
- The developer VM's password is shown once; `python -m dbp_mvp vm reset-password` replaces it.
- The app's sign-in secret is stored only in Azure; `python -m dbp_mvp deploy --from B3` replaces it.
- `python -m dbp_mvp teardown` removes everything the MVP created.
