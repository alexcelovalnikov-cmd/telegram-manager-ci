#!/bin/sh
set -eu
cd /opt/telegram-manager/releases/V19
TM_WRITES_ENABLED=false docker compose -f deploy/compose.yaml up -d --no-build --force-recreate api
# Completed business actions and the idempotency journal are deliberately retained.
