#!/bin/sh
set -eu
printf "%s\n" "V24: automatic rollback is intentionally disabled. See DEPLOY_V24.md; keep calendar data and inspect outstanding previews before switching API back." >&2
exit 1
