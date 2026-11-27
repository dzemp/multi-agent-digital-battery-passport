"""Who is calling? Reads the identity that Container Apps authentication (Easy Auth) injects.

Easy Auth validates the sign-in cookie or bearer token before a request reaches this code and
then adds the X-MS-CLIENT-PRINCIPAL header. Trusting that header is safe only behind Easy Auth,
which strips client-supplied copies. Local mode trusts local identity headers and is for a PC only.
"""

import base64
import json
from dataclasses import dataclass, field

OPERATOR = "Passport.Operator"
REVIEWER = "Passport.Reviewer"
ADMIN = "Passport.Admin"  # edits the Settings page only; grants no passport data or decisions
CONNECTOR = "Passport.Connector"  # delivers partner messages; their content is trusted only by signature
AUTHORITY = "Passport.Authority"  # reads passports (no evidence), parties, the connector log, quarantine and cases
COMPLIANCE = "Passport.Compliance"  # opens and decides compliance cases; uploads adverse and audit reports
POLICY_APPROVER = "Passport.PolicyApprover"  # approves versioned Watch guidance; never bypasses application gates
OEM = "Passport.OEM"  # see access.FIELD_POLICY
FLEET_ROLES = {"Passport.Fleet.AdventureWorks": "OEM-ADVENTUREWORKS", "Passport.Fleet.Fabrikam": "OEM-FABRIKAM"}
RECYCLER = "Passport.Recycler"  # reads the public passport plus performance and composition
ROLE_CLAIMS = {"roles", "http://schemas.microsoft.com/ws/2008/06/identity/claims/role"}
OID_CLAIMS = {"oid", "http://schemas.microsoft.com/identity/claims/objectidentifier"}
NAME_CLAIMS = {"name", "preferred_username", "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/upn"}
LOCAL_USER_HEADER = "x-local-user"
LOCAL_ROLES_HEADER = "x-local-roles"


@dataclass(frozen=True)
class Principal:
    user_id: str
    name: str
    roles: frozenset = field(default_factory=frozenset)
    source: str = "easyauth"

    def has_any(self, roles):
        return bool(self.roles & set(roles))

    def actor(self):
        return f"{self.name} ({self.user_id})"[:200]


def from_easy_auth(headers):
    encoded = headers.get("x-ms-client-principal")
    if not encoded:
        return None
    try:
        data = json.loads(base64.b64decode(encoded + "=" * (-len(encoded) % 4)))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    claims = [c for c in data.get("claims") or [] if isinstance(c, dict)]
    role_types = ROLE_CLAIMS | {data.get("role_typ")}
    name_types = NAME_CLAIMS | {data.get("name_typ")}
    roles = frozenset(c["val"] for c in claims if c.get("typ") in role_types)
    oid = next((c["val"] for c in claims if c.get("typ") in OID_CLAIMS), None)
    name = next((c["val"] for c in claims if c.get("typ") in name_types), None)
    user_id = oid or headers.get("x-ms-client-principal-id") or "unknown"
    return Principal(user_id, name or headers.get("x-ms-client-principal-name") or user_id, roles)


def from_local_headers(headers):
    user = headers.get(LOCAL_USER_HEADER)
    if not user:
        return None
    roles = frozenset(r.strip() for r in headers.get(LOCAL_ROLES_HEADER, "").split(",") if r.strip())
    return Principal(f"local:{user}", user, roles, "local")


def principal(headers, mode):
    return from_local_headers(headers) if mode == "local" else from_easy_auth(headers)
