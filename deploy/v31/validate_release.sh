#!/usr/bin/env bash
set -euo pipefail

compose_file="${1:-$(cd "$(dirname "$0")" && pwd)/compose.production.yaml}"

docker compose -f "$compose_file" config -q

mapfile -t images < <(docker compose -f "$compose_file" config --images | sort -u)
if [[ "${#images[@]}" -lt 2 ]]; then
  echo "ERROR: expected API and Calendar images" >&2
  exit 1
fi

for image in "${images[@]}"; do
  if [[ "$image" == "latest" || "$image" == *":latest" ]]; then
    echo "ERROR: mutable latest image is not allowed: $image" >&2
    exit 1
  fi
  docker image inspect "$image" >/dev/null
  printf 'image=%s id=%s\n' "$image" "$(docker image inspect "$image" --format '{{.Id}}')"
done

docker compose -f "$compose_file" run --rm --no-deps api python -m tm_api.healthcheck
docker compose -f "$compose_file" run --rm --no-deps calendar python -m tm_calendar.healthcheck
docker compose -f "$compose_file" run --rm --no-deps api python -m tm_api.v25.preflight

echo "RELEASE_VALIDATION_OK"
