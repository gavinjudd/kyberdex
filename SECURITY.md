# Security policy

KyberDex is self-hosted software. Only the latest code on the default branch is currently considered for fixes; there is no guaranteed response time or backport schedule.

## Report a vulnerability

Private vulnerability reporting is enabled. Use GitHub's private reporting flow: [Report a vulnerability](https://github.com/gavinjudd/kyberdex/security/advisories/new).

If that private channel is unavailable, open an issue titled **Security contact requested** with no exploit details, credentials, personal data or affected deployment addresses. Wait for the maintainer to establish a private reporting route before sharing sensitive material.

A useful private report includes the affected commit, the vulnerable component, expected and observed behavior, a minimal reproduction with synthetic data and the impact. Do not test against deployments you do not administer or access other players' data.

## Operator responsibilities

Keep Discord tokens in a protected file outside tracked source. Rotate a token through Discord immediately if it is exposed. Keep databases, backups, logs and submitted player complaints private. Run the bot as an unprivileged account, configure the intended guild/channels explicitly and review dependencies and updates before deploying.

The catalog server exposes public definitions and reviewed assets; it is not an authenticated player API. Do not extend it to serve inventory or private state without designing authentication and authorization first.

The MIT license and security policy do not certify a deployment or guarantee that all defects are known. Offline tests are useful evidence, but operators should verify permissions, restore procedures and real interactions in a controlled test server.
