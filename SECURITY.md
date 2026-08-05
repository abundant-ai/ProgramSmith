# Security Policy

## Supported versions

Security fixes are applied to the latest `0.1.x` release while ProgramSmith is in alpha.

## Reporting a vulnerability

Please use GitHub's **Report a vulnerability** flow in the repository Security tab. This creates a
private advisory visible only to the maintainers. Do not include credentials, private task assets,
or exploit details in a public issue.

## Local security boundary

The ProgramSmith dashboard is a local control plane: it can launch model-backed work and save
provider credentials in an owner-readable local config file. `programsmith serve` therefore binds
only to loopback addresses and rejects public interfaces. For remote access, keep the loopback
binding and use an authenticated SSH tunnel.

Task verification runs in a separate container with networking disabled. Model-solving containers
may use outbound network access only for their configured model provider.
