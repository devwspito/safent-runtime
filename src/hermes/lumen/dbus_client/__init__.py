"""hermes.lumen.dbus_client — thin transport wrappers over org.hermes.Runtime1.

Shared by the overlay (T025) and the capability apps (T040-T046).
Pure transport: no business logic, no caching, no state.

All authorship is derived from the bus sender_uid by the daemon (CWE-862).
Never HTTP, never operator tokens in the payload — GATE 0 / M1.
"""
