# Deployment guide

This guide covers Azure prerequisites, configuration, deployment, maintenance, and infrastructure troubleshooting. See the [architecture guide](architecture.md) for the components and the [scenario walkthrough](scenario-walkthrough.md) for the scenario and app screens.

All commands run from the MVP folder (the folder with `dbp_mvp/`). On your laptop, `python` means Python 3.14 or later; on the developer VM, use `.\.venv\Scripts\python`.

## Overview: two stages

The deployment uses two stages because SQL, the storage accounts, and the Foundry account have public network access disabled. Stage A provisions Azure resources from your laptop. Its preflight also checks Microsoft Entra permissions through Microsoft Graph and, when Fabric is enabled, checks Fabric access through the Fabric API. Stage B runs on the developer VM and configures the app and data services over the private network. The [architecture guide](architecture.md#2-network-and-trust-boundaries) describes the network design.

| Stage | Runs on | Reaches | Creates |
|---|---|---|---|
| A: `bootstrap` | Your laptop | Azure Resource Manager, Microsoft Graph, and the Fabric API when Fabric is enabled | Resource group, network, private DNS, developer VM, storage, SQL server and database, container registry, Container Apps environment, monitoring, Foundry account and project with model deployments, and Fabric capacity. The VM then installs its tools, clones the repository, and prepares a Python environment. |
| B: `deploy` | The developer VM | Private endpoints, the app, Fabric, and Durable Task Scheduler when agent mode is enabled | The Entra app registration, SQL schema and grants, app image and Container App, five agents, durable workflow resources, Fabric items, and synthetic data import. |

Stage B and the scenario run on the VM inside the network, signed in with the operator's Azure CLI account. They do not use SQL passwords or storage keys.

**API versions.** Bicep templates and direct Azure Resource Manager calls use stable, non-preview API versions supported by the resource providers and Bicep type library. Azure CLI commands, such as `az acr build`, select their own API versions.

## 1. Before you start

| Need | Why | How to check |
|---|---|---|
| An Azure subscription in the public Azure cloud | Everything is deployed there and billed to it. | `az account show` |
| **Owner**, or **Contributor** plus **User Access Administrator** (or Role Based Access Control Administrator), at subscription or management-group scope | Stage A creates role assignments for the app's identity and for you. If the role is eligible through Privileged Identity Management, activate it first. | The preflight checks it. |
| Permission to register applications in Microsoft Entra ID | Stage B registers the app that signs staff in. Allowed for all users by default, or through Application Developer, Application Administrator, Cloud Application Administrator or Global Administrator. | The preflight checks it when it can and warns otherwise. |
| **An active Cloud Application Administrator, Application Administrator or Global Administrator role during stage B** | B4 grants each hosted agent's identity its app role on the passport app. Microsoft Graph treats this as granting an application permission and requires a supported directory role, of which these are the usual ones (Privileged Role Administrator and a few identity-sync roles also qualify); app ownership alone is not enough, and Global Reader cannot. If the role is eligible through Privileged Identity Management, activate it before stage B and sign in again. See [Microsoft Entra roles for stage B](#microsoft-entra-roles-for-stage-b). | The preflight warns when none is active. |
| A region with Foundry agents, the three models, and Fabric | Default `swedencentral`. The models use Data Zone Standard, so prompts are processed within the Microsoft-defined data zone for the region, not necessarily within one country. Check regional availability for your chosen location. | The preflight checks the VM image and size and the model quota. |
| Model quota for capacity 200 of DataZoneStandard per default model | Effective RPM and TPM vary by model and SKU. Request sufficient quota; lowering `capacity` may also lower rate limits. | The preflight checks quota against requested capacity. Confirm effective limits in Foundry. |
| Eligible Windows 10/11 Enterprise per-user licenses with multitenant hosting rights | The developer VM runs Windows 11 Enterprise with license type `Windows_Client`. The tool cannot check this. | Your licensing agreement. |
| **Manual, once per tenant:** Microsoft Fabric activated for you, and permission for you to create Fabric items | Azure cannot create the Fabric capacity until Fabric is activated. Stage B creates a workspace, lakehouses, notebooks and pipelines. Fabric offers no API for sign-up, so the tool cannot do it for you; see [step 3](#3-stage-a-on-your-laptop). Not needed with `deploy_fabric = false`. | The preflight checks access through the Fabric API. |
| Possibly: the subscription feature `Microsoft.Network/AllowBringYourOwnPublicIpAddress` | Not needed by the template, which uses only Azure-allocated public IP addresses (the VM's RDP address and, optionally, the NAT gateway's). Some subscriptions still require it to create them; A1 then stops and prints the remedy. The feature needs Microsoft's approval. | `az feature show --namespace Microsoft.Network --name AllowBringYourOwnPublicIpAddress --query properties.state` |
| Optional: Microsoft Defender for Servers Plan 2 | Only for `vm_access = "jit"`. | The preflight checks it. |
| Optional: Power BI Pro or Premium Per User licenses | Only for people who open the optional semantic model or report on an F2 capacity. | Microsoft 365 admin center. |

**On your laptop:** Python 3.14 or later and Azure CLI 2.67.0 or later. The preflight installs Bicep if it is missing and upgrades versions older than 0.49.2 so it can validate the templates. Stage A needs no Python packages. The developer VM installs its own tools and packages.

**The repository must be reachable by the VM.** The VM clones the repository over HTTPS (by default this checkout's `origin` and upstream branch). A public GitHub repository works as is. For a private one, see [troubleshooting](#7-troubleshooting).

## 2. Settings

Copy `config/mvp.example.toml` to `config/mvp.toml` (Git ignores it). It holds no secrets.

| Setting | Default | Meaning |
|---|---|---|
| `subscription_id` | required | `az account show --query id -o tsv` |
| `operator_upn` | required | The user you sign in with: `az ad signed-in-user show --query userPrincipalName -o tsv`. This user becomes the SQL administrator and gets the Operator, Reviewer, Connector and Compliance app roles. |
| `prefix` | required | 3-6 lowercase letters or digits, starting with a letter. Names every resource, for example `<prefix>-rg`. Use a new prefix for a second, independent environment. |
| `client_ip` | required | Your public IPv4 address, allowed to reach the VM over RDP. |
| `location` | `swedencentral` | Azure region for everything. |
| `vm_access` | `source-ip` | `source-ip`: RDP open to `client_ip` only. `jit`: Defender for Cloud just-in-time access. |
| `vm_auto_shutdown`, `time_zone` | `19:00`, `W. Europe Standard Time` | Daily VM shutdown time and its Windows time zone ID. |
| `deploy_fabric`, `fabric_sku` | `true`, `F2` | Fabric capacity, workspace and reporting copy. Without Fabric the synthetic package is built locally. |
| `enable_nat_gateway` | `false` | A fixed outbound address for the agents subnet. |
| `warm_replica` | `false` | Set to `true` for a minimum of one passport app replica even when agent mode is off. Agent mode already sets that minimum for its workflow worker. |
| `enable_alerts`, `alert_email` | `false`, empty | Turns on the three log alert rules; needs an address that someone reads. |
| `powerbi` | `false` | Publishes the optional semantic model and its report during `report publish`. |
| `extra_batteries` | `0` | Additional synthetic batteries beyond the five-battery story (0-50). More batteries mean more agent calls. From three added batteries, about a fifth carry seeded data issues, which open more reviews, compliance cases and pipeline blocks ([seeded issues](scenario-walkthrough.md#1-the-story)). The party registry that stage B3 deploys lists every issue lot, whatever this value; an app deployed before seeded issues existed needs a `deploy` rerun to update it. |
| `repo_url`, `repo_ref` | empty | Where the VM clones from (HTTPS only) and which branch or tag. Empty: this checkout's `origin` and upstream branch. |
| `prices_file` | empty | Optional model token price list next to the settings file, usually `prices.json`, so the screens show a model cost instead of "not measured". See [Model prices](#model-prices). |
| `[scenarios] evaluation` | `false` | Allows `scenario evaluation --apply`, which makes billable model calls. |
| `[scenarios] credential_negatives` | `false` | Allows `scenario credential_negatives --apply`, which sends forged and replayed synthetic messages to the deployed connector. The rejections stay in its append-only log. |
| `[[models]]` | three defaults, capacity `200` each | Model deployments: `name`, `version`, `sku` (default `DataZoneStandard`), `capacity`. An explicit list replaces every default, so include all three models named by the agent contracts. |

The VM receives a copy of these settings during stage A. **Change settings on your laptop**, rerun `bootstrap`, then rerun `deploy` on the VM. **Model prices are the exception:** use `app prices` directly from either machine; no VM update or deployment phases are needed.

**SQL compute is fixed, not a TOML setting.** Provisioning, `data reset` and `data clean` use paid Azure SQL Database General Purpose serverless on Standard-series (Gen5): `GP_S_Gen5_2`, minimum 0.5 and maximum 2 vCores, with auto-pause after 60 idle minutes. Storage remains billable while compute is paused.

**Passport Container App sizing:** Stage B deploys **1 vCPU and 2 GiB per replica, with a maximum of 3 replicas**. The minimum is 1 in agent mode or with `warm_replica = true`, otherwise 0. These limits apply to the passport app, not the Foundry hosted agents. On an existing environment, use `python -m dbp_mvp deploy` from the updated VM checkout. B3 detects changes to the app template; `app redeploy` alone changes only the image.

### Model changes and rate limits

The defaults are `gpt-6.1-sol` (`2026-09-29`) for Supplier, `gpt-5.6-luna` (`2026-07-09`) for Carbon, Issuance and Lifecycle, and `gpt-5.6-terra` (`2026-07-09`) for Watch. See the [selection rationale](agents-and-process.md#36-model-selection-and-request-capacity).

Capacity is allocated **per model deployment, not per agent**. The defaults request capacity `200` per deployment. The Sweden Central `DataZoneStandard` catalog, checked on November 16, 2026, listed 1 RPM and 1,000 TPM per capacity unit for these versions, so capacity `200` targets 200 RPM and 200,000 TPM per deployment. Effective limits vary by model, SKU, region, and current quota. Tool-calling activations can make several model requests, and event-triggered workflows can overlap manual runs. Confirm each deployment's effective limits in Foundry. Capacity provides request headroom, not a guarantee of unlimited throughput or freedom from short-window burst throttling. Standard deployments are pay-per-token, not reserved PTU purchases.

For an existing deployment, update any explicit `[[models]]` list, then run `bootstrap --from A0` on the laptop to check quota, apply A1, and copy the settings to the VM. Update the VM's checkout and run `deploy` there. Model and contract changes invalidate the relevant app and agent fingerprints. **Changing the settings file, running only A2, or rebuilding only the app does not change model capacity.** Check each deployment's `sku.capacity` and `properties.rateLimits` with `az cognitiveservices account deployment list`; effective RPM and TPM limits can change. Deployments omitted from an explicit model list are not automatically deleted.

If throttling persists, inspect which deployment is exhausted, include all callers sharing it, and respect the service's retry delay with bounded backoff/request pacing. Do not retry failed agent task IDs expecting a new model call: the app replays their stored result. Use a fresh authorized task after addressing capacity; do not delete the audit trail or reset passport data to clear a rate-limit error. [Azure quota and rate-limit guidance](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/quota).

### Model prices

Without a price list, every model cost shows "not measured"; the app never guesses a price. With `prices_file`, the decision trail and **Passport readiness** show an estimated model cost per agent run, in total and per published passport. Run registers contain the same estimate, and Application Insights `agent.run` events get `estimated_cost_usd` when the currency is USD.

1. **Create `config/prices.json`** next to `config/mvp.toml`. Git ignores both files; keep prices out of the repository ([contributing rules](../CONTRIBUTING.md)).

   ```json
   {
     "currency": "USD",
     "source": "Azure Retail Prices API <date read> swedencentral Data Zone Standard short context",
     "models": {
       "gpt-5.6-luna": {"input_per_million": <input price>, "output_per_million": <output price>},
       "gpt-5.6-terra": {"input_per_million": <input price>, "output_per_million": <output price>},
       "gpt-6.1-sol": {"input_per_million": <input price>, "output_per_million": <output price>}
     }
   }
   ```

2. **Enter the pay-as-you-go price per million tokens that matches your deployments.**
   - Model names exactly as in `[[models]]`. A missing model stays unpriced and the totals say "partially measured".
   - Data Zone prices for the default `DataZoneStandard` SKU, not Global prices, in the region of `location`.
   - Short-context prices. Prompt agents stop at 24,000 reported tokens per activation, and hosted agents read at most eight sections per attempt. Verify the model's pricing tiers rather than treating a tool-read count as a token count.
   - `currency`: the Azure price list is in USD, and telemetry records a cost only in USD.
   - `source` (optional, at most 200 characters): the price list and the date you read it. Up to 100 letters, digits, spaces and `_.:/{}()+-` also become the `price_version` of telemetry events; otherwise they show `custom`.

   Read the prices on the [Azure OpenAI pricing page](https://azure.microsoft.com/pricing/details/azure-openai/) or in the [Azure pricing calculator](https://azure.microsoft.com/pricing/calculator/); signed in, the calculator shows your agreement's prices. The public [Azure Retail Prices API](https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices) returns the same retail prices without sign-in. `Inp` is the input meter and `Opt` the output meter:

   ```powershell
   $filter = "serviceName eq 'Foundry Models' and armRegionName eq 'swedencentral' and contains(meterName, 'ShortCo')"
   (Invoke-RestMethod "https://prices.azure.com/api/retail/prices?`$filter=$([uri]::EscapeDataString($filter))").Items |
       Where-Object meterName -Match '^(6\.1-sol|5\.6 luna|5\.6 terra) ShortCo (Inp|Opt) Std DZ ' |
       Sort-Object meterName | Format-Table meterName, retailPrice, currencyCode, effectiveStartDate
   ```

3. **Apply to an existing app from your laptop or the VM.** Use a checkout containing the `app prices` command, Python 3.14+ and Azure CLI, signed in as the configured `operator_upn` in the configured subscription. You need permission to read the resource group and update its Container App (for example, Contributor on the group); no new directory-role grants or private-network access are needed. From the MVP folder:

   ```powershell
   python -m dbp_mvp app prices --file .\config\prices.json
   ```

   `--file` is relative to your current directory (absolute paths also work). It overrides `prices_file` or inline `prices` for this command only, so you do not need to edit `mvp.toml`, copy anything to the VM or run bootstrap. The file is validated before any Azure changes. Environment identity, the MVP resource-group tag and the existing app are checked before updating it. No local `output/state.json` is required.

   If you prefer a saved file reference, set `prices_file = "prices.json"` in `config/mvp.toml` and run `python -m dbp_mvp app prices` without `--file`. This setting's path is relative to `mvp.toml`, not the current directory. Remove any inline `prices` entry before setting `prices_file`; configuring both in TOML is rejected.

4. **Check and update.** Refresh the deployed app. Its **Settings** page (requires `Passport.Admin`) shows "Model prices: Configured (USD)"; runs with recorded token counts show their estimated costs. To change rates, edit the same JSON, update `source` and rerun the same `app prices` command. The command rereads the file each time; it does not watch it automatically.

**Exact update scope.** `app prices` updates only `DBP_PRICES_B64` on the existing `passport` container. Azure creates a new revision with the **same image** and restarts the app; the command checks the requested revision, image and prices, then `/health/ready`, before reporting success. An unchanged list only checks readiness, without creating another revision. No image build, VM update, B1-B8 phase, SQL/Blob/Fabric write, data import, agent/model deployment, identity change or sign-in-secret renewal is performed. Other environment variables and infrastructure are left alone. Normal app workers resume after restart, so pending business activity can still run. **Do not use `app redeploy`, `deploy --from B3` or `data clean` just to update prices.**

**First deployment and later maintenance.** On first deployment, B3/B4 seed prices from their settings when none are deployed; Stage A still carries configured prices inline to the VM. Later B3/B4 deployments with this version of the tool preserve the app's applied price list instead of replacing it with a stale or empty VM copy. Use `app prices` for intentional changes. Update an older VM checkout before a future full deployment, since older orchestrators can overwrite the list. Price-only updates themselves need no VM change. Run registers continue to use the local settings of the machine that runs the act, not the deployed environment variable; keep that local list current if you use their cost estimates.

**Failure/retry.** A rejected file or environment check makes no Azure change. An Azure update or readiness failure is reported as an error; an older healthy revision is not accepted as proof that the new prices are serving. Inspect **Container App > Revisions** and **Monitoring > Log stream**, fix the cause, then rerun the same `app prices` command, including `--file` when used. A failed update can leave a revision behind; no automatic rollback or data reset is attempted.

**What the estimate covers.** Per run: reported input tokens × input price + output tokens × output price. Output tokens include reasoning tokens. Cached-input discounts are not applied, so the estimate can overstate input cost. It excludes agreement discounts, currency conversion, hosting, storage, SQL, Foundry, Fabric and review work. Runs without reported tokens, such as local scripted runs, stay "not measured". The screens price stored token counts with the current list, so earlier runs are repriced; telemetry events keep the price that applied at the time of the run. Prices change: check them before each use and update `source`.

## 3. Stage A on your laptop

**Manual step before the first run (with `deploy_fabric = true`):** in a browser, sign in to [app.fabric.microsoft.com](https://app.fabric.microsoft.com) as `operator_upn` and accept the free Fabric sign-up. If self-service sign-up is blocked, an administrator assigns you a Fabric (Free) license in the Microsoft 365 admin center. A Fabric administrator must also allow **Users can create Fabric items** ([Enable Fabric](https://learn.microsoft.com/fabric/admin/fabric-switch)). Complete these steps for the tenant and user before continuing.

```powershell
az login --tenant <tenant ID>
az account set --subscription <subscription ID>
python -m dbp_mvp bootstrap              # add --what-if to preview the infrastructure changes only
```

| Phase | What happens |
|---|---|
| A0 preflight | Checks Azure CLI, Bicep, the cloud, your sign-in against `operator_upn`, your roles, app registration rights, Fabric activation (when `deploy_fabric = true`), resource providers, VM image and size, an existing resource group's tag and region, model quota, and Defender for Servers Plan 2 (when `vm_access = "jit"`). The preflight starts missing resource provider registrations and waits up to 15 minutes. `Microsoft.DevTestLab` is needed only for the VM's auto-shutdown schedule. |
| A1 infrastructure | Shows what will be created, the cost and licensing notes, and asks you to type `yes` (`--yes` skips this). Deploys `infra/main.bicep` at subscription scope as deployment `<prefix>-mvp`. When it creates the VM, it **prints the administrator password once**; it is stored nowhere. A paused Fabric capacity is resumed before the deployment, because Azure cannot update it while paused, and the capacity is paused again afterwards. |
| A2 developer VM | Through Run Command: installs pinned versions of Git, Python 3.14, Azure CLI, Azure Developer CLI and Bicep (each download must match its SHA-256 in `dbp_mvp/devbox-setup.ps1` and carry a valid signature), clones the repository to `C:\battery-passport`, writes the settings and creates `.venv` with the requirements. Tools already on the VM are kept. The log is `C:\ProgramData\battery-passport\devbox-setup.log` on the VM. |
| A3 access | Makes sure the VM runs and RDP is allowed from `client_ip`, then prints the address, user `mvpadmin` and the next steps. |

Stage A takes a while; the progress lines show which step is running.

The monitoring template creates three `Microsoft.Insights/scheduledQueryRules` even when `enable_alerts = false`; that setting disables evaluation, not resource creation. A0 registers the required resource providers, including `Microsoft.AlertsManagement`.

## 4. Stage B on the developer VM

Connect with Remote Desktop to the address from A3, user `mvpadmin`. In PowerShell:

```powershell
az login --tenant <tenant ID>            # the same user as operator_upn
cd C:\battery-passport                   # or the subfolder A3 printed
.\.venv\Scripts\python -m dbp_mvp deploy
```

**Foundry portal:** open it in a browser running inside the developer VM. If Agents shows "Error loading your agents" despite private connectivity, check the browser's [Local network access permission](#foundry-portal-local-network-access-permission); keep Foundry public network access disabled.

| Phase | What happens |
|---|---|
| B1 identity | Registers the app `<prefix>-passport` in Microsoft Entra ID with its roles, creates its service principal, makes you an owner of both and gives you `Passport.Operator`, `Passport.Reviewer`, `Passport.Connector` and `Passport.Compliance`. |
| B2 database | Creates the fresh schema, including aftersales receipts, warranty, service and campaign records, applies per-table grants and anchors a first ledger digest. Earlier schemas are refused, not migrated. For an intentionally disposable existing environment use the confirmed `data clean` procedure. |
| B3 app | Before building, deletes previous `passport-app` images from ACR, then builds with ACR Tasks (or uses `--image`, or the last image with `--skip-build`, without cleanup) and deploys the Container App with sign-in. Sets `DBP_PARTY_REGISTRY_B64` from the series' party registry (public keys only; the signing keys stay in `output/keys/` on the VM). Creates the sign-in secret, valid for 30 days. |
| B4 agents | Creates the Durable Task Scheduler/task hub with a private endpoint in the endpoints subnet and public network access disabled, and grants the app managed identity Durable Task Data Contributor; agent mode requires one running app replica for its workflow worker. Checks Foundry's private access and model versions, creates or updates the two hosted and three prompt agents, gives each hosted agent its `Passport.Agent.Supplier` or `Passport.Agent.Lifecycle` role, adds their client IDs to the app's sign-in allowlist and switches the app to agent mode. Application events then enqueue durable Supplier, Lifecycle and weekly Watch triggers. See [Agents and process](agents-and-process.md#31-deployment-creates-the-agents). |
| B5 synthetic data | Generates the synthetic series, prints the expected agent calls, uploads the deliveries to the ingest lakehouse and builds the passport package in Fabric with `supply_flow` (resuming and pausing the capacity). Fabric's package must pass the independent package check and match the local build: data files byte for byte, the manifest apart from its completion time. Otherwise the local package is used and the next `deploy` tries Fabric again. |
| B6 import | Before B6 or B7 uses the app, `deploy` checks the expected image and waits for its revision to be ready and receive 100% of ingress traffic, even on a resumed deployment. B6 imports the production package (`dbp-passport-package-2`), evidence and connector messages. With Fabric enabled, runs the separate `aftersales_flow` for the imported series, validates the Gold package, uploads its bytes to app intake and checks the SQL import receipt. A failed aftersales run stops B6; it never substitutes local data. With Fabric explicitly disabled the production story still works, but no deployed warranty or recall feed is imported. |
| B7 checks | Private name resolution and network posture, app readiness, the SQL ledger against the anchored digests, one call of the hosted Supplier agent and its `agent.run` event in Application Insights. |
| B8 summary | Pauses Fabric and prints the app address, your roles, the ledger and data results, agent versions, workbook links, the scaling mode, the sign-in secret's expiry and a reminder to stop the VM. |

**Assign the roles `deploy` does not give.** B1 declares `Passport.Admin` (Settings page), `Passport.PolicyApprover` (Watch guidance activation) and the passport viewer roles `Passport.OEM`, `Passport.Recycler` and `Passport.Authority` but assigns none of them. Assign, remove or list them with:

```powershell
.\.venv\Scripts\python -m dbp_mvp roles assign Passport.Admin                                   # you (the signed-in user)
.\.venv\Scripts\python -m dbp_mvp roles assign Passport.PolicyApprover --user <user-principal-name>
.\.venv\Scripts\python -m dbp_mvp roles assign Passport.OEM Passport.Fleet.AdventureWorks --user <user-principal-name>
.\.venv\Scripts\python -m dbp_mvp roles remove Passport.OEM Passport.Fleet.AdventureWorks --user <user-principal-name>
.\.venv\Scripts\python -m dbp_mvp roles list --user <user-principal-name>
```

Only roles this MVP declares can be assigned; assigning a role the user holds already changes nothing. The change applies at the user's next sign-in. You can also use Microsoft Entra admin center > App registrations > `<prefix>-passport` > Managed application in local directory > Users and groups.

`Passport.OEM` grants the OEM persona and second-life view. For warranty and recall, also assign exactly one fleet: `Passport.Fleet.AdventureWorks` or `Passport.Fleet.Fabrikam`. Neither a fleet alone nor both fleets together grant access to aftersales records. B1 declares these roles without assigning a customer to the deployer. User and group assignments belong in Entra, not in `mvp.toml`; `operator_upn` identifies the person running deployment, not an OEM customer.

**Optional tour roles.** B1 gives the operator Operator, Reviewer and Compliance. To enable the Policy Approver, OEM and Recycler personas for that same account, assign:

```powershell
.\.venv\Scripts\python -m dbp_mvp roles assign Passport.PolicyApprover
.\.venv\Scripts\python -m dbp_mvp roles assign Passport.OEM Passport.Fleet.AdventureWorks
.\.venv\Scripts\python -m dbp_mvp roles assign Passport.Recycler
```

`Passport.Admin` and `Passport.Authority` are optional for settings and inspection; neither has a tour. Refresh Entra sign-in after assignment. For tour behavior and least-privilege caveats, see the [scenario walkthrough](scenario-walkthrough.md#4-showing-the-executive-screens-on-the-deployed-app). These are broad permissions for an intentional walkthrough account, not recommended production separation of duties.

**Scheduler access:** the Durable Task Scheduler is reachable only through its private endpoint: public network access is disabled and the IP allowlist is empty. The app's workflow worker connects from the VNet with its managed identity and task-hub role. The scheduler dashboard therefore works only from a browser on the developer VM.

Then play the scenario: start `run story --tag <TAG>` on the VM and make the decisions in the app, as described in the [scenario walkthrough](scenario-walkthrough.md#before-you-start).

## 5. Reruns and changes

- **Staged deployment is resumable.** Each machine records finished phases and their inputs in `output/state.json`; a rerun skips phases whose inputs are unchanged and continues where it stopped. `--from <phase>` repeats a phase and every later one. A skipped A0 still confirms your sign-in; A3, B7 and B8 always run. The separate `app redeploy` command always builds a fresh image and runs no deployment phases.
- **After changing settings:** rerun `bootstrap` on your laptop (A2 copies the new settings to the VM), then `deploy` on the VM. Infrastructure settings such as `enable_nat_gateway` change A1. **For model prices only**, run `app prices [--file <path>]` on either machine; no bootstrap or Stage B rerun is needed ([Model prices](#model-prices)).
- **After changing only application code:** follow [app-only redeployment](#51-redeploy-only-the-application) to retain data and configuration. For broader code/configuration changes, commit and push to the branch the VM clones, `git pull` on the VM, then `deploy`. Changed app files rebuild the image; changed agent files or instructions create new agent versions.
- **The sign-in secret** expires 30 days after B3. A normal `deploy` renews it when fewer than seven days remain; `deploy --from B3` renews it at once.
- **Exit codes:** 0 success, 1 failure, 130 interrupted with Ctrl+C (rerun the same command to continue). Scenario acts also return 3 when they stop safely, for example without a confirmation.

### 5.1 Redeploy only the application

Use **`.\.venv\Scripts\python -m dbp_mvp app redeploy`** to rebuild the application container and roll it out to the **existing** Container App, without resetting, regenerating, importing or refreshing data. This is the route for UI/application fixes on a running environment.

**Prerequisites:** Stage B has already deployed the app. Use the same developer VM checkout, `config/mvp.toml` and `output/state.json`; keep the rest of `output/`, including signing keys and scenario state. The signed-in operator needs access to list repositories, delete images and build in the existing ACR, and update the Container App. The command checks the configured subscription/user, saved environment and existing app before building. It requires the MVP's existing **Single** revision mode and `passport` container; it will not silently change a custom traffic/revision configuration.

1. Commit and push the application changes to the branch used by the VM. On the VM, open PowerShell in the MVP folder containing `app/`, `config/` and `dbp_mvp/` (use the path printed by A3):

   ```powershell
   cd C:\battery-passport                # or the MVP subfolder printed by A3
   git pull --ff-only
   ```

2. Confirm Azure CLI sign-in as `operator_upn` in `config/mvp.toml`, using that environment's tenant and subscription:

   ```powershell
   az login --tenant <tenant ID>
   az account set --subscription <subscription ID>
   ```

3. Build and redeploy **only the app**:

   ```powershell
   .\.venv\Scripts\python -m dbp_mvp app redeploy
   ```

The command prints the previous image, runs ACR Tasks against the current `app/` folder and its Dockerfile, and pushes a timestamp-tagged `passport-app` image to the existing registry. Docker does not need to be installed on the VM. It then uses `az containerapp update --container-name passport --image ...`, waits for the **new** revision to become ready, and reads `/health/ready` to confirm Azure SQL/Blob readiness. It records only the successful image change in B3's local outputs; it does not clear or rerun B1-B8. Open the unchanged app URL and refresh the browser to see the application changes.

**Image cleanup before every build:** the shared Python image builder first deletes the existing `passport-app` repository, including all its tags and manifests, then creates the new image. This removes accumulated images, including failed-attempt images and the currently deployed image, without touching other repositories. It applies to `app redeploy`, B3 builds during `deploy`, and the B3 rebuild in `data clean`. A first build with no repository proceeds normally. Skipped builds, `--image`, `--skip-build` and `app prices` do not delete images. Do not run builds concurrently or share this repository with another deployment that needs its images.

**What stays unchanged:** SQL database/schema/data and ledger digests; evidence and passport Blobs; Fabric data, pipelines, reports and capacity state; Foundry agents/models; Durable Task scheduler, task hub and queued work; network, identities and role assignments; Container App environment variables, secrets, sign-in configuration, resources and scaling. The sign-in secret is **not renewed**. New values in `config/mvp.toml`, Bicep changes and hosted-agent code are **not applied**. The app restarts on the new image, so its normal workers can resume pending work and normal telemetry continues; this is not a freeze of business activity.

**Do not substitute `deploy --from B3`:** that repeats B3 **and every later phase**, including data preparation/import and the Supplier smoke call. Plain `deploy`, including `--skip-build`, is also not an app-only operation. No `bootstrap`, `data reset`, `data clean`, `fabric run`, `report publish` or scenario command is needed here.

**Failure/retry:** cleanup errors stop the command before building or updating the app. Deleting images does not stop existing replicas, but the previous image is no longer available for a fresh pull, restart, scale-out or rollback. If the build or rollout fails after cleanup, availability is not guaranteed; fix the error and rerun `app redeploy` to build a replacement. If rollout/readiness fails, the command exits with an error rather than accepting the old healthy revision as success. Inspect **Container App > Revisions** and **Monitoring > Log stream**. A failed attempt can leave a new ACR image, which the next build deletes, or a Container App revision, which is not automatically deleted. This path does not migrate or repair an incompatible SQL schema: if the new code needs a schema change, choose a separate, explicitly planned maintenance/reset procedure rather than expecting app-only deployment to change data.

## 6. Day-to-day commands

| Command | Where | Purpose |
|---|---|---|
| `vm start`, `vm stop` | Laptop | Start or deallocate the VM. |
| `vm access [--ip <IPv4>]` | Laptop | Allow RDP from a new address (default `client_ip`), or request just-in-time access. |
| `vm reset-password` | Laptop | Set and show a new administrator password. |
| `status` | Either | Recorded phases, VM power state and Fabric capacity state. |
| `app redeploy` | VM | Rebuild `app/` with ACR Tasks and update only the existing Container App image; preserve data, settings and other infrastructure. See [the app-only procedure](#51-redeploy-only-the-application). |
| `app prices [--file <path>]` | Laptop or VM | Validate and apply model token prices to the existing app, using the same image; no build, VM update or deployment phases. Edit the JSON and rerun to refresh prices. See [Model prices](#model-prices). |
| `fabric check` | VM | Read-only: the capacity, its Fabric ID and the workspace. |
| `fabric run supply [--new-build]`, `fabric run aftersales`, `fabric run status [--export-id]` | VM | Run the named flow and pause the capacity afterward. `aftersales` also imports the verified Gold result for the prepared MAIN series into the existing app. |
| `report publish`, `powerbi` | VM | Publish the current app export and CSO report to Fabric. See [Publish and open Power BI reports](#66-publish-and-open-power-bi-reports). |
| `ledger anchor`, `ledger verify` | VM | Store a new ledger digest; verify the database against all digests stored for it. |
| `run story --tag <TAG>` | VM | Start a separate scenario series in the existing database and play its story while people decide in the app; preserves earlier data and infrastructure. See [the fresh-run recipe](#62-start-the-story-again-without-recreating-resources). |
| `data reset [--yes]` | VM | **Delete and recreate the SQL database**, then repeat B2 and B6. Not an in-place clear or an all-store purge. Asks you to type the database name (`--yes` skips this). See [the exact reset scope](#64-what-data-reset-deletes-and-keeps). |
| `data clean [--yes]` | VM | Coordinated reset of active generated app, SQL, Fabric and workflow data while keeping infrastructure, then rebuilds and imports the main-series package. Asks you to type `<prefix>-data-clean`; `--yes` skips that confirmation. See [the full clean-reset procedure](#65-clean-generated-data-for-a-fresh-run). |
| `scenario evaluation [--apply [--shield]]`, `scenario credential_negatives [--apply]` | Anywhere; VM with `--apply` | Optional scenarios; without `--apply` they run locally and need no settings. With `--apply` each needs its `[scenarios]` setting. `--shield` screens the Prompt Shields probes instead of calling the models. See the [scenario walkthrough](scenario-walkthrough.md#8-optional-evaluating-the-agent-instructions). |

**Ledger digests** are stored under `manual/<server>/<database>/<incarnation>/` in the digest storage account. The incarnation is the database's `create_date` from `sys.databases` in UTC, written `YYYYMMDDTHHMMSSZ`. A recreated database therefore starts a new prefix: the app, `ledger verify` and B7 read only the current one, and the old digests stay where they are.

### 6.1 Choose a fresh run, a new build or a database rebuild

**To start the story again without recreating the SQL database or any infrastructure, use a new `--tag`, not `data reset`.** This creates fresh scenario identities alongside the previous run. It does **not** delete previous data.

| Your goal | Supported route | What happens to earlier data |
|---|---|---|
| Replay the five-battery story with fresh batteries, lots, reviews and agent tasks, keeping the database | `run story --tag R2` (or `--tag R2` on every single act) | Retained; select the new series on the executive screen. |
| Run Fabric's supplier pipeline again with a new delivery/package build | `fabric run supply --new-build`, then `deploy --from B6` | Retained. The main series' battery and lot IDs are unchanged; this is not a fresh scenario. |
| Replace the SQL schema or restart the main series in a newly created database | `data reset`, only when database recreation is acceptable | The old SQL database is deleted; other stores are not cleared. B2/B6 recreate and seed it. |
| Clear active generated app, SQL, Fabric and workflow data before a clean main-series run, while retaining infrastructure | `data clean` | Recreates the SQL database and Durable Task hub, clears current generated Blob/Fabric data and local run artifacts, then redeploys and reseeds. Retained Azure history is not erased. |

**Why not `DELETE` or `TRUNCATE`?** The [schema](../app/sql/schema.sql) deliberately uses append-only ledger tables for packages, evidence, passport versions, agent records and audit events, and updatable ledger tables with retained history for current state and reviews. Azure SQL does not support deleting append-only rows or ledger history, or truncating ledger tables. Dropping a ledger table only logically removes it; its data remains for verification. See Microsoft's [ledger limitations](https://learn.microsoft.com/en-us/sql/relational-databases/security/ledger/ledger-limits?view=azuresqldb-current). A UI filter or dropping/recreating tables would therefore not satisfy "delete all data".

### 6.2 Start the story again without recreating resources

For another walkthrough without rebuilding infrastructure or recreating SQL, follow the [tagged-series procedure](scenario-walkthrough.md#5-a-fresh-series-with---tag). A tag keeps the prior series and ledger history; it is not a data reset or a security boundary. Tagged package preparation runs locally on the VM rather than through Fabric's `supply_flow`.

Finish deployment and resolve any B7 agent failure first; a new tag will not fix a broken agent. Preserve the VM's `config/mvp.toml`, `output/state.json`, `output/series/` and especially `output/keys/`. Deleting local output does not clear cloud data, and replacing signing keys can break message verification. If Fabric reporting is enabled, remember that `report publish` exports rows across all series, not just the selected tag.

### 6.3 Rerun Fabric supply without resetting the scenario

For a **new Fabric build of the main series**, with `deploy_fabric = true`, run on the VM:

```powershell
.\.venv\Scripts\python -m dbp_mvp fabric run supply --new-build
.\.venv\Scripts\python -m dbp_mvp deploy --from B6
```

The first command prepares new delivery IDs, a later build sequence and a new package ID, runs the pipeline in the existing workspace, checks the package and pauses the capacity again. Continue to import only after it reports success; if Fabric fails, the command explicitly reports that only a local fallback is ready.

The second command imports the prepared package and continues the deployment checks. It is a normal resumable deployment: unchanged earlier phases are skipped, but changed code/settings or an expiring sign-in secret can still cause app deployment work. It does not drop the database.

**`--new-build` does not create a new scenario series.** With unchanged generator settings, the main batteries, lots and underlying synthetic facts remain the same. Their previous reviews, accepted claims, publication and lifecycle history are not erased; a closed passport does not become a fresh battery. The existing `fabric run supply` command has no `--tag` option. Use section 6.2 for fresh scenario identities, and do not describe that local tagged path as a Fabric execution.

### 6.4 What `data reset` deletes and keeps

**Do not use this command if keeping the current SQL database is a requirement.** It is a destructive database rebuild, principally for a schema change (`schema_mismatch`), not a general "clear all data" operation.

After you confirm the database name, `data reset` deletes the database and recreates it with the same name, tags and backup storage redundancy, using the paid serverless settings in section 2. B2 reinstalls the schema and grants and anchors a first digest; B6 then imports the prepared **main** package, opening evidence and signed claims. It does not leave an empty database or force a new supply build. Previously tagged series are not automatically reimported.

| Store or resource | Effect of `data reset` |
|---|---|
| SQL database | Replaced. Its old passports, reviews, compliance cases, connector/agent records, outbox, policy state and ledger history are removed from the live database; the main series is seeded again. |
| SQL logical server and other infrastructure | Kept: VM, network/private endpoints, Container App, identities/app registration, Foundry agents/models, storage accounts, Fabric capacity/workspace and monitoring resources. |
| App Blob storage | Existing intake packages, evidence files, passport snapshot blobs and blob versions are **not cleared**. B6 may reuse intake files and writes newly seeded evidence. |
| Ledger digest Blob storage | Old digests remain under the previous database-incarnation prefix; new verification uses the new prefix. |
| Fabric/OneLake and Power BI | Existing deliveries, lakehouse data, export files and report definitions are **not reset**. Reporting can remain stale until `report publish`. |
| Durable Task Scheduler and Foundry runtime state | Task-hub instances/history and hosted sessions/responses are **not purged** by this command. |
| Application Insights / Log Analytics | Existing events, traces and metrics remain subject to their retention policies. |
| VM-local files | Settings, signing keys, prepared series/builds and evidence registers remain. Only phase records B2/B6/B7 are invalidated; B2/B6 are then recorded again. |

The command does not stop app/workflow workers or cancel in-flight scheduler work. Do not run it during active scenario, pipeline or workflow execution, or treat it as a coordinated reset of the whole system. It also makes no secure-erasure guarantee for retained backups or copies.

**Only if database recreation is acceptable**, save any wanted evidence and use the explicit confirmation rather than `--yes`:

```powershell
.\.venv\Scripts\python -m dbp_mvp data reset
.\.venv\Scripts\python -m dbp_mvp deploy --from B7
```

After successful checks, `run story` without a tag starts the reseeded main story. For the no-database-recreation requirement, return to section 6.2 instead.

### 6.5 Clean generated data for a fresh run

Use `data clean` when you want to keep the environment but remove the **active generated dataset and workflow state** before preparing the main story again. It replaces the SQL database, so use it only when that destructive database recreation is acceptable. It does not tear down or reprovision the VM, network, Container Apps environment, identities, Foundry account/models/agents, storage accounts, SQL server, Fabric capacity or workspace, or monitoring resources.

Run on the developer VM, from the MVP folder, signed in as the configured operator. Stop scenario work first; the command takes the app offline while it clears data. It refuses to clear an unmarked resource group, a workspace/lakehouse it cannot verify as MVP-owned, or Fabric data while a pipeline is running.

```powershell
.\.venv\Scripts\python -m dbp_mvp data clean
```

Type the exact `<prefix>-data-clean` value shown by the command. `--yes` bypasses this confirmation and should be used only in an explicitly controlled script. The operation:

1. Deactivates active app revisions, replaces the Durable Task task hub, and clears current objects, snapshots, and versions from the generated `evidence`, `passports`, and `intake` Blob containers. The app `settings` and ledger-digest containers are not cleared. The operator's standing Storage Blob Data Contributor role cannot delete previous Blob versions, so when versions remain the command temporarily grants **Storage Blob Data Owner** on the app storage account. It waits up to ten minutes for the grant to take effect and removes it afterward. If removal fails, the command prints the `az role assignment delete` command to run; the next `data clean` also attempts removal.
2. If the deployment has a Fabric capacity, verifies workspace ownership, resumes the capacity and waits for `Active` before listing the five lakehouses. It checks the lakehouse ownership markers and refuses to clear data while a pipeline is running. It removes generated deliveries, packages, reporting exports and aftersales folders, and empties the known MVP Delta tables. Workspace items and reports remain. The command attempts to pause the capacity again even if a check or the cleanup fails.
3. Removes local generated `output/series`, `output/runs`, `output/exports`, `output/evaluation` and `output/credential_negatives` folders. It keeps `config/mvp.toml`, `output/state.json`, `output/keys/` and the app settings. Previously used tags can be reused. On `/exec`, a browser that remembers a removed series selects the default with a notice and ends that series' tour; an explicit `?series=` link to removed data still reports it as unavailable.
4. Recreates SQL, then reruns deployment from B2 through B8 with a new main-series package, imports it and refreshes Fabric reporting when enabled. B3 builds an app image from the current checkout and B4 updates the agents, so after a `git pull`, `data clean` does not need a separate `app redeploy` or `deploy`. Changed settings or infrastructure still require `bootstrap` on your laptop first ([section 5](#5-reruns-and-changes)). The operation runs app-readiness and ledger checks. Start the interactive story separately:

   ```powershell
   .\.venv\Scripts\python -m dbp_mvp run story
   ```

   Wait for `data clean` to exit successfully with **Clean reset complete** before starting the story. B6 must have imported the fresh package and verified the signed claims, B7 must have passed app-readiness and ledger checks, and any enabled reporting refresh must have finished. The seeded dataset is the starting point, not the ready-to-show "2 of 5 published" checkpoint; `run story` prepares that checkpoint and the human decisions.

   Use `run story --tag CEO` instead if you want a named series alongside the fresh main series. The story waits while people decide in the app and anchors the ledger after publication and aftercare. To play the acts one at a time instead, use `run prepare`, `run review`, `run complete` and `run aftercare`, then `ledger anchor` after new ledger activity.

The active data is cleared, but this is **not secure erasure**. Blob soft-deleted objects/versions and backups can remain under retention; Fabric Delta transaction/history files and pipeline-run history, SQL backups, Foundry service history and Azure Monitor/Application Insights logs are outside this purge. Do not claim that all copies or operational records were erased. Fabric and agent/model activity during redeployment may incur usage charges.

If a step fails or is interrupted, fix the reported issue and rerun `data clean` in the same environment, then type the confirmation again. `output/data-clean.json` records that an interrupted reset is in progress; the cleanup is designed to be repeated. The app may remain offline until the redeploy succeeds. If the command reports that only the reporting refresh failed after the reset completed, run `.\.venv\Scripts\python -m dbp_mvp report publish`; do not repeat the destructive reset just for reporting.

**`404 CapacityNotActive` while listing lakehouses:** Older versions listed lakehouses before resuming the capacity. This error does not mean the workspace is missing. If the clean stopped here, SQL recreation has not started, but the earlier app, task hub and Blob cleanup steps have already run. Use the updated VM checkout, keep `output/data-clean.json`, and rerun the confirmed `data clean` command above. It repeats cleanup from the beginning, not from the failed step. There is no need to resume Fabric manually or recreate the workspace.

**`Fabric PATCH .../notebooks/<id> failed: HTTP 500` during cleanup:** The older tool had finished updating a notebook definition and was recording its version marker in the notebook description. `Fabric capacity paused.` is the cleanup action after the error, not its cause. At this point the reset pipeline has not run, and Fabric file deletion and SQL recreation have not started.

The updated tool records the marker through the [Fabric item metadata API](https://learn.microsoft.com/en-us/rest/api/fabric/core/items/update-item). On a `500`, `502`, `503` or `504`, it reads the item back. If the marker was saved, it continues. If the old marker is still present, it retries only the metadata write, up to five attempts in total. If the read fails or the marker cannot be confirmed, cleanup stops before deleting Fabric data. Only the `reset_tables` notebook receives the aftersales reset setting. Notebooks changed by an older clean may be updated once to restore their ordinary definitions.

To recover, keep `output/data-clean.json` and rerun `data clean` from the updated VM checkout. The command repeats cleanup from the beginning. Do not delete the notebook or manually resume the capacity.

The existing `data reset` remains SQL-only and does not stop app/workflow workers or clear other stores. Use it for a schema rebuild, not as a substitute for this coordinated clean run. Neither command deletes the underlying infrastructure.

### 6.6 Publish and open Power BI reports

There are two separate reports with different data sources and deployment paths. The CSO report reads the app's minimized Fabric reporting export. The CFO report uses entirely synthetic data embedded in its own Power BI project and does not connect to the app or the Fabric finance environment.

#### CSO report, publish to the MVP Fabric workspace

Requirements: `deploy_fabric = true`, an existing deployed Fabric workspace and reporting lakehouse, and a complete app status export. Open the VM terminal in the MVP folder and sign in as the configured operator. Set `powerbi = true` in `config/mvp.toml` **before** running `report publish` if you want that command to publish the semantic model and report as well as refresh the export and SQL views.

To publish the latest app snapshot and report together:

```powershell
.\.venv\Scripts\python -m dbp_mvp report publish
```

If `powerbi = false`, publish or update the model/report separately after at least one successful status export:

```powershell
.\.venv\Scripts\python -m dbp_mvp powerbi
```

`report publish` fetches the app's current status export, writes it to the reporting lakehouse, runs `status_flow`, updates SQL views, and, when enabled, publishes the Direct Lake semantic model `passport-measures` and its six-page CSO report. `powerbi` requires an existing complete export, then ensures the same model and report are up to date. Both commands activate the existing Fabric capacity for the operation and attempt to pause it afterward. Check warnings if pausing fails. They do not publish the separate CFO project. If the capacity is paused, resume it in Fabric before opening the report. On F2, viewers need Power BI Pro or Premium Per User licensing and workspace/report access.

The report represents the latest loaded export, not live app telemetry. After new decisions or a new scenario run, repeat `report publish` to update the snapshot. Rows from all app series are included. Check the export ID and generation time on **Trust and gaps** before presenting values.

#### CFO report, open locally or publish from Power BI Desktop

The CFO project is `fabric\powerbi\finance-synthetic.pbip`. Its import model embeds the synthetic contract portfolio, costs, budget, cash flows, scenarios and sensitivities generated by `data\finance.py`. It requires no app status export, Fabric lakehouse, SQL endpoint, cloud credentials or Azure resource deployment to open locally.

1. Install a current Power BI Desktop release with PBIP and TMDL support. Keep the `.pbip` file, `finance-synthetic-report` folder and `finance-synthetic` model folder together.
2. Open `fabric\powerbi\finance-synthetic.pbip` in Power BI Desktop. Choose **Refresh** to load the embedded tables. If Desktop asks for project or TMDL support, update Desktop or enable the required project options.
3. For local use, no publish step is required. In Desktop authoring mode, Ctrl-click the page-navigation buttons. Review the report's synthetic labels and the **Assumptions and risk** page.
4. To share, sign in to Desktop, choose **Publish**, select a workspace for which you have permission, and complete the publish. Follow the prompts to confirm or replace existing items. Check service refresh, report rendering, viewer permissions and licensing before distributing it.

The CFO report's numbers are fictional and unvalidated, not actual contract records, cloud prices, approved budget, expected losses or realized returns. Contract exposure and the liquidity proxy are not investment benefits. The downside scenario has negative NPV. Confirm every assumption with its proposed owner before treating the model as a funding case. Financial figures, native Power BI refresh/rendering and tenant deployment have not been verified against a live Power BI tenant.

The [Power BI authoring guide](../fabric/powerbi/README.md) describes the generated project, page design, source boundaries and offline HTML design proof. That proof is not a Power BI rendering.

## 7. Troubleshooting

| Symptom | What to do |
|---|---|
| Preflight lists unmet prerequisites | Fix each listed item; the messages name the role, provider, quota or region problem. |
| A1: the subscription is not registered to use namespace `Microsoft.AlertsManagement` | Update the checkout and run `python -m dbp_mvp bootstrap --from A0`. A0 registers the alert-management dependency even when alerts are disabled. `--from A1` alone skips a previously completed A0 and will not register it. |
| A3: `nsg rule ... AllowRdpFromClient not found` | Something outside the template, such as Defender just-in-time access or a subscription policy, removed the RDP rule after A1. Update the checkout and rerun `python -m dbp_mvp bootstrap --from A3`; A3 now recreates the rule with the template's values. If it disappears again, check the NSG's activity log for the identity that removes it, or switch to `vm_access = "jit"`. |
| Supplier runs fail with a Content Safety `401` or `403`, or `scenario evaluation --apply --shield` reports errors, after updating to the Prompt Shields version | The Cognitive Services User role on the Foundry account is granted by the template, not by `deploy`. On the laptop, run `python -m dbp_mvp bootstrap --from A1` with the updated checkout, wait a few minutes for the role to take effect, then rerun the command on the VM. |
| A1: `ProvisioningDisabled: Cannot update paid database to free database` | Keep the existing paid database. On the laptop, use the updated checkout, remove `use_free_sql_offer` from `config/mvp.toml` if present, and run `python -m dbp_mvp bootstrap --from A1`. No database deletion, data reset or Fabric rebuild is needed for this correction. |
| `Deployment <prefix>-mvp is still running` | Wait for it to finish (Azure portal > Subscriptions > Deployments), then rerun. |
| A1: `Another operation is pending` for a private DNS VNet link (`Conflict`) | Wait for the operation to finish. Do not delete the link or launch another bootstrap concurrently. See [DNS recovery](#dns-recovery-and-a-clean-restart). |
| A1: `SubscriptionNotRegisteredForFeature` ... `Microsoft.Network/AllowBringYourOwnPublicIpAddress` | The template brings no own addresses, but some subscriptions gate Azure-allocated public IPs on this feature too. With the subscription owner's agreement run `az feature register --namespace Microsoft.Network --name AllowBringYourOwnPublicIpAddress`; it stays `Pending` until Microsoft approves it (open a support request with the error's correlation ID if it does not, or if the error persists once `Registered`). Then `az provider register --namespace Microsoft.Network` and rerun the bootstrap. Created resources are kept. |
| A1: the Fabric capacity fails with "Tenant ... wasn't recognized by Microsoft Fabric. Sign up for Microsoft Fabric" | Fabric has not been activated; do the [manual step](#3-stage-a-on-your-laptop) and rerun the bootstrap, or set `deploy_fabric = false` and rerun. The preflight normally catches this first. |
| A1 rerun: the Fabric capacity fails with `BadRequest: Service is not ready to be updated` | Azure cannot update a paused capacity, and A1 pauses it after every run. The current version resumes a paused capacity before A1 and pauses it again afterwards, even when A1 fails. With an older checkout, resume the capacity in the Azure portal first, then rerun `bootstrap --from A1`. |
| A1: `RoleAssignmentExists: The role assignment already exists` | The same role, identity and scope was already granted outside the template, for example by hand in the portal, under another assignment ID. The bootstrap prints the lookup command. If the existing assignment matches, delete it with `az role assignment delete --ids <id>` and rerun `bootstrap --from A1`; the template grants it again under its own ID. |
| A2: the setup script did not report a result, or tool installation failed | Sign in to the VM, read `C:\ProgramData\battery-passport\devbox-setup.log`, then `bootstrap --from A2`. |
| A3: the VM could not clone the repository | A private repository or no HTTPS `origin`. Follow the printed commands on the VM: clone manually to `C:\battery-passport`, copy `C:\ProgramData\battery-passport\mvp.toml` to `config\mvp.toml` and create `.venv`. |
| RDP does not connect | Your address changed: `vm access --ip <new address>` on the laptop, and set the same `client_ip` in `config/mvp.toml` (a bootstrap rerun resets access to `client_ip`). The VM may also be shut down: `vm start`. |
| Foundry portal: "Error loading your agents. Your request for data was not sent" from the developer VM | Allow `https://ai.azure.com` in the browser's **Site permissions > Local network access**, then restart the browser. See [the steps below](#foundry-portal-local-network-access-permission). This browser permission is separate from agent outbound networking; do not enable Foundry public access or add NAT to fix it. |
| B3: the local log stream fails during an Azure Container Registry build | Check the remote ACR run status and output image. If the remote build succeeded, resume with `python -m dbp_mvp deploy --image <verified-image>` instead of rebuilding it. Use an image from this MVP's registry, preferably pinned by digest. |
| B4: hosted agent provisioning fails | Check the failed agent version's status and error in the Foundry portal. Provisioning failures occur before runtime container logs are available. See [hosted agent failures](#hosted-agent-build-failures-and-logs). |
| B1 or B4: `az rest --method patch` (or `post`) fails with `Authorization_RequestDenied`, "Insufficient privileges to complete the operation" | Your directory role is missing or has expired. Activate Cloud Application Administrator (or Application Administrator or Global Administrator), sign in again, and rerun the failed phase. See [Microsoft Entra roles for stage B](#microsoft-entra-roles-for-stage-b). Network settings are not involved. |
| B7: private name resolution or network posture problems | Run `deploy` on the VM, not on the laptop: the private names resolve only inside the network. For posture findings, compare the listed resource with the [network design](architecture.md#2-network-and-trust-boundaries) and rerun `bootstrap --from A1` to restore the template's settings. |
| B7: the Supplier check on `LOT-GR-A` answers `422 grounding_required` | Check which Container App revision served `POST /agents/supplier/run`. If the updated image is already deployed and now receives all traffic, run `python -m dbp_mvp deploy --from B7` on the VM. No data reset or image rebuild is needed for a rollout timing failure. See [Hosted-agent runtime failures](#hosted-agent-runtime-failures) for the other cases. |
| B7: the hosted Supplier agent call fails, including `Expected ResponseStream for streaming, got AgentResponse` | See [Hosted-agent runtime failures](#hosted-agent-runtime-failures). The stream-type error is a Python middleware defect, not evidence of blocked egress. Update the checkout and run `deploy --from B3` without `--skip-build` to replace the app diagnostics and hosted code. Inspect other failures' provider errors/session logs before changing networking. |
| A model call reports `Model deployment rate limit exceeded`, sometimes wrapped as a hosted `server_error` | Check the actual model deployment's RPM/TPM and apply the [model capacity settings](#model-changes-and-rate-limits), not just an app restart. Tool turns and all agents sharing a model consume its allowance. After the limit is corrected, use a fresh task; an already failed task replays its recorded failure. |
| A Supplier task fails with `Hosted response completed without answer text`, or on an older app image with `JSONDecodeError: Expecting value: line 1 column 1 (char 0)` | No answer text was available to record as a decision. Inspect the provider error and response metadata before changing settings. Confirm model compatibility through the project's Responses endpoint; see [Hosted-agent runtime failures](#hosted-agent-runtime-failures). Apply the configured default with `deploy` on the VM, then rerun the scenario. |
| `data reset` fails in B6 with `The connector agreement AGR-<party> answered 422: unknown_party The sender must be a party in the registry.` | The older app image remembered that it had copied the party registry into SQL. Because `data reset` recreates SQL while the app stays running, that image did not copy the registry into the new database. Update the VM checkout and run `python -m dbp_mvp app redeploy`, then run `python -m dbp_mvp data reset` again. The reset replaces the database again, removing the recorded refusal, and reruns B2 and B6. Run `python -m dbp_mvp deploy --from B7` for the checks. Do not retry the agreement or reset before redeploying the app. |
| A prompt-agent call returns HTTP 400: `The 'agent' property is deprecated. Use 'agent_reference' instead.` | Update the checkout and rebuild the app with `python -m dbp_mvp app redeploy`. The shared prompt runtime now sends `agent_reference` on both the initial request and tool continuations. Increasing capacity or changing network access cannot fix a rejected request envelope. |
| After a clean, Supplier tasks show `no_issue` but batteries stay "Awaiting agent checks" with no Carbon or Issuance task | An older automatic workflow dropped the battery ID between steps. Update the VM checkout and run `python -m dbp_mvp app redeploy`; no hosted-agent redeployment or data reset is needed. Old workflows that already ended do not restart merely because the image changed: run `python -m dbp_mvp run story` for the existing MAIN series (or the same `--tag` for an existing named series). It resumes from recorded evidence and checks, and prepares the app's human decisions. The fixed workflow retains the battery context; failures before task creation are logged and fail the workflow rather than silently ending it. |
| B7/B8: no `agent.run` event in Application Insights | Ingestion can take minutes. Rerun `deploy --from B7` later; the agents workbook stays empty until then. |
| The Fabric capacity is not visible to you | Stage A makes `operator_upn` the capacity administrator. Check Capacity administrators in the Azure portal and that Fabric is enabled for your tenant. |
| Fabric fails during B5 | Stage B continues with the local package and records B5 as unfinished; the next `deploy` or `fabric run supply` tries again. If `gold_package` says the build does not match its deliveries, a delivery was rejected: its coded reason is in the bronze lakehouse's `deliveries` table. |
| The first page is slow | When agent mode is off, the app may have scaled to zero; `warm_replica = true` keeps one replica running. Agent mode already keeps one workflow-worker replica running, so this setting is not the remedy for a slow first page there. |
| The trail shows "Ledger chain verified, anchor pending" | Run `ledger anchor` on the VM after publishing. |
| `/health/ready` answers `schema_mismatch`, or B2 stops with "Recreate it: python -m dbp_mvp data reset" | The database holds another schema version. Run `data reset` on the VM. |

### Foundry portal: Local network access permission

The public portal page at `https://ai.azure.com` sends requests from your browser to the Foundry account's private endpoint. Edge or Chrome can block this public-site-to-private-address connection even when the browser runs inside the developer VM and private DNS and HTTPS connectivity work.

1. In the **VM's browser**, open **Settings > Site permissions > Local network access**. In some Edge versions, Site permissions is under **Privacy, search, and services**, followed by **All permissions**; Chrome has the corresponding site permission.
2. Allow **`https://ai.azure.com`** specifically, rather than all websites. If the setting is organization-managed, ask the administrator for this site-specific permission.
3. Restart the browser and reopen the project's **Agents** page, leaving Foundry public network access **Disabled**.

Do not disable browser security globally, enable Foundry access from all networks, or add a NAT Gateway for this browser-permission problem. The permission does not replace VNet connectivity, private DNS or Entra authorization. If access still fails, confirm that the private endpoint is **Approved**, the account hostname resolves to its private IP from the VM, and TCP port 443 is reachable; then inspect the browser's F12 Network/Console errors.

For background, see Microsoft's explanation of [Foundry portal access through browser Local Network Access](https://techcommunity.microsoft.com/blog/azure-ai-foundry-blog/it-wasnt-the-private-endpoint-why-the-foundry-playground-breaks-behind-private-n/4552771).

### Hosted-agent build failures and logs

**Provisioning failures occur before an agent container starts.** Inspect the failed agent version's status and error in the Foundry portal. Container log streaming is available only after an invocation creates a session.

The deployment packages the hosted agent code as a ZIP and uploads it to Foundry. The ZIP root must contain the agent entry point, its dependency manifest, and required package files. The hosted dependency manifest is [`agents/hosted/requirements.txt`](../agents/hosted/requirements.txt), not the separate requirements file used to prepare the VM. The repository does not need to be public.

After correcting the reported cause, update the VM checkout and resume from B4. Preserve `config/mvp.toml` and `output/state.json`:

```powershell
git pull --ff-only
.\.venv\Scripts\python -m dbp_mvp deploy --from B4
```

If B4 stops on a hosted agent error, resolve that error before retrying. Do not change network settings or make the repository public unless the diagnostic identifies a need to do so. Foundry can deduplicate identical ZIP and definition submissions, so repeating an unchanged upload does not guarantee a fresh build; record the version number that the command returns.

**Read back a failed version.** Run this in PowerShell on the developer VM, from the MVP folder, after `az login`. Change `AGENT` and `VERSION` to the values in the failure message. The script reads the configured deployment's endpoint, the version, and its uploaded code. It does not change Azure, extract or run the code, or print tokens or environment settings.

```powershell
@'
import hashlib
import io
import json
import stat
import zipfile
from importlib.metadata import version as package_version
from dbp_mvp import foundry
from dbp_mvp.az import Azure
from dbp_mvp.config import CONFIG, load

AGENT, VERSION = "dbp-supplier", "2"
config = load(CONFIG)
az = Azure(config.subscription_id)
endpoint = az("deployment", "sub", "show", "--name", config.names.deployment,
              "--query", "properties.outputs.foundryProjectEndpoint.value")
print("SDK:", package_version("azure-ai-projects"))
with foundry.project_client(endpoint) as project:
    version = project.agents.get_version(agent_name=AGENT, agent_version=VERSION)
    print("Agent:", AGENT, "version:", VERSION, "status:", version.status)
    print("Error:", json.dumps(version.get("error"), indent=2))
    code = b"".join(project.agents.download_code(AGENT, agent_version=VERSION))

print("Downloaded ZIP SHA256:", hashlib.sha256(code).hexdigest())
print("Matches this checkout:", code == foundry.package()[0])
with zipfile.ZipFile(io.BytesIO(code)) as archive:
    for entry in archive.infolist():
        mode = entry.external_attr >> 16
        print(entry.filename, "bytes:", entry.file_size, "mode:", oct(mode),
              "regular:", stat.S_ISREG(mode))
    print("CRC failure:", archive.testzip())
    print("Nonempty root requirements.txt:",
          "requirements.txt" in archive.namelist() and bool(archive.read("requirements.txt").strip()))
'@ | .\.venv\Scripts\python -
```

A ZIP from an older checkout need not match the current checkout. If an upload still reports a missing manifest despite a nonempty root manifest and a valid CRC, stop repeating identical retries. Keep the agent and version, UTC time, region, full error, ZIP hash, and entry listing for Azure Support. Do not include tokens or full environment settings. A present manifest does not establish that its dependencies will install or that the agent will start.

**Why only one agent?** B4 creates the agents in sequence: Supplier (hosted), Carbon (prompt), Issuance (prompt), Lifecycle (hosted), and Watch (prompt). It waits for each hosted build before granting that agent its app role. A Supplier failure therefore stops B4 before the remaining agents are created. A successful retry continues through all five; it does not skip the failed agent.

**Runtime logs come later.** After an invocation creates a container session, use its `x-agent-session-id` and the documented [container log stream](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/deploy-hosted-agent-code#stream-container-logs), including for `session_not_ready` failures. The app's `agent.run` events in Application Insights describe invocation outcomes; they are not a replacement for the version's provisioning error.

### Hosted-agent runtime failures

**Supplier returns `422 grounding_required` in B7:** Container Apps can finish the ARM deployment while the previous revision still serves the shared app URL. Even a successful `/health/ready` at that URL does not identify the revision. In **Container App > Logs**, match the timestamp of `POST /agents/supplier/run` to `RevisionName_s` in `ContainerAppConsoleLogs_CL`. In **Revisions**, check which revision now has the expected image and 100% of traffic.

- **The previous revision answered.** If the fixed image is now serving, rerun `.\.venv\Scripts\python -m dbp_mvp deploy --from B7` from the updated VM checkout. A failed check advances the smoke task ID; the old result stays in the audit history. The updated deployment script waits for the traffic switch before sending imports or smoke tasks.
- **The deployed image predates attempt tracking.** Its Operator route did not register an attempt, so neither `evidence` nor `reviews` could be retrieved. Update the VM checkout and run `.\.venv\Scripts\python -m dbp_mvp deploy` without `--skip-build` or `--image` to build and deploy the fix.
- **The fixed revision answered `Missing on this attempt: ...`.** Inspect the task's `agent_attempt_started` and `agent_tool_read` audit events and the two callback HTTP statuses. Both `evidence` and `reviews` must be read under the same attempt, even when `reviews` is empty. A genuine missing read remains a failure. None of these cases calls for resetting data or changing the model.

**`Expected ResponseStream for streaming, got AgentResponse`:** this is a runtime stream-type error, not evidence of blocked networking. The current [`hosted runtime`](../agents/hosted/main.py) applies its **115-second deadline outside agent middleware**, preserving the SDK response-stream contract, finalization, and cleanup. Its manifest pins `agent-framework-core==1.26.0`. Supplier and Lifecycle share this source, so a source update affects both hosted agents.

To replace older app diagnostics and hosted code, update the VM checkout and run from the MVP folder:

```powershell
git pull --ff-only
.\.venv\Scripts\python -m dbp_mvp deploy --from B3
```

Keep `config/mvp.toml` and `output/state.json`. B3 rebuilds the app image with improved diagnostics; B4 uploads the changed hosted source and reuses matching prompt agents, then the command continues to B7. **Do not use `--skip-build` or an old `--image`** for this recovery. No Stage A redeployment, NAT Gateway, public Foundry access, agent deletion or data reset is required for this error. Keep the [Stage B directory permissions](#microsoft-entra-roles-for-stage-b) active for any renewed identity grants.

**If the next invocation fails:** the app retains the provider error code/message (or incomplete reason) plus the response, session, and request IDs when supplied. It does not dump full responses, task contents, or authorization headers. The app API limits its error message to 400 characters; use **Container App > Monitoring > Log stream** and the printed smoke task ID for the full exception. Use the `x-agent-session-id`/`session_id` with the deployed agent version and Microsoft's [container log stream](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/deploy-hosted-agent-code#stream-container-logs) for the hosted traceback. Session IDs are not task IDs or response IDs. Review diagnostic logs for sensitive content before sharing them.

Only if those diagnostics establish blocked outbound access from the agents subnet should you consider `enable_nat_gateway = true`, followed by `bootstrap --from A1` on the laptop and `deploy --from B4` on the VM. A Python exception or authorization refusal requires its own correction; changing network topology does not fix either.

**Supplier replies without answer text:** inspect `Hosted response completed without answer text (output items: ...; output tokens: ...)` and the response, session, and request IDs. Older images can surface this as a JSON parse error instead. A deployment that answers through an account-level OpenAI endpoint is not necessarily compatible with the Foundry project's Responses path. Validate the configured model through the actual agent path before adopting it; do not infer a general model defect from one endpoint failure.

The default Supplier model is `gpt-6.1-sol`. To apply that assignment, run `deploy` on the VM: B3 rebuilds the app, B4 updates changed hosted settings, and B7 makes a Supplier smoke call. Then rerun the scenario, which can create a fresh task after a recorded failure. Reusing the exact failed task ID still returns the stored failure. See the [model selection notes](agents-and-process.md#36-model-selection-and-request-capacity) before changing assignments. A successful smoke call establishes connectivity and one response, not model quality or end-to-end scenario success.

### Microsoft Entra roles for stage B

**Global Administrator is not required.** Prefer a time-limited **Cloud Application Administrator** activation for this deployment. Application Administrator also works but includes additional Application Proxy management rights. These are deployment permissions, not roles that the running agents need. A custom directory role with the necessary permissions is another possible route, but its permission/consent-policy configuration needs tenant-specific review; this MVP does not provision or validate that configuration.

Stage B changes the `<prefix>-passport` app registration through Microsoft Graph using your sign-in. Two kinds of permission are involved:

- **Changing the app** (its manifest in B1, the agent app roles in B4, the sign-in secret in B3, and deletion at teardown) is allowed to the app's **owners**. B1 makes you an owner of the app and its service principal.
- **Granting a hosted agent its `Passport.Agent.*` role** (B4, every time an agent identity is new) assigns an application permission. Microsoft Graph requires a supported directory role for this, such as **Cloud Application Administrator**, **Application Administrator** or **Global Administrator** (the preflight and error messages name these three); ownership is not enough. Cloud Application Administrator is the least privileged of the three.
- **Adding you as owner** (B1) itself needs Cloud Application Administrator or Application Administrator (or Global Administrator) when you are not yet an owner.

See Microsoft Graph's documentation for [granting application permissions](https://learn.microsoft.com/en-us/graph/api/serviceprincipal-post-approleassignments?view=graph-rest-1.0) and [adding an application owner](https://learn.microsoft.com/en-us/graph/api/application-post-owners?view=graph-rest-1.0).

When B1 or B4 fails with `Authorization_RequestDenied`:

1. Check your active directory roles and the app's owners on the VM:

   ```powershell
   az rest --method get --url "https://graph.microsoft.com/v1.0/me/transitiveMemberOf/microsoft.graph.directoryRole" --query "value[].displayName"
   az ad app owner list --id <app client ID> --query "[].userPrincipalName"
   ```

2. Activate one of the three roles, for example in Privileged Identity Management (Microsoft Entra admin center > Identity governance > Privileged Identity Management > My roles). Global Reader is not sufficient.
3. Sign in again so your token carries the role: `az logout`, then `az login --tenant <tenant ID>` as `operator_upn`. Reselect the configured subscription with `az account set --subscription <subscription ID>` and repeat the role readback above. Proceed only after sign-in succeeds and the activated role appears.
4. If B1 failed, rerun `.\.venv\Scripts\python -m dbp_mvp deploy --from B1`. If B1 completed and B4 failed, resume with `.\.venv\Scripts\python -m dbp_mvp deploy --from B4`. Keep the role active through stage B, including Lifecycle's grant, then deactivate the temporary elevation.

If you cannot manage app ownership yourself, an administrator can add you as owner in Microsoft Entra ID (App registrations > `<prefix>-passport` > Owners, and Enterprise applications > `<prefix>-passport` > Owners). B4 role grants still require one of the supported directory roles. If your organization cannot grant you such a role, an administrator with one must run stage B or perform the two hosted-agent app role assignments for you.

### DNS recovery and a clean restart

If a private DNS link reports a pending operation, inspect the `network` deployment under `<prefix>-rg` in the Azure portal and check the affected link from your laptop:

```powershell
az network private-dns link vnet show --resource-group <prefix>-rg --zone-name privatelink.services.ai.azure.com --name <prefix>-link --query "{provisioning:provisioningState,link:virtualNetworkLinkState}"
```

Wait while the link or deployment is still updating. A failed parent deployment does not establish that the service has finished its queued operation. Once it finishes, rerun `python -m dbp_mvp bootstrap --from A0`. If the conflict persists with no deployment running, provide the operation ID to Azure Support rather than repeatedly deleting and recreating DNS resources.

Starting from scratch is optional and destructive. After active deployments have stopped, run `python -m dbp_mvp teardown` on your laptop with the original settings. Save any wanted data first. Wait for **Teardown complete** and resolve any incomplete-cleanup message before running `bootstrap` again. See the [teardown guide](teardown.md) for the deletion scope and cleanup sequence. Deleting a deployment record alone does not delete its resources. Provider registrations remain and need not be undone; restarting will not remove tenant prerequisites.
