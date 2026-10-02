#!/bin/sh
# Run on the personal server. Stops only the additive V19 gateway.
# V18 Mac services, Supabase and the separate RentRabbit API are not touched.
set -eu
cd /opt/telegram-manager/releases/V19/deploy
docker compose stop api
printf '%s\n' 'Telegram Manager V19 API stopped. State and audit retained. Mac V18 remains operational.'
