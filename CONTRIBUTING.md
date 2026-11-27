# Contributing

Contributions are welcome: bug reports, fixes, clearer documentation and new scenario cases. For a larger change, open an issue first so that the approach can be agreed before you write code. Report security problems as described in [SECURITY.md](SECURITY.md), not in a public issue.

## Ground rules

- **Synthetic data only.** Never commit real supplier, personal, or commercial data. The generator in `data/` creates the scenario's business records. Deployed sign-in and decision records can contain real staff identities, so review operational exports before sharing them.
- **No secrets or real identifiers.** Do not commit keys, passwords, connection strings, tokens, tenant or subscription IDs, real email addresses or public IP addresses. Use placeholders such as `00000000-0000-0000-0000-000000000001`, `operator@contoso.example` and documentation addresses from `203.0.113.0/24`. `config/mvp.toml` and `output/` stay on your machine; Git ignores them.
- **Honest verification.** Say what you ran and where. Local tests and a template that compiles show that the code is consistent, not that it works in Azure. Describe Azure behavior as observed only if you deployed and saw it.
- **No prices.** Documentation names cost drivers and their controls, not amounts, because prices depend on region, agreement and date. Your deployment's model token prices go in `config/prices.json`, which Git ignores ([Model prices](docs/deployment.md#model-prices)).
- **One source of truth.** Settings live in `config/mvp.example.toml`, infrastructure in `infra/`, and commands in `dbp_mvp/`. Update the relevant guides in `docs/`, the [README](README.md), and [app/README.md](app/README.md) when the behavior they describe changes.

## Set up

Use Python 3.14, the minimum version and the version that CI, the app image and hosted agents run. The hosted agents need package versions that conflict with the app's, so they get their own environment. From this folder:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt -r app/requirements-dev.txt ruff==0.17.6
python -m venv agents/hosted/.venv
.\agents\hosted\.venv\Scripts\python -m pip install -r agents/hosted/requirements.txt
```

On Linux or macOS use `.venv/bin/python` and `agents/hosted/.venv/bin/python`. Never install both requirement sets into one environment: pip refuses the combination, and installing them one after the other leaves a broken environment.

The template tests need the Azure CLI with Bicep (`az bicep install`) or the standalone Bicep CLI. No Azure sign-in is needed for any test.

## Run the checks

```powershell
.\.venv\Scripts\python -m unittest discover -s tests
.\.venv\Scripts\python -m unittest discover -s agents/evaluation
Push-Location app; ..\.venv\Scripts\python -m unittest discover -s tests; Pop-Location
.\.venv\Scripts\python -m ruff check .
.\agents\hosted\.venv\Scripts\python -m unittest discover -s agents/hosted
```

Some tests skip themselves when a tool is missing: the template tests without Bicep, the script parse test without Windows PowerShell, and the hosted agent tests without their packages. Set `DBP_REQUIRE_HOSTED=1` to make the hosted agent tests fail instead of skipping. The CI workflow in `ci/github-workflows/ci.yml` runs the same checks on Ubuntu in two jobs, one per environment, and also builds the Bicep entry points.

The tests never call Azure, Fabric, or Foundry: they use in-memory fakes. Keep it that way, and add a focused test for new behavior or a fixed bug.

## Style

- Python 3.14 or later. Ruff targets Python 3.14.
- At most 120 characters per line, and the ruff rules in `pyproject.toml`, including bugbear checks for common Python
  mistakes.
- Use comments to explain why, not to restate what the code does. Keep public API contracts, tool directives,
  license notices, and explanations of non-obvious access rules, audit guarantees, or platform constraints.
  Remove obsolete comments and commented-out code. If a comment's purpose is unclear, flag it for review rather
  than deleting it. Keep comment cleanup separate from behavior changes.
- UTF-8 without a byte order mark, LF line endings and a final newline.
- Bicep compiles without warnings; the template tests enforce this.
- Plain, short sentences in the documentation.

## Pull requests

- Keep each pull request to one coherent change.
- Describe what changed, why, and how you verified it.
- Make sure every check above passes and that the documentation matches the change.

By contributing, you agree that your contribution is licensed under the [MIT License](LICENSE).
