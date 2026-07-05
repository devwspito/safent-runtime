"""hermes.instance — enterprise pairing (association) for the Safent runtime.

A Safent instance is Community Edition (CE) by default.  When the operator
runs `safent pair <code>`, this package orchestrates the handshake with the
control plane and persists the resulting association, switching the instance
to "associate" edition.

Dependency direction (one-way):
  instance  →  agents_os.application  (NodeEnrollmentService, TenantBindingService)
  instance  →  shell_server.security  (SecretsVault)
  shell_server  →  instance            (router wires the service)

No framework imports in this top-level package.
"""
