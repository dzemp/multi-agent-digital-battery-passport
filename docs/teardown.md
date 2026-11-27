# Teardown guide

You can stop the developer VM temporarily or remove the MVP environment when you no longer need it. Run both commands from the MVP folder on your laptop.

| Goal | Command | Costs and effects |
|---|---|---|
| Pause the developer VM | `python -m dbp_mvp vm stop` | Stops VM compute charges only. SQL compute can auto-pause after 60 idle minutes, but SQL storage, the VM disk and public IP, and other deployed resources remain billable. Check whether Fabric capacity is paused; an active capacity remains billable. See the [cost table](../README.md#costs-drivers-and-controls). |
| Remove the environment | `python -m dbp_mvp teardown` | Permanently deletes the resource group and the other resources described below. Check the command's output and the resources listed in [what stays](#4-what-stays) for remaining costs. |

## Before you begin

**Teardown deletes every resource in the tagged resource group**, including resources that this MVP did not create. It also deletes synthetic data and ledger digests in the resources it removes. Check the group and copy any data or evidence registers you want to keep before proceeding. Foundry accounts are purged, not just deleted.

Run `teardown` on your laptop, not on the developer VM, because the command deletes that VM. Use the same `config/mvp.toml` as the deployment. Sign in with `az login` as `operator_upn` and select the subscription specified in the settings.

Teardown lists what it intends to delete and asks you to type the resource group name. The `--yes` option skips that confirmation; use it only in scripts you have reviewed. Use `--keep-app-registration` if you want to retain the app registration.

## 1. What teardown checks first

Teardown stops before deleting anything if:

- The resource group `<prefix>-rg` exists without the tag `project=battery-passport`. The tag is a deletion guard, not proof that every resource in the group belongs to the MVP.
- A ledger digest container has a **Locked** retention policy. Azure blocks deletion of the storage account until the retention period ends. The MVP leaves this policy unlocked.

The retention check examines every storage account in the group. It skips a missing digest container or policy, including Azure's `ContainerOperationFailure` with the exact message **The specified container does not exist**. Other storage failures, including permission errors, stop the check before deletion. A missing container in one account does not bypass a locked policy in another.

## 2. What it deletes, in order

1. **Fabric workspace:** `<prefix>-passport`, when Fabric is enabled and the workspace description carries the MVP marker. If deletion fails, teardown resumes the capacity and retries once. An active capacity is billable.
2. **Unlocked retention policies:** the policies on ledger digest containers, so the storage accounts can be deleted.
3. **Cognitive Services accounts in the resource group:** for each account, teardown deletes the `passport` project's agents capability host, the **`passport` project**, and any account-level capability hosts. It waits up to 15 minutes for each child to disappear, then deletes and purges the account. Missing children are skipped. A timeout or permission error stops account deletion; rerun after resolving it. Teardown then waits up to 25 minutes for Azure to release the agents subnet.
4. **Resource group:** `<prefix>-rg` and everything in it, including the network, VM, SQL, storage, registry, Container Apps, monitoring, Fabric capacity, and role assignments in the group. Deletion can take time.
5. **Subscription deployment record:** `<prefix>-mvp`.
6. **App registration:** `<prefix>-passport`, only if it carries the MVP marker tag. Its app roles and assignments are removed with it. `--keep-app-registration` leaves it in place.

When Azure reports that the resource group no longer exists, teardown clears `output/state.json` on the laptop. It also checks for soft-deleted Foundry accounts associated with the group. Other cleanup issues can remain even if the group is gone, so check the final output.

## 3. When teardown is incomplete

The command reports **Teardown complete** when its cleanup checks pass. If it reports **Teardown incomplete**, follow the listed actions. Other errors, such as permission failures or child-resource timeouts, can stop the command earlier. Resources can remain and incur charges until cleanup finishes.

| Message or condition | Action |
|---|---|
| `ContainerOperationFailure`: "The specified container does not exist" while checking an immutability policy | Rerun `teardown` with the original settings and a current checkout. Missing containers do not need to be recreated; do not remove retention safeguards to work around the error. |
| Fabric workspace "could not be checked" or "delete it in the Fabric portal" | Check the workspace in Fabric portal > Workspaces > `<prefix>-passport` > Workspace settings. Remove it there if appropriate. |
| Fabric workspace "not created by this MVP" | The workspace is intentionally left in place. Remove it yourself only after confirming that it is unused. |
| `CannotDeleteResource`: "Cannot delete resource while nested resources exist", naming `projects/passport` | Use a current checkout and rerun `teardown` with the original settings. Do not recreate deleted resources or erase `output/state.json` to bypass the failure. |
| A capability host or project "was not removed within 15 minutes" | The account was not deleted. Wait for Azure to finish deleting the child, then rerun `teardown`. Resolve any reported permission error first. |
| "the agents subnet still has a service link" and the resource group remains | Wait 30 minutes, then rerun `teardown`. |
| "resource group ... still exists" | Inspect the resource group in the Azure portal for a resource that failed to delete, then rerun `teardown`. |
| "soft-deleted Foundry account ...: purge it" | Run `az cognitiveservices account purge --location <location> --resource-group <prefix>-rg --name <account>`. |
| "app registration ...: left in place, not created by this MVP" | An app with the same name lacks the marker. Check it before deciding whether to delete it yourself. |

Teardown can be rerun with the original settings; resources that are already gone are skipped. See Microsoft's [Foundry project deletion guidance](https://learn.microsoft.com/en-us/azure/foundry/how-to/create-projects#delete-projects) for more information.

## 4. What stays

- `config/mvp.toml` and evidence registers under `output/` on your machines. Registers on the developer VM are deleted with it, so copy them first if needed.
- Resource provider registrations on the subscription. They do not incur charges.
- Microsoft Defender for Servers Plan 2, if you enabled it for `vm_access = "jit"`. This MVP checks the plan but does not enable or disable it.
- Your role assignments outside the resource group and any Power BI licenses you assigned.
- The app registration if you used `--keep-app-registration`, plus any workspace or app registration left in place because it lacked the MVP marker.

To start again, run `bootstrap` with the same or a new prefix.
