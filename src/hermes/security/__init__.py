"""hermes.security — kernel-level confinement helpers.

Public surface:
    landlock_loader  — invocable module that applies a Landlock ruleset
                       to the CURRENT process (must be exec'd as child,
                       never imported by the daemon).
"""
