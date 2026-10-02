# Contributing

Thanks for helping improve Telegram Manager.

## Repository model

This public repository is a generated, reviewed mirror of a private canonical source repository.
Direct commits or pull requests against the mirror can be overwritten by the next sanitized
snapshot.

For that reason:

1. Open an issue describing the change or bug using synthetic examples only.
2. If a patch is useful, a public PR is welcome as review material.
3. Maintainers reproduce/port accepted changes into canonical source.
4. The next privacy-reviewed sanitized snapshot publishes the accepted result.

## Before submitting

- never include real Telegram/WhatsApp messages, chat IDs, phone numbers or usernames;
- never include credentials, sessions, keys, environment files, database dumps or production logs;
- use synthetic fixtures for tests;
- keep production infrastructure details generic;
- run the existing Python and JavaScript tests relevant to your change.

## Generated files

`README.md`, `LICENSE`, `SECURITY.md`, `CONTRIBUTING.md`,
`.github/workflows/ci.yml` and `SOURCE_MANIFEST.json` are generated or controlled by the
publication process. Changes to them must be made in the canonical publication tooling rather than
only in this mirror.

## License

By contributing, you agree that your contribution may be distributed under the repository's MIT
License.
