#!/usr/bin/env bash
# Match the compiler's docker-sudo-iptables invocation for deterministic checks.
set -euo pipefail
mode=${1:?smoke or candidate}
[[ "$mode" == smoke || "$mode" == candidate ]]
test -n "${MAINTAINER_SERVICE_PORTS:-}"
trusted="$RUNNER_TEMP/maintainer-trusted"
# Candidate checks receive only tool locations and immutable source metadata.
# Provider, publisher, telemetry and arbitrary inherited variables stay outside.
sudo /usr/bin/env -i PATH="$PATH" HOME="$HOME" USER="${USER:-runner}" \
  SUDO_USER="${USER:-runner}" SUDO_UID="$(id -u)" SUDO_GID="$(id -g)" \
  RUNNER_TOOL_CACHE="${RUNNER_TOOL_CACHE:-}" UV_CACHE_DIR="${UV_CACHE_DIR:-}" \
  RUNNER_TEMP="$RUNNER_TEMP" GITHUB_WORKSPACE="$GITHUB_WORKSPACE" \
  /usr/local/bin/awf \
  --config "$trusted/sandbox.json" --container-workdir "$GITHUB_WORKSPACE" \
  --allow-host-service-ports "$MAINTAINER_SERVICE_PORTS" \
  --legacy-security --enable-host-access --allow-host-ports 80,443,8080 \
  --mount "$trusted:/trusted-maintainer:ro" \
  --mount /tmp/maintainer-input:/tmp/maintainer-input:ro \
  --env "PATH=$PATH" --env "UV_CACHE_DIR=${UV_CACHE_DIR:-}" \
  --env "RUNNER_TOOL_CACHE=${RUNNER_TOOL_CACHE:-}" \
  --env "RUNNER_TEMP=$RUNNER_TEMP" --env "GITHUB_SHA=$GITHUB_SHA" \
  --env CI=true \
  -- /bin/bash -c "bash /trusted-maintainer/check_maintainer_environment.sh $mode"
