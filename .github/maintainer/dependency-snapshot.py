"""Index committed locks without installing dependencies or running package scripts.

Format sources: docs.astral.sh/uv/concepts/projects/layout/#the-lockfile,
bun.sh/docs/pm/lockfile and oven-sh/bun/packages/bun-types/bun.d.ts (JSONC).
Submission contract: docs.github.com/en/rest/dependency-graph/dependency-submission.
"""

import hashlib
from fnmatch import fnmatchcase
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import PurePosixPath
from urllib.parse import quote


def git(*args):
    return subprocess.check_output(["git", *args])


def purl(ecosystem, name, version):
    if not isinstance(name, str) or not isinstance(version, str) or not version:
        raise ValueError("Missing locked package name/version")
    if ecosystem == "pypi":
        name = re.sub(r"[-_.]+", "-", name).lower()
    return f"pkg:{ecosystem}/{quote(name, safe='/')}@{quote(version, safe='')}"


def uv_packages(data, project=None, workspace_projects=None):
    if data.get("version") != 1 or data.get("revision", 0) > 3:
        raise ValueError("Unsupported uv lock format")
    resolved, excluded, limitations = {}, [], []
    local_projects = workspace_projects or ({".": project} if project else {})
    for package in data["package"]:
        name, version, source = (
            package["name"],
            package.get("version"),
            package["source"],
        )
        if len(source) == 1 and any(
            local_projects.get(source.get(kind)) == name
            for kind in ("virtual", "editable")
        ):
            excluded.append(
                {
                    "name": name,
                    "reason": "declared repository project/workspace, not a registry dependency",
                }
            )
            continue
        registry = source.get("registry")
        if source == {"registry": "https://pypi.org/simple"}:
            versions = [version]
        elif (
            source == {"registry": "https://download.pytorch.org/whl/cpu"}
            and name in {"torch", "torchvision"}
            and isinstance(version, str)
            and version.endswith("+cpu")
        ):
            # Retain the exact build AND conservatively monitor the upstream
            # public version. Local builds can contain different code (PEP 440).
            versions = [version, version.removesuffix("+cpu")]
            limitations.append(
                {
                    "name": name,
                    "locked_version": version,
                    "advisory_version": versions[1],
                    "reason": "CPU build also indexed as upstream; build-specific advisories outside GitHub npm/PyPI database are not covered",
                }
            )
        else:
            raise ValueError(
                f"Unsupported/private/VCS/local dependency source: {name}: {source}"
            )
        for indexed_version in versions:
            url = purl("pypi", name, indexed_version)
            resolved[url] = {
                "package_url": url,
                "metadata": {"registry": registry, "locked_version": version},
            }
    return resolved, excluded, limitations


def bun_packages(data):
    if data.get("lockfileVersion") not in {1, 2}:
        raise ValueError("Unsupported Bun lock format")
    resolved, excluded = {}, []
    workspaces = data["workspaces"]
    for key, value in data["packages"].items():
        descriptor = value[0]
        name, separator, version = descriptor.rpartition("@")
        if not separator:
            raise ValueError(f"Missing Bun package identity: {key}")
        if version.startswith("workspace:"):
            workspace = workspaces.get(version.removeprefix("workspace:"))
            if not workspace or workspace.get("name") != name or len(value) != 1:
                raise ValueError(f"Unrecognized local Bun source: {key}")
            excluded.append(
                {
                    "name": name,
                    "reason": "repository workspace, not a registry dependency",
                }
            )
            continue
        if not re.fullmatch(
            r"(?:@[a-z0-9._-]+/)?[a-z0-9._-]+", name
        ) or not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", version):
            raise ValueError(f"Unsupported/private/VCS Bun identity: {descriptor}")
        if (
            len(value) != 4
            or value[1] != ""
            or not isinstance(value[2], dict)
            or not re.fullmatch(r"sha(?:256|512)-[A-Za-z0-9+/=]+", value[3])
        ):
            raise ValueError(f"Unsupported/custom-registry Bun resolution: {key}")
        url = purl("npm", name, version)
        resolved[url] = {"package_url": url}
    return resolved, excluded, []


def workspace_match(path, pattern):
    """Match workspace globs by path component; * cannot cross directories."""

    def match(parts, patterns):
        if not patterns:
            return not parts
        if patterns[0] == "**":
            return (
                match(parts, patterns[1:]) or bool(parts) and match(parts[1:], patterns)
            )
        return (
            bool(parts)
            and fnmatchcase(parts[0], patterns[0])
            and match(parts[1:], patterns[1:])
        )

    return match(PurePosixPath(path).parts, PurePosixPath(pattern).parts)


def manifest_coverage(files, locks, read):
    """Require an own lock or membership declared in BOTH root and lock."""
    uv_projects = {}
    for path in files:
        filename = PurePosixPath(path).name
        if filename not in {"package.json", "pyproject.toml"}:
            continue
        is_bun = filename == "package.json"
        manifest = (
            json.loads(read(path)) if is_bun else tomllib.loads(read(path).decode())
        )
        name = (
            manifest.get("name") if is_bun else manifest.get("project", {}).get("name")
        )
        kind = "bun.lock" if is_bun else "uv.lock"
        directory = PurePosixPath(path).parent
        covered = False
        for parent in PurePosixPath(path).parents:
            lock_path = str(parent / kind)
            if lock_path not in locks:
                continue
            data = locks[lock_path]
            relative = str(directory.relative_to(parent))
            if is_bun:
                member = data.get("workspaces", {}).get(
                    "" if relative == "." else relative
                )
                root = json.loads(read(str(parent / filename)))
                patterns = root.get("workspaces", [])
                if not isinstance(patterns, list):
                    raise ValueError(f"Unsupported Bun workspace declaration: {path}")
                declared = relative == "." or (
                    any(
                        workspace_match(relative, p)
                        for p in patterns
                        if not p.startswith("!")
                    )
                    and not any(
                        workspace_match(relative, p[1:])
                        for p in patterns
                        if p.startswith("!")
                    )
                )
                covered = (
                    isinstance(name, str)
                    and declared
                    and member is not None
                    and member.get("name") == name
                )
            else:
                root = tomllib.loads(read(str(parent / filename)).decode())
                workspace = root.get("tool", {}).get("uv", {}).get("workspace", {})
                declared = relative == "." or (
                    any(
                        workspace_match(relative, p)
                        for p in workspace.get("members", [])
                    )
                    and not any(
                        workspace_match(relative, p)
                        for p in workspace.get("exclude", [])
                    )
                )
                member = any(
                    p["name"] == name
                    and p["source"] in ({"virtual": relative}, {"editable": relative})
                    for p in data["package"]
                )
                covered = (
                    isinstance(name, str)
                    and declared
                    and member
                    and (
                        relative == "."
                        or name in data.get("manifest", {}).get("members", [])
                    )
                )
                if covered:
                    uv_projects.setdefault(lock_path, {})[relative] = name
            # The nearest lock owns the project; don't escape to an outer lock.
            break
        if not covered:
            raise ValueError(
                f"Manifest without indexed lock/workspace membership: {path}"
            )
    return uv_projects


def snapshot(sha, env):
    if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
        raise ValueError("Use an exact source commit SHA")
    files = git("ls-tree", "-r", "--name-only", sha).decode().splitlines()
    unsupported = [
        p
        for p in files
        if PurePosixPath(p).name
        in {
            "bun.lockb",
            "package-lock.json",
            "yarn.lock",
            "pnpm-lock.yaml",
            "poetry.lock",
            "Pipfile.lock",
        }
    ]
    if unsupported:
        raise ValueError(f"Unindexed lock formats: {unsupported}")
    locks = [
        p
        for p in files
        if PurePosixPath(p).name in {"uv.lock", "bun.lock"} or p.endswith(".py.lock")
    ]
    if not locks:
        raise ValueError("No supported committed locks")

    def read(path):
        return git("show", f"{sha}:{path}")

    raw_locks = {path: read(path) for path in locks}
    parsed = {}
    for path, raw in raw_locks.items():
        if path.endswith("bun.lock"):
            if (
                subprocess.check_output(["bun", "--version"]).decode().strip()
                != "1.4.2"
            ):
                raise ValueError("Bun lock parser requires reviewed Bun 1.4.2")
            parsed[path] = json.loads(
                subprocess.check_output(
                    [
                        "bun",
                        "-e",
                        "console.log(JSON.stringify(Bun.JSONC.parse(await Bun.stdin.text())))",
                    ],
                    input=raw,
                )
            )
        else:
            parsed[path] = tomllib.loads(raw.decode())
    projects = manifest_coverage(files, parsed, read)
    manifests, inventory = {}, {}
    for path in locks:
        raw = raw_locks[path]
        if path.endswith("bun.lock"):
            directory = PurePosixPath(path).parent
            config = str(directory / "bunfig.toml")
            if config in files:
                install = tomllib.loads(git("show", f"{sha}:{config}").decode()).get(
                    "install", {}
                )
                if "registry" in install or "scopes" in install:
                    raise ValueError(
                        f"Custom/private Bun registry configuration: {config}"
                    )
            if any(PurePosixPath(p).name == ".npmrc" for p in files):
                raise ValueError("Review .npmrc registry configuration before indexing")
            resolved, excluded, limitations = bun_packages(parsed[path])
        else:
            resolved, excluded, limitations = uv_packages(
                parsed[path], workspace_projects=projects.get(path, {})
            )
        if not resolved:
            raise ValueError(f"Empty indexed dependency set: {path}")
        digest = hashlib.sha256(raw).hexdigest()
        manifests[path] = {
            "name": path,
            "file": {"source_location": path},
            "resolved": resolved,
            "metadata": {"lock_sha256": digest, "source_sha": sha},
        }
        inventory[path] = {
            "lock_sha256": digest,
            "count": len(resolved),
            "excluded": excluded,
            "limitations": limitations,
        }
    repository = env["GITHUB_REPOSITORY"]
    result = {
        "version": 0,
        "sha": sha,
        "ref": env["GITHUB_REF"],
        "job": {
            "id": env["GITHUB_RUN_ID"],
            "correlator": "maintainer-locked-dependencies",
            "html_url": f"https://github.com/{repository}/actions/runs/{env['GITHUB_RUN_ID']}",
        },
        "detector": {
            "name": "maintainer-locks",
            "version": "1",
            "url": "https://docs.github.com/en/rest/dependency-graph/dependency-submission",
        },
        "scanned": env["SCANNED_AT"],
        "manifests": manifests,
    }
    return {"snapshot": result, "inventory": inventory}


if __name__ == "__main__":
    print(json.dumps(snapshot(sys.argv[1], os.environ), sort_keys=True))
