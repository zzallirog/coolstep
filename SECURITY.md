# Security policy

## Trust model

coolstep is a single-user local daemon. The dashboard binds
`127.0.0.1:18889` by default; loopback is the gate. Authentication for
remote exposure is chosen by the operator (SSH tunnel, reverse proxy,
WireGuard). See [`docs/security.md`](docs/security.md) and
[`docs/headless-deployment.md`](docs/headless-deployment.md) for the
patterns we recommend.

## Reporting a vulnerability

Please use GitHub's **Security → Report a vulnerability** tab on this
repository, or email the maintainer (address in the maintainer's
GitHub profile). Include:

- coolstep version (`coolstep --version`),
- a minimal reproduction (curl / script preferred),
- the attacker capability the issue assumes,
- demonstrable impact.

We acknowledge within **7 days** and aim to ship a fix or a clear
mitigation within **30 days** for confirmed reports. Public disclosure
happens once a fix ships, or at 90 days, whichever serves users best.

## Contributing hardening

PRs that close a specific gap with a regression test are very welcome.
See *Contributing* in [`docs/security.md`](docs/security.md) for the bar
and `tests/dashboard/test_security.py` for the test pattern.
