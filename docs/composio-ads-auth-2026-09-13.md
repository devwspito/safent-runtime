# Composio OAuth selection and isolation for Ads

## What is implemented

The existing Composio SDK 0.13.1 / Connect Link integration now supports an
explicit owner-selected OAuth auth configuration for `googleads` or `metaads`.
The mapping is not a secret: `integration_auth_configs` stores only toolkit and
auth-config IDs. Provider credentials remain in Composio, not in API responses
or the native application bundle.

The selection is validated against the current Composio project API key using
`auth_configs.get()`. Its ID, toolkit, `ENABLED` state, and OAuth scheme must
match. Validation happens before storing and again before initiating a link;
disabling a selected config does not silently fall back to another account.

Managed Google Ads is accepted only when the SDK's actual
`composio_managed_auth_schemes` metadata includes OAuth and the toolkit is
enabled. OAuth2 support alone is not evidence that Composio supplies the app.
Meta without managed OAuth requires the administrator-selected app config.
Other integrations retain the existing managed OAuth path.

## Existing API extended, not a second authentication framework

All paths below have prefix `/api/v1/integrations/composio`.

| Method / path | Contract |
| --- | --- |
| `GET /auth-configs/{googleads\|metaads}` | Owner session; returns `{toolkit_slug, auth_config_id}`; ID may be null. |
| `PUT /auth-configs/{googleads\|metaads}` | Owner session; accepts only `{auth_config_id}`; validates before storing. |
| `DELETE /auth-configs/{googleads\|metaads}` | Owner session; clears local selection, does not delete the provider app or accounts. |
| `POST /connect` | Owner session; accepts `{toolkit_slug, redirect_url?}`. No entity/auth-config override. |
| `GET /connected/{id}` | Server-scoped confirmation; returns only `{id, toolkit_slug, entity_id, status, auth_config_id}`. |
| `DELETE /connected/{id}` | Owner session; checks remote ID and server-owned entity before deletion. |

The real UI owner bearer is required for administrative changes; an internal
daemon bearer is not an owner session. Key changes also require the owner.
New installations receive unique server-generated entity IDs. Rotating a key
preserves the existing instance identity in both HTTP and D-Bus paths.
Existing installations retain their old entity so existing grants are not
silently lost. Deployments already sharing an old `default` entity require an
explicit migration; this change does not assert that they are isolated.

Ads Connect Link uses `allow_multiple=True`, allowing multiple accounts without
switching an existing connection. Listing and exact lookups independently
check remote entity ownership before returning metadata.

## Broker/onboarding reuse

- `SQLiteIntegrationsRepository.auth_config_ids()` provides the local mapping.
- `repo.get(kind="composio").entity_id` provides server identity.
- `ComposioClient.get_connected_account(id, entity_id=...)` returns a safe
  metadata projection, not the credential-bearing SDK object.
- Confirmation may return `INITIATED`; execution admission must require
  `ACTIVE`, the expected toolkit/auth config, and Ads account authorization.
- The Composio API key is still only revealed internally by the vault-backed
  repository. No endpoint exports it to the panel or companion.

## Not completed by this change

This is not proof of a real Google/Meta consent, provider app approval, full
API tool coverage, or connected Ads campaigns. No provider grant was created
and no campaign was mutated for these tests. The `safent-ads` transport and
authenticated runtime-to-companion handoff are separate integration work.
Direct Composio tools in runtime do not replace Ads-specific budget limits,
signed approvals, drift checks, or its ledger.

## Validation

The dependency-matched SDK environment ran 251 tests successfully across the
Composio client, Ads OAuth/configuration and owner API regressions, credential
repository/source, tool registry/specs, read broker security route and config
sync applier. One existing Starlette/AnyIO deprecation warning. Tests use fake
provider transports; they do not prove live permissions or consent.

## Generic tool safety boundary

Google Ads and Meta Ads connections remain available for the dedicated Ads
onboarding, but their tools are no longer published through the runtime's
generic Composio discovery. This applies to reads as well as writes so the
agent has one account- and policy-scoped route: Anuncios.

The generic Composio surface adapter independently rejects `GOOGLEADS_*` and
`METAADS_*` before any SDK invocation, even for an old tool specification or a
generic human approval. Case, surrounding whitespace, account suffixes and
misleading schemas/labels do not bypass the actual SDK-slug check. Malformed
or non-ASCII tool slugs are rejected before network access as well.

The error is `safent_ads_module_required` with the message: "Usa Anuncios para
que se apliquen cuentas, presupuestos y aprobaciones." Gmail and other
integrations retain their existing broker path. The lower-level SDK wrapper
and dedicated Ads transport are not disabled by this gate.

Gate validation: 107 tests passed across the new safety cases, generic
read-broker regression, discovery/specs and SDK wrapper tests. No real Ads
operation was executed.
