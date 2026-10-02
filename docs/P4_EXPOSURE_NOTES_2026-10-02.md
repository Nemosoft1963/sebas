# P4 exposure scope (2026-10-02)

The default public Compose file binds port 8099 to loopback. The production deployment also uses one explicitly configured LAN interface for a separate approved collection host. `scripts/healthcheck.ps1` derives allowed bindings from Compose and rejects wildcard interfaces even when an override requests them. The application does not yet authenticate LAN callers; restrict that deployment with an authenticated proxy and source allowlist before expanding access. No port or Compose configuration is changed by P4.
