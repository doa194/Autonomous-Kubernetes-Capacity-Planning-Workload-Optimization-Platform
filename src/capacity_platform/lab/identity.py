"""Keycloak realm for the lab (S06): roles, the public PKCE CLI client and test humans.

Five distinct test identities exist so separation of duties can be exercised honestly
(two roles on one person is never valid SoD). Passwords are generated per install, stored
in Vault (for Keycloak's realm import) and DPAPI-protected on the host for the operator.

`capacity-e2e` is a TEST-ONLY client with the direct-access (password) grant, used only by
the automated end-to-end verification. It is created only when `--with-e2e-client` is set,
and the API accepts its tokens only in the verification profile (docs/security.md).
"""

from __future__ import annotations

import json
import secrets

from capacity_platform.lab.protect import read_json, write_json
from capacity_platform.lab.tools import HOME

USERS_FILE = HOME / "identity" / "test-users.json.dpapi"
ROLES = ["Viewer", "Analyst", "Planner", "Approver", "Executor", "Administrator"]
TEST_USERS = {
    "planner1": ["Planner", "Analyst"],
    "approver1": ["Approver"],
    "operator1": ["Executor"],
    "viewer1": ["Viewer"],
    "admin1": ["Administrator"],
}
CLI_CALLBACK_PORT = 8765


def ensure_passwords() -> dict[str, str]:
    if USERS_FILE.exists():
        return read_json(USERS_FILE)
    passwords = {user: secrets.token_urlsafe(18) for user in TEST_USERS}
    write_json(USERS_FILE, passwords)
    return passwords


def realm(passwords: dict[str, str], with_e2e_client: bool) -> dict:
    audience_scope = {
        "name": "capacity-api-audience",
        "protocol": "openid-connect",
        "attributes": {"include.in.token.scope": "false"},
        "protocolMappers": [
            {
                "name": "capacity-api-audience",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-audience-mapper",
                "config": {
                    "included.custom.audience": "capacity-api",
                    "access.token.claim": "true",
                    "id.token.claim": "false",
                },
            }
        ],
    }
    # A realm import that lists `clientScopes` replaces Keycloak's built-in scopes, so `basic`
    # (the `sub` claim) and `roles` (realm roles in `realm_access.roles`) are declared here.
    basic_scope = {
        "name": "basic",
        "protocol": "openid-connect",
        "attributes": {"include.in.token.scope": "false"},
        "protocolMappers": [
            {
                "name": "sub",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-sub-mapper",
                "config": {"access.token.claim": "true", "introspection.token.claim": "true"},
            }
        ],
    }
    roles_scope = {
        "name": "roles",
        "protocol": "openid-connect",
        "attributes": {"include.in.token.scope": "false"},
        "protocolMappers": [
            {
                "name": "realm roles",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-usermodel-realm-role-mapper",
                "config": {
                    "claim.name": "realm_access.roles",
                    "multivalued": "true",
                    "access.token.claim": "true",
                    "id.token.claim": "false",
                    "jsonType.label": "String",
                },
            }
        ],
    }
    clients = [
        {
            "clientId": "capacityctl",
            "name": "capacityctl operator CLI",
            "publicClient": True,
            "standardFlowEnabled": True,
            "directAccessGrantsEnabled": False,
            "implicitFlowEnabled": False,
            "serviceAccountsEnabled": False,
            "redirectUris": [f"http://127.0.0.1:{CLI_CALLBACK_PORT}/callback"],
            "webOrigins": [],
            "attributes": {"pkce.code.challenge.method": "S256", "access.token.lifespan": "600"},
            "defaultClientScopes": ["basic", "roles", "capacity-api-audience"],
        }
    ]
    if with_e2e_client:
        clients.append(
            {
                "clientId": "capacity-e2e",
                "name": "TEST ONLY: automated verification",
                "publicClient": True,
                "standardFlowEnabled": False,
                "directAccessGrantsEnabled": True,
                "defaultClientScopes": ["basic", "roles", "capacity-api-audience"],
            }
        )
    return {
        "realm": "capacity",
        "enabled": True,
        "sslRequired": "all",
        "registrationAllowed": False,
        "accessTokenLifespan": 600,
        "ssoSessionIdleTimeout": 1800,
        "bruteForceProtected": True,
        "roles": {"realm": [{"name": r, "description": f"Capacity platform {r}"} for r in ROLES]},
        "clientScopes": [basic_scope, roles_scope, audience_scope],
        "clients": clients,
        "users": [
            {
                "username": u,
                "enabled": True,
                "emailVerified": True,
                "firstName": u,
                "lastName": "lab",
                "email": f"{u}@capacity.test",
                "realmRoles": roles,
                "credentials": [{"type": "password", "value": passwords[u], "temporary": False}],
            }
            for u, roles in TEST_USERS.items()
        ],
    }


def realm_json(with_e2e_client: bool) -> str:
    return json.dumps(realm(ensure_passwords(), with_e2e_client))
