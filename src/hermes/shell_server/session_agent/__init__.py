"""session_agent — session-side helper services.

Each module here runs as a systemd USER unit (hermes-user session), giving
it access to the Wayland / mutter D-Bus that the hardened hermes-runtime.service
daemon cannot reach (ProtectHome=yes, PrivateDevices=yes, no session bus).

The daemon communicates with these helpers over authenticated AF_UNIX sockets
inside /run/hermes/ (mode 0700 hermes:hermes + SO_PEERCRED UID check).
"""
