"""Normalize pinned compiler images, dependencies, and publisher sanitizer parity."""

import json
import re
import shlex
from pathlib import Path

root = Path(__file__).resolve().parents[2]
path = root / ".github/workflows/pr-review.lock.yml"
source = (root / ".github/workflows/pr-review.md").read_text()
lock = path.read_text()
pins = dict(re.findall(r"      (agent|apiProxy|squid): (ghcr.io/[^\n]+)", source))
if len(pins) != 3:
    raise SystemExit("Expected three source firewall image pins")
start = lock.index("  detection:\n")
prefix, detector = lock[:start], lock[start:]
for image in pins.values():
    detector = re.sub(re.escape(image.split("@")[0]) + r"(?!@)", image, detector)
lines = detector.splitlines(keepends=True)
for index, line in enumerate(lines):
    if (
        line.strip().startswith("printf")
        and "awf-config.json" in line
        and "network" in line
    ):
        config = json.loads(shlex.split(line)[2])
        config["container"].pop("imageTag", None)
        config["container"]["images"] = pins
        encoded = json.dumps(config, separators=(",", ":"))
        lines[index] = (
            "          printf '%s\\n' '"
            + encoded
            + '\' > "${RUNNER_TEMP}/gh-aw/awf-config.json"\n'
        )
        break
else:
    raise SystemExit("Expected native detection configuration")
lock = prefix + "".join(lines)
lock = lock.replace(
    "      - pre_activation\n      - pre_activation\n", "      - pre_activation\n"
)
lock = lock.replace(
    "\non:\n",
    "\n# Metadata only: trusted policy checkout, read-only inference, isolated publisher.\n"
    "on: # zizmor: ignore[dangerous-triggers]\n",
    1,
)
# Prompt generation must read the same admitted immutable policy as inference.
activation_start = lock.index("  activation:\n")
activation_end = lock.index("\n  agent:\n", activation_start)
activation = lock[activation_start:activation_end]
checkout = "      - name: Checkout .github and .agents folders\n"
checkout_start = activation.index(checkout)
checkout_with = activation.index("        with:\n", checkout_start) + len(
    "        with:\n"
)
checkout_end = activation.find("      - ", checkout_with)
if checkout_end < 0:
    checkout_end = len(activation)
checkout_options = activation[checkout_with:checkout_end]
policy_ref = "          ref: ${{ needs.pre_activation.outputs.policy }}\n"
if re.search(r"^          ref: .+\n", checkout_options, re.MULTILINE):
    checkout_options = re.sub(
        r"^          ref: .+\n",
        lambda _: policy_ref,
        checkout_options,
        count=1,
        flags=re.MULTILINE,
    )
else:
    checkout_options = policy_ref + checkout_options
activation = activation[:checkout_with] + checkout_options + activation[checkout_end:]
lock = lock[:activation_start] + activation + lock[activation_end:]
# The pinned compiler redundantly grants GITHUB_TOKEN PR write even when every
# write handler uses the separate App token. Reusable callers stay read-only.
for job_name in ("safe_outputs", "conclusion"):
    match = re.search(
        rf"^  {job_name}:\n[\s\S]*?(?=^  [a-z_]+:\n|\Z)", lock, re.MULTILINE
    )
    if not match:
        raise SystemExit(f"Expected native {job_name} job")
    job = match[0]
    if "github-token: ${{ steps.safe-outputs-app-token.outputs.token }}" not in job:
        raise SystemExit(f"Expected isolated App publication in {job_name}")
    readonly, count = re.subn(
        r"^      pull-requests: write$",
        "      pull-requests: read",
        job,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise SystemExit(f"Expected native redundant token permission in {job_name}")
    lock = lock[: match.start()] + readonly + lock[match.end() :]
# The trusted guard hashes exactly the text produced by the pinned native publisher.
# Copy its sanitizer environment instead of maintaining a second domain allowlist.
publisher_start = lock.index(
    "      - name: Process Safe Outputs\n", lock.index("  safe_outputs:\n")
)
publisher_env = lock[publisher_start : lock.index("        with:\n", publisher_start)]
sanitizer_env = re.findall(
    r"^          (GH_AW_ALLOWED_DOMAINS|GITHUB_SERVER_URL|GITHUB_API_URL): (.+)$",
    publisher_env,
    re.MULTILINE,
)
if len(sanitizer_env) != 3:
    raise SystemExit("Expected native publisher sanitizer environment")
guard_start = lock.index("      - name: Validate review before native publication\n")
guard_end = lock.index("        with:\n", guard_start)
lock = (
    lock[:guard_end]
    + "".join(f"          {key}: {value}\n" for key, value in sanitizer_env)
    + lock[guard_end:]
)
path.write_text(lock)
