# Telegram Manager

Telegram Manager is a self-hosted workflow manager that turns bounded Telegram/WhatsApp evidence
into structured project, payment, reconciliation, Calendar/Reminders and post-production workflows.

This repository is a **reviewed sanitized source mirror** of the private canonical project. It is
published so the architecture, tests and implementation can be inspected and exercised without
exposing production data, credentials, internal development history or private operational
identifiers.

## What is included

- application source for the API, Calendar/Reminders, workers and supported read-oriented sources;
- Docker/build material needed by the public CI suite;
- synthetic fixtures and pseudonymized identifiers;
- the exact public CI workflow used for this snapshot.

## What is intentionally excluded

- databases, messages and customer/project data;
- credentials, sessions, keys and environment files;
- production logs, backups and release state;
- private development documents and original Git history;
- production-only publication policy and deployment authority.

The sanitizer pseudonymizes names, instance labels, service addresses and operational identifiers.
If protected content survives the reviewed transformation policy, export fails closed.

## Verification

Every snapshot contains `SOURCE_MANIFEST.json` with the exact private source SHA and the digest of
the sanitized public content. Public CI validates Python/JavaScript syntax, Compose configuration,
Docker images and the automated test suite. Passing public CI is evidence for this sanitized
snapshot; it is **not** a byte-for-byte certification of the private production artifact.

## Development and contributions

See [CONTRIBUTING.md](CONTRIBUTING.md). This repository is generated from a private canonical
source, so direct changes to this mirror can be overwritten by the next reviewed snapshot. Issues
and proposed patches are welcome; maintainers port accepted changes to canonical source before the
next export.

## Security

See [SECURITY.md](SECURITY.md). Never post credentials, private messages, production identifiers,
session material or customer data in public issues.

## License

Licensed under the MIT License. See [LICENSE](LICENSE).

## Production boundary

Do not treat this repository as a production release artifact. Production releases are built only
from the private canonical source through the project's separate guarded release process.

Generic installation labels: system user: `telegram-manager`; SSH alias: `telegram-server`.
These labels contain no credentials or access instructions.

The repository may be ahead of production.
