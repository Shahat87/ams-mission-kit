#!/usr/bin/env bash
# Build the mc-lane images and print name@sha256:<image id> for each. No --pull: bases are pinned by digest in the
# Dockerfiles. Context is plugin/ (the smoke image copies tests/fixtures). Note: the printed digest is the local image ID.
set -euo pipefail

if ! command -v docker >/dev/null 2>&1; then
  echo "build.sh: docker not found. Install Docker (Desktop on the Surface, Engine on the Lenovo) and re-run." >&2
  exit 2
fi

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
plugin="$(cd "$here/.." && pwd)"

build() {  # build <name> <dockerfile> <context> [extra docker build args...]
  local name="$1" file="$2" ctx="$3"
  shift 3
  docker build -f "$file" -t "$name" "$@" "$ctx" >&2
  printf '%s@%s\n' "$name" "$(docker image inspect --format '{{.Id}}' "$name")"
}

build mc-lane        "$here/Dockerfile"       "$plugin"
build mc-lane-ams    "$here/Dockerfile.ams"   "$plugin" --build-arg BASE=mc-lane
build mc-lane-smoke  "$here/Dockerfile.smoke" "$plugin" --build-arg BASE=mc-lane
build mc-lane-proxy  "$here/proxy/Dockerfile" "$here/proxy"
