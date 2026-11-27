"""The Microsoft Entra app registration that protects the passport app: app roles, sign-in and its secret.

Microsoft Graph is called through az rest with the operator's own sign-in. The sign-in secret is created right before
each app deployment and handed to Container Apps through a temporary parameter file; it is never stored or printed.
"""

import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from . import MvpError
from .az import AzError
from .config import MARKER

GRAPH = "https://graph.microsoft.com/v1.0"
AZURE_CLI_APP_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"  # Microsoft's public client ID for the Azure CLI
GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
SIGN_IN_SCOPES = {  # Microsoft Graph delegated permissions Easy Auth asks for at browser sign-in
    "openid": "37f7f235-527c-4136-accd-4a02d197296e",
    "profile": "14dad69e-099b-42c9-810b-d002981feec1",
    "email": "64a6cdd6-aab1-4aaf-94b8-3cc8405e90d0",
}
OPERATOR_ROLES = {  # assigned to the operator by the deployment
    "Passport.Operator": ("Passport operator", "Imports supplier packages, runs the snapshot worker and reporting."),
    "Passport.Reviewer": ("Passport reviewer", "Uploads evidence and opens or resolves sourcing reviews."),
    "Passport.Connector": ("Passport connector", "Delivers signed partner messages to the mock connector."),
    "Passport.Compliance": ("Passport compliance", "Decides compliance cases on critical-material lots."),
}
ADMIN_ROLES = {  # declared on the app; an administrator assigns it in Microsoft Entra when needed
    "Passport.Admin": ("Passport administrator",
                       "Changes the app's Azure connections and monitoring links on the Settings page."),
}
VIEWER_ROLES = {  # declared on the app; assigned to test users with python -m dbp_mvp roles assign
    "Passport.OEM": ("Passport OEM viewer", "Reads passports with performance data (synthetic)."),
    "Passport.Fleet.AdventureWorks": ("Adventure Works fleet", "Scopes OEM aftersales access to Adventure Works."),
    "Passport.Fleet.Fabrikam": ("Fabrikam fleet", "Scopes OEM aftersales access to Fabrikam."),
    "Passport.Recycler": ("Passport recycler viewer", "Reads passports with composition and dismantling data."),
    "Passport.Authority": ("Passport authority viewer",
                           "Reads passports, parties, the connector log and compliance cases (market surveillance)."),
}
POLICY_ROLES = {
    "Passport.PolicyApprover": ("Passport policy approver",
                               "Reviews and activates versioned Regulatory Watch guidance; cannot bypass app gates."),
}
DECLARED_ROLES = {**OPERATOR_ROLES, **VIEWER_ROLES, **ADMIN_ROLES, **POLICY_ROLES}
SCOPE = "user_impersonation"
SECRET_NAME = "passport-sign-in"
SECRET_DAYS = 30
ATTEMPTS = 6
# A new app or service principal can take a few seconds to appear everywhere; Graph also throttles.
RETRY = re.compile(r"Not ?Found|Too Many Requests|Service Unavailable|\b(?:404|429|503)\b", re.IGNORECASE)
DENIED = re.compile(r"Authorization_RequestDenied|Insufficient privileges", re.IGNORECASE)
# Directory roles that may grant one application's app role to another identity (an application permission).
GRANT_ROLES = "Cloud Application Administrator, Application Administrator or Global Administrator"
GRANT_ROLE_TEMPLATES = {
    "158c047a-c907-4556-b7ef-446551a6b5f7",  # Cloud Application Administrator
    "9b895d92-2cd3-44c7-9d02-a6ac2d5ea5c3",  # Application Administrator
    "62e90394-69f5-4237-9190-012177145e10",  # Global Administrator
}
ROLES_HELP = "docs/deployment.md#microsoft-entra-roles-for-stage-b"


def denied(error):
    return isinstance(error, AzError) and bool(DENIED.search(error.stderr))


def refusal(action, phase):
    """The operator's directory role is missing or has expired (Privileged Identity Management)."""
    return MvpError(f"Microsoft Entra ID refused to {action} (Authorization_RequestDenied). This step needs an active "
                    f"{GRANT_ROLES} role. Activate one (for example in Privileged Identity Management), run az login "
                    f"again so your token carries it, then rerun 'python -m dbp_mvp deploy --from {phase}'. "
                    f"See {ROLES_HELP}.")


def graph(az, method, path, body=None, parameters=None, missing_ok=False, sleep=time.sleep):
    for attempt in range(ATTEMPTS):
        try:
            return az.rest(method, f"{GRAPH}{path}", body=body, parameters=parameters)
        except AzError as error:
            if missing_ok and error.not_found:
                return None
            if attempt == ATTEMPTS - 1 or not RETRY.search(error.stderr):
                raise
            sleep(5 * (attempt + 1))


def definition():
    """Everything the manifest is built from, for the phase fingerprint."""
    return {"roles": DECLARED_ROLES, "scope": SCOPE, "sign_in": SIGN_IN_SCOPES,
            "preauthorized": AZURE_CLI_APP_ID, "owners": "operator"}


def stable_id(app_id, value, existing):
    found = [item["id"] for item in existing if item.get("value") == value]
    return found[0] if found else str(uuid.uuid5(uuid.NAMESPACE_URL, f"{MARKER}/{app_id}/{value}"))


def manifest(app_id, fqdn, existing=None):
    """Two PATCH bodies. The scope must exist before the Azure CLI can be pre-authorized for it."""
    existing = existing or {}
    roles_defined = DECLARED_ROLES
    old_roles = existing.get("appRoles") or []
    old_scopes = (existing.get("api") or {}).get("oauth2PermissionScopes") or []
    roles = [{"id": stable_id(app_id, value, old_roles), "value": value, "displayName": name,
              "description": description, "allowedMemberTypes": ["User"], "isEnabled": True}
             for value, (name, description) in roles_defined.items()]
    roles += [role for role in old_roles if role.get("value") not in roles_defined]  # keep roles added by others
    scope_id = stable_id(app_id, SCOPE, old_scopes)
    scopes = [{"id": scope_id, "value": SCOPE, "type": "User", "isEnabled": True,
               "adminConsentDisplayName": "Use the battery passport app",
               "adminConsentDescription": "Lets a signed-in user call the passport app with their own app roles.",
               "userConsentDisplayName": "Use the battery passport app",
               "userConsentDescription": "Lets you call the passport app with your own app roles."}]
    scopes += [scope for scope in old_scopes if scope.get("value") != SCOPE]
    first = {
        "identifierUris": [f"api://{app_id}"],
        "appRoles": roles,
        "api": {"requestedAccessTokenVersion": 2, "oauth2PermissionScopes": scopes},
        "web": {"redirectUris": [f"https://{fqdn}/.auth/login/aad/callback"],
                "implicitGrantSettings": {"enableIdTokenIssuance": True, "enableAccessTokenIssuance": False}},
        "requiredResourceAccess": [{"resourceAppId": GRAPH_APP_ID, "resourceAccess": [
            {"id": scope, "type": "Scope"} for scope in SIGN_IN_SCOPES.values()]}],
    }
    second = {"api": {"requestedAccessTokenVersion": 2, "oauth2PermissionScopes": scopes,
                      "preAuthorizedApplications": [{"appId": AZURE_CLI_APP_ID,
                                                     "delegatedPermissionIds": [scope_id]}]}}
    return first, second


def exists(az, object_id, sleep=time.sleep):
    return graph(az, "get", f"/applications/{object_id}", parameters={"$select": "id"}, missing_ok=True,
                 sleep=sleep) is not None


def ensure_owner(az, collection, object_id, user_id, sleep=time.sleep):
    """Make the user an owner of the application or service principal. True when newly added."""
    path = f"/{collection}/{object_id}/owners"
    owners = (graph(az, "get", path, parameters={"$select": "id"}, sleep=sleep) or {}).get("value", [])
    if any((owner.get("id") or "").lower() == user_id.lower() for owner in owners):
        return False
    graph(az, "post", f"{path}/$ref", {"@odata.id": f"{GRAPH}/directoryObjects/{user_id}"}, sleep=sleep)
    return True


def ensure_registration(az, name, fqdn, sleep=time.sleep, owner_id=None):
    """Create or update the app registration and its service principal. Refuses one this MVP did not create.

    With owner_id, that user becomes an owner of both. Entra makes no administrator who creates an app its owner, so
    without this, later changes (the agents' app roles in B4, the sign-in secret, teardown) would depend on an
    administrator role that may since have expired."""
    found = (graph(az, "get", "/applications", parameters={"$filter": f"displayName eq '{name}'"},
                   sleep=sleep) or {}).get("value", [])
    if len(found) > 1:
        raise MvpError(f"More than one app registration is called {name}. Delete the extra ones in Microsoft Entra "
                       "ID (App registrations), then rerun.")
    if found and MARKER not in (found[0].get("tags") or []):
        raise MvpError(f"App registration {name} exists but was not created by this MVP (no {MARKER} tag). "
                       "Choose another prefix, or delete that registration if it is yours.")
    app = found[0] if found else graph(az, "post", "/applications", {
        "displayName": name, "signInAudience": "AzureADMyOrg", "tags": [MARKER],
        "notes": "Synthetic battery passport MVP. Removed by python -m dbp_mvp teardown."}, sleep=sleep)
    app = graph(az, "get", f"/applications/{app['id']}", sleep=sleep)
    for body in manifest(app["appId"], fqdn, app):
        graph(az, "patch", f"/applications/{app['id']}", body, sleep=sleep)
    principals = (graph(az, "get", "/servicePrincipals", parameters={"$filter": f"appId eq '{app['appId']}'"},
                        sleep=sleep) or {}).get("value", [])
    principal = principals[0] if principals else graph(az, "post", "/servicePrincipals", {"appId": app["appId"]},
                                                       sleep=sleep)
    if owner_id:
        ensure_owner(az, "applications", app["id"], owner_id, sleep)
        ensure_owner(az, "servicePrincipals", principal["id"], owner_id, sleep)
    return {"display_name": name, "object_id": app["id"], "app_id": app["appId"],
            "service_principal_id": principal["id"]}


def assign_roles(az, service_principal_id, user_id, sleep=time.sleep):
    """Give the operator the operator and reviewer roles. Returns (newly assigned, all passport roles held)."""
    path = f"/servicePrincipals/{service_principal_id}"
    for attempt in range(ATTEMPTS):
        roles = {role["value"]: role["id"] for role in
                 (graph(az, "get", path, parameters={"$select": "appRoles"}, sleep=sleep) or {}).get("appRoles", [])}
        missing = [value for value in OPERATOR_ROLES if value not in roles]
        if not missing:
            break
        if attempt == ATTEMPTS - 1:
            raise MvpError(f"The app roles {', '.join(missing)} did not appear on the service principal. Rerun "
                           "with --from B1.")
        sleep(5 * (attempt + 1))
    assigned = {(item["principalId"], item["appRoleId"]) for item in
                (graph(az, "get", f"{path}/appRoleAssignedTo", parameters={"$top": "999"}, sleep=sleep)
                 or {}).get("value", [])}
    added = []
    for value in OPERATOR_ROLES:
        if (user_id, roles[value]) not in assigned:
            graph(az, "post", f"{path}/appRoleAssignedTo",
                  {"principalId": user_id, "resourceId": service_principal_id, "appRoleId": roles[value]}, sleep=sleep)
            assigned.add((user_id, roles[value]))
            added.append(value)
    held = sorted(value for value, role_id in roles.items()
                  if value in DECLARED_ROLES and (user_id, role_id) in assigned)
    return added, held


def find_user(az, user=None, sleep=time.sleep):
    """The signed-in user (user None) or the user with this user principal name or object ID: (object ID, name)."""
    path = "/me" if user is None else f"/users/{quote(user, safe='@')}"
    found = graph(az, "get", path, parameters={"$select": "id,userPrincipalName"}, missing_ok=True, sleep=sleep)
    if not found or not found.get("id"):
        raise MvpError(f"No user {user} in this Microsoft Entra tenant. Give a user principal name or object ID "
                       "of a user in the tenant." if user else "Microsoft Graph did not return the signed-in user.")
    return found["id"], found.get("userPrincipalName") or user


def check_roles(values):
    unknown = [value for value in values if value not in DECLARED_ROLES]
    if unknown:
        raise MvpError(f"{', '.join(unknown)} is not a role of this MVP. Choose from {', '.join(DECLARED_ROLES)}.")


def held_roles(az, service_principal_id, user_id, sleep=time.sleep):
    """The MVP's app roles on the service principal and the user's assignments: ({value: role ID},
    {value: assignment ID})."""
    path = f"/servicePrincipals/{service_principal_id}"
    roles = {role["value"]: role["id"] for role in
             (graph(az, "get", path, parameters={"$select": "appRoles"}, sleep=sleep) or {}).get("appRoles", [])
             if role.get("value") in DECLARED_ROLES}
    by_id = {role_id: value for value, role_id in roles.items()}
    held = {by_id[item["appRoleId"]]: item.get("id") for item in
            (graph(az, "get", f"{path}/appRoleAssignedTo", parameters={"$top": "999"}, sleep=sleep)
             or {}).get("value", [])
            if item.get("principalId") == user_id and item.get("appRoleId") in by_id}
    return roles, held


def change_roles(az, service_principal_id, user_id, values, remove=False, sleep=time.sleep):
    """Assign (or remove) these declared app roles for one user; repeating it changes nothing.
    Returns (changed, roles held afterwards)."""
    check_roles(values)
    roles, held = held_roles(az, service_principal_id, user_id, sleep)
    missing = [value for value in values if value not in roles]
    if missing:
        raise MvpError(f"The app roles {', '.join(missing)} are not on the app registration yet. Run python -m "
                       "dbp_mvp deploy (it repeats B1 when the declared roles change), then rerun.")
    path = f"/servicePrincipals/{service_principal_id}/appRoleAssignedTo"
    changed = []
    for value in dict.fromkeys(values):
        if remove and value in held:
            graph(az, "delete", f"{path}/{held.pop(value)}", sleep=sleep)
            changed.append(value)
        elif not remove and value not in held:
            graph(az, "post", path, {"principalId": user_id, "resourceId": service_principal_id,
                                     "appRoleId": roles[value]}, sleep=sleep)
            held[value] = None
            changed.append(value)
    return changed, sorted(held)


def add_secret(az, object_id, now=None, sleep=time.sleep):
    """A new 30-day sign-in secret. Returns (key ID, secret text, expiry); only the key ID and expiry may be kept."""
    end = (now or datetime.now(timezone.utc)) + timedelta(days=SECRET_DAYS)
    result = graph(az, "post", f"/applications/{object_id}/addPassword",
                   {"passwordCredential": {"displayName": SECRET_NAME,
                                           "endDateTime": end.strftime("%Y-%m-%dT%H:%M:%SZ")}}, sleep=sleep)
    if not result or not result.get("keyId") or not result.get("secretText"):
        raise MvpError("Microsoft Graph did not return the new sign-in secret.")
    return result["keyId"], result["secretText"], end.strftime("%Y-%m-%d")


def remove_secret(az, object_id, key_id, sleep=time.sleep):
    graph(az, "post", f"/applications/{object_id}/removePassword", {"keyId": key_id}, sleep=sleep)


def prune_secrets(az, object_id, keep, sleep=time.sleep):
    """Remove this MVP's older sign-in secrets; other credentials on the app are left alone."""
    app = graph(az, "get", f"/applications/{object_id}", parameters={"$select": "passwordCredentials"},
                sleep=sleep) or {}
    removed = 0
    for credential in app.get("passwordCredentials") or []:
        if credential.get("displayName") == SECRET_NAME and credential.get("keyId") != keep:
            remove_secret(az, object_id, credential["keyId"], sleep)
            removed += 1
    return removed
