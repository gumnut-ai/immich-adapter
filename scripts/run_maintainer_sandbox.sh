#!/usr/bin/env bash
# Match the compiler's docker-sudo-iptables invocation for deterministic checks.
set -euo pipefail
mode=${1:?smoke or candidate}
test -n "${MAINTAINER_SERVICE_PORTS:-}"
trusted="$RUNNER_TEMP/maintainer-trusted"
sudo -E /usr/bin/env PATH="$PATH" /usr/local/bin/awf \
  --config "$trusted/sandbox.json" --container-workdir "$GITHUB_WORKSPACE" \
  --allow-host-service-ports "$MAINTAINER_SERVICE_PORTS" \
  --legacy-security --enable-host-access --allow-host-ports 80,443,8080 \
  --mount "$trusted:/trusted-maintainer:ro" \
  --mount /tmp/maintainer-input:/tmp/maintainer-input:ro \
  --env-all --exclude-env CODEX_API_KEY --exclude-env OPENAI_API_KEY \
  --exclude-env COPILOT_GITHUB_TOKEN --exclude-env GITHUB_TOKEN \
  --exclude-env MAINTAINER_ALERTS_TOKEN --exclude-env ALERT_TOKEN \
  --exclude-env GUMBOT_PRIVATE_KEY --exclude-env GH_AW_GITHUB_TOKEN \
  -- /bin/bash -c "bash /trusted-maintainer/check_maintainer_environment.sh $mode"
