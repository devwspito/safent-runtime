# Composio dependency-matched regression tests

The broad DGX host suite skipped Composio modules because its installed
distribution lacks `composio.exceptions.ComposioError`. The source image and
`pyproject.toml` pin **composio==0.13.1**, not the >=1.0 version claimed in old
test comments. Those comments and skip messages now name the actual pin.

Created an isolated virtualenv on the DGX (no user environment changes) and
installed the exact SDK pin. The first run exposed a stale assertion: the
wrapper forwards `connected_account_id=None`, but the test expected no keyword.
The test now covers both None and an explicitly selected account while keeping
the user/entity scope. The real SDK's `Tools.execute` signature in 0.13.1 accepts
both keywords; no product fallback or compatibility shim was added.

Final targeted result: **89 passed**, zero skipped, 1.88s. These tests import the
real SDK and exercise our wrapper with a fake SDK transport/interface; they do
not send provider requests or prove real consent/account permissions. The three
warnings were unregistered unit marks in the isolated test snapshot.

Scratch: `/tmp/safent-composio-sdk.eSFEY2` on DGX. No credentials, remote tool
execution, package changes outside the isolated environment, or release.
