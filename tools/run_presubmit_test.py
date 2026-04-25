"""Unit tests for ``presubmit_logic`` and ``run_presubmit`` helpers."""

from __future__ import annotations

import unittest

from tools import presubmit_logic as pl
from tools import run_presubmit as rp


class PresubmitLogicTest(unittest.TestCase):
    def test_platform_family(self) -> None:
        self.assertEqual(pl.platform_family("debian10"), "linux")
        self.assertEqual(pl.platform_family("ubuntu2204"), "linux")
        self.assertEqual(pl.platform_family("macos"), "macos")
        self.assertEqual(pl.platform_family("macos_arm64"), "macos")
        self.assertEqual(pl.platform_family("windows"), "windows")

    def test_agent_queue(self) -> None:
        self.assertEqual(pl.agent_queue("debian10"), "default")
        self.assertEqual(pl.agent_queue("macos"), "macos")
        self.assertEqual(pl.agent_queue("macos_arm64"), "macos_arm64")
        self.assertEqual(pl.agent_queue("windows"), "windows")

    def test_rewrite_llvm_project_label(self) -> None:
        self.assertEqual(
            pl.rewrite_llvm_project_label("@llvm-project//llvm/foo"),
            "//llvm/foo",
        )
        self.assertEqual(
            pl.rewrite_llvm_project_label("@other//foo"),
            "@other//foo",
        )

    def test_versions_from_git_diff_lines(self) -> None:
        out = """versions/17.0.3/presubmit.yml
versions/17.0.3/version.txt
README.md
versions/20.1.0/patches/001_x.patch
"""
        self.assertEqual(pl.versions_from_git_diff_lines(out), ["17.0.3", "20.1.0"])

    def test_expand_matrix_cartesian(self) -> None:
        doc = {
            "matrix": {
                "platform": ["debian10", "ubuntu2004"],
                "bazel": ["7.x", "8.x"],
            },
            "tasks": {
                "run_tests": {
                    "name": "t",
                    "platform": "${{ platform }}",
                    "bazel": "${{ bazel }}",
                    "test_targets": ["@llvm-project//llvm:all"],
                }
            },
        }
        expanded = pl.expand_presubmit(doc)
        self.assertEqual(len(expanded), 4)
        platforms = {e.platform for e in expanded}
        bazel = {e.bazel for e in expanded}
        self.assertEqual(platforms, {"debian10", "ubuntu2004"})
        self.assertEqual(bazel, {"7.x", "8.x"})

    def test_expand_fixed_platform_matrix_bazel(self) -> None:
        doc = {
            "matrix": {"bazel": ["7.x", "8.x"]},
            "tasks": {
                "run_tests_macos": {
                    "platform": "macos",
                    "bazel": "${{ bazel }}",
                    "test_targets": ["//x"],
                }
            },
        }
        expanded = pl.expand_presubmit(doc)
        self.assertEqual(len(expanded), 2)
        self.assertTrue(all(e.platform == "macos" for e in expanded))
        self.assertEqual({e.bazel for e in expanded}, {"7.x", "8.x"})

    def test_validate_missing_bazel(self) -> None:
        doc = {"tasks": {"t": {"platform": "debian10"}}}
        errs = pl.validate_presubmit(doc)
        self.assertTrue(any("missing required field `bazel`" in e for e in errs))

    def test_validate_unknown_platform(self) -> None:
        doc = {
            "tasks": {
                "t": {
                    "platform": "unknown-os",
                    "bazel": "7.x",
                    "test_targets": ["//a"],
                }
            },
        }
        errs = pl.validate_presubmit(doc)
        self.assertTrue(any("unknown platform" in e for e in errs))

    def test_find_expanded_task(self) -> None:
        doc = {
            "matrix": {"platform": ["debian10"], "bazel": ["7.x"]},
            "tasks": {
                "run_tests": {
                    "platform": "${{ platform }}",
                    "bazel": "${{ bazel }}",
                    "test_targets": ["//a"],
                }
            },
        }
        expanded = pl.expand_presubmit(doc)
        et = pl.find_expanded_task(expanded, "run_tests", "debian10", "7.x")
        self.assertEqual(et.task_id, "run_tests")

    def test_bcr_test_module_tasks(self) -> None:
        doc = {
            "bcr_test_module": {
                "module_path": "",
                "matrix": {
                    "platform": ["ubuntu2204"],
                    "bazel": ["8.x"],
                },
                "tasks": {
                    "verify": {
                        "platform": "${{ platform }}",
                        "bazel": "${{ bazel }}",
                        "build_targets": ["//tools:all"],
                    }
                },
            }
        }
        errs = pl.validate_presubmit(doc)
        self.assertEqual(errs, [])
        expanded = pl.expand_presubmit(doc)
        self.assertEqual(len(expanded), 1)
        self.assertEqual(expanded[0].task_id, "verify")

    def test_filter_by_platform_family(self) -> None:
        doc = {
            "tasks": {
                "linux": {"platform": "debian10", "bazel": "8.x", "test_targets": ["//a"]},
                "mac": {"platform": "macos", "bazel": "8.x", "test_targets": ["//b"]},
            }
        }
        expanded = pl.expand_presubmit(doc)
        linux_only = pl.filter_by_platform_family(expanded, "linux")
        self.assertEqual(len(linux_only), 1)
        self.assertEqual(linux_only[0].task_id, "linux")


class RunPresubmitPipelineTest(unittest.TestCase):
    def test_build_step_commands(self) -> None:
        from pathlib import Path

        repo = Path("/tmp/repo")
        et = pl.ExpandedTask(
            task_id="run_tests",
            platform="debian10",
            bazel="7.x",
            name="n",
            build_targets=(),
            test_targets=("//a",),
            build_flags=(),
            test_flags=("--verbose_failures",),
        )
        cmds = rp._build_step_commands(
            repo,
            "17.0.3",
            "17.0.3.bcr.1",
            Path("versions/17.0.3/presubmit.yml"),
            et,
            "llvm-project-17.0.3.bcr.1.bzl",
        )
        self.assertTrue(any("bazel run //tools:build" in c for c in cmds))
        self.assertTrue(any("bazel run //tools:run_presubmit" in c for c in cmds))
        self.assertTrue(any("--run-task=run_tests" in c for c in cmds))


if __name__ == "__main__":
    unittest.main()
