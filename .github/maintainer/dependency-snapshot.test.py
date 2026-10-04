import importlib.util
import json
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location(
    "snapshot", Path(__file__).with_name("dependency-snapshot.py")
)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class LockedDependencyTests(unittest.TestCase):
    def test_uv_retains_all_platform_versions_and_records_project_exclusion(self):
        data = {
            "version": 1,
            "revision": 3,
            "package": [
                {"name": "project", "version": "0", "source": {"virtual": "."}},
                *[
                    {
                        "name": "Some_Package",
                        "version": v,
                        "source": {"registry": "https://pypi.org/simple"},
                    }
                    for v in ["1.0", "2.0"]
                ],
            ],
        }
        resolved, excluded, limitations = module.uv_packages(data, "project")
        self.assertEqual(
            set(resolved), {"pkg:pypi/some-package@1.0", "pkg:pypi/some-package@2.0"}
        )
        self.assertEqual(excluded[0]["name"], "project")
        self.assertFalse(limitations)

    def test_new_manifest_without_covering_lock_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "Manifest without indexed lock"):
            module.manifest_coverage(
                ["uv.lock", "new/package.json"],
                {"uv.lock": {}},
                lambda p: b'{"name":"new"}',
            )

    def test_bun_requires_declared_and_locked_membership_or_own_lock(self):
        files = ["app/package.json", "app/new/package.json"]
        documents = {
            "app/package.json": {"name": "root", "workspaces": ["shared"]},
            "app/new/package.json": {"name": "new"},
        }
        locks = {
            "app/bun.lock": {
                "workspaces": {"": {"name": "root"}, "new": {"name": "new"}}
            }
        }

        def read(p):
            return json.dumps(documents[p]).encode()

        with self.assertRaisesRegex(ValueError, "workspace membership"):
            module.manifest_coverage(files, locks, read)
        documents["app/package.json"]["workspaces"] = ["new"]
        del locks["app/bun.lock"]["workspaces"]["new"]
        with self.assertRaisesRegex(ValueError, "workspace membership"):
            module.manifest_coverage(files, locks, read)
        locks["app/bun.lock"]["workspaces"]["new"] = {"name": "new"}
        module.manifest_coverage(files, locks, read)
        documents["app/package.json"]["workspaces"] = ["*", "!new"]
        with self.assertRaisesRegex(ValueError, "workspace membership"):
            module.manifest_coverage(files, locks, read)
        locks["app/new/bun.lock"] = {"workspaces": {"": {"name": "new"}}}
        module.manifest_coverage(files, locks, read)
        self.assertFalse(module.workspace_match("new/deep", "*"))
        self.assertTrue(module.workspace_match("packages/new", "packages/*"))

    def test_uv_requires_declared_and_locked_workspace_membership(self):
        files = ["pyproject.toml", "packages/member/pyproject.toml"]
        documents = {
            "pyproject.toml": '[project]\nname="root-example"\n',
            "packages/member/pyproject.toml": '[project]\nname="member-example"\n',
        }
        data = {
            "version": 1,
            "revision": 3,
            "manifest": {"members": ["member-example", "root-example"]},
            "package": [
                {"name": "root-example", "version": "0", "source": {"virtual": "."}},
                {
                    "name": "member-example",
                    "version": "0",
                    "source": {"virtual": "packages/member"},
                },
                {
                    "name": "registry-dependency",
                    "version": "1.0",
                    "source": {"registry": "https://pypi.org/simple"},
                },
            ],
        }
        locks = {"uv.lock": data}

        def read(p):
            return documents[p].encode()

        with self.assertRaisesRegex(ValueError, "workspace membership"):
            module.manifest_coverage(files, locks, read)
        documents["pyproject.toml"] += '[tool.uv.workspace]\nmembers=["packages/*"]\n'
        projects = module.manifest_coverage(files, locks, read)
        resolved, excluded, _ = module.uv_packages(
            data, workspace_projects=projects["uv.lock"]
        )
        self.assertEqual(len(resolved), 1)
        self.assertEqual(
            {p["name"] for p in excluded}, {"root-example", "member-example"}
        )
        data["manifest"]["members"] = ["root-example"]
        with self.assertRaisesRegex(ValueError, "workspace membership"):
            module.manifest_coverage(files, locks, read)
        data["manifest"]["members"].append("member-example")
        documents["pyproject.toml"] += 'exclude=["packages/member"]\n'
        with self.assertRaisesRegex(ValueError, "workspace membership"):
            module.manifest_coverage(files, locks, read)

    def test_uv_source_gap_and_future_format_fail_closed(self):
        for source in [
            {"git": "https://example.com/code"},
            {"registry": "https://private.example/simple"},
            {"editable": "../other"},
        ]:
            with self.assertRaisesRegex(ValueError, "Unsupported/private/VCS/local"):
                module.uv_packages(
                    {
                        "version": 1,
                        "package": [
                            {"name": "other", "version": "1", "source": source}
                        ],
                    }
                )
        with self.assertRaisesRegex(ValueError, "format"):
            module.uv_packages({"version": 2, "package": []})

    def test_cpu_build_retained_with_explicit_conservative_upstream_identity(self):
        resolved, excluded, limitations = module.uv_packages(
            {
                "version": 1,
                "package": [
                    {
                        "name": "torch",
                        "version": "2.11.0+cpu",
                        "source": {"registry": "https://download.pytorch.org/whl/cpu"},
                    }
                ],
            }
        )
        self.assertEqual(
            set(resolved), {"pkg:pypi/torch@2.11.0%2Bcpu", "pkg:pypi/torch@2.11.0"}
        )
        self.assertEqual(limitations[0]["locked_version"], "2.11.0+cpu")


if __name__ == "__main__":
    unittest.main()
