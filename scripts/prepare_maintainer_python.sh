#!/usr/bin/env bash
# AWF mounts RUNNER_TOOL_CACHE read-only at the same path after replacing HOME.
set -euo pipefail
test "$(uv --version | awk '{print $2}')" = "$(cat .uv-version)"
export UV_PYTHON_INSTALL_DIR="${RUNNER_TOOL_CACHE:?RUNNER_TOOL_CACHE must be set}/uv-python"
uv python install "$(cat .python-version)"
uv python find --managed-python --no-python-downloads "$(cat .python-version)"
printf 'UV_PYTHON_INSTALL_DIR=%s\n' "$UV_PYTHON_INSTALL_DIR" >> "${GITHUB_ENV:?GITHUB_ENV must be set}"
