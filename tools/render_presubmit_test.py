#!/usr/bin/env python3
"""Unit tests for render_presubmit.py."""

import tempfile
import unittest
from pathlib import Path

from tools.render_presubmit import (
    DEFAULT_LINUX_PLATFORMS,
    WINDOWS_EXCLUDED_TARGETS,
    WINDOWS_SHARDS,
    expand_config,
    flags_for,
    parse_bazelrc,
    render_presubmit,
)


class ParseBazelrcTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.rc = Path(self.tmpdir.name) / ".bazelrc"

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_unconditional_flag(self) -> None:
        self.rc.write_text("build --force_pic\n")
        configs = parse_bazelrc(self.rc)
        self.assertEqual(configs[None], ["--force_pic"])

    def test_named_config(self) -> None:
        self.rc.write_text("build:generic_clang --cxxopt=-std=c++17\n")
        configs = parse_bazelrc(self.rc)
        self.assertEqual(configs["generic_clang"], ["--cxxopt=-std=c++17"])

    def test_multiple_lines_aggregate(self) -> None:
        self.rc.write_text("build:generic_clang --cxxopt=-std=c++17\nbuild:generic_clang --copt=-Wall\n")
        configs = parse_bazelrc(self.rc)
        self.assertEqual(
            configs["generic_clang"],
            ["--cxxopt=-std=c++17", "--copt=-Wall"],
        )

    def test_multiple_flags_per_line(self) -> None:
        self.rc.write_text("build:generic_clang --cxxopt=-std=c++17 --host_cxxopt=-std=c++17\n")
        configs = parse_bazelrc(self.rc)
        self.assertEqual(
            configs["generic_clang"],
            ["--cxxopt=-std=c++17", "--host_cxxopt=-std=c++17"],
        )

    def test_skips_comments_and_blanks(self) -> None:
        self.rc.write_text("# header comment\n\nbuild --x\n    # indented comment\nbuild --y\n")
        configs = parse_bazelrc(self.rc)
        self.assertEqual(configs[None], ["--x", "--y"])

    def test_aggregates_common_and_build_and_test(self) -> None:
        self.rc.write_text("common:ci --a\nbuild:ci --b\ntest:ci --c\n")
        configs = parse_bazelrc(self.rc)
        self.assertEqual(configs["ci"], ["--a", "--b", "--c"])

    def test_ignores_run_and_query_commands(self) -> None:
        self.rc.write_text("run:ci --r\nquery:ci --q\nbuild:ci --b\n")
        configs = parse_bazelrc(self.rc)
        self.assertEqual(configs["ci"], ["--b"])

    def test_backslash_continuation(self) -> None:
        self.rc.write_text("build:ci --a \\\n  --b \\\n  --c\n")
        configs = parse_bazelrc(self.rc)
        self.assertEqual(configs["ci"], ["--a", "--b", "--c"])

    def test_inline_comment_after_flag(self) -> None:
        """Real bazelrc usage: ``build:msvc --copt=/WX  # Treat warnings as errors``."""
        self.rc.write_text(
            "build:msvc --copt=/WX --host_copt=/WX  # Treat warnings as errors...\n"
            "build:msvc --copt=/wd4141 # inline used more than once\n"
        )
        configs = parse_bazelrc(self.rc)
        # Comment content (`#`, `Treat`, `warnings`, etc.) MUST NOT leak in as flags.
        self.assertEqual(
            configs["msvc"],
            ["--copt=/WX", "--host_copt=/WX", "--copt=/wd4141"],
        )


class ExpandConfigTest(unittest.TestCase):
    def test_simple_no_inheritance(self) -> None:
        configs: dict[str | None, list[str]] = {"x": ["--a", "--b"]}
        self.assertEqual(expand_config(configs, "x"), ["--a", "--b"])

    def test_one_level_inheritance(self) -> None:
        configs: dict[str | None, list[str]] = {
            "base": ["--a"],
            "ci": ["--config=base", "--b"],
        }
        self.assertEqual(expand_config(configs, "ci"), ["--a", "--b"])

    def test_two_level_inheritance(self) -> None:
        configs: dict[str | None, list[str]] = {
            "base": ["--a"],
            "mid": ["--config=base", "--b"],
            "top": ["--config=mid", "--c"],
        }
        self.assertEqual(expand_config(configs, "top"), ["--a", "--b", "--c"])

    def test_unknown_config_errors(self) -> None:
        with self.assertRaises(KeyError):
            expand_config({"x": ["--a"]}, "missing")

    def test_cycle_detected(self) -> None:
        configs: dict[str | None, list[str]] = {
            "a": ["--config=b"],
            "b": ["--config=a"],
        }
        with self.assertRaises(ValueError):
            expand_config(configs, "a")


class FlagsForTest(unittest.TestCase):
    def test_includes_unconditional_then_config_then_common_then_invariants(self) -> None:
        configs: dict[str | None, list[str]] = {
            None: ["--unconditional"],
            "generic_clang": ["--cxxopt=-std=c++17"],
        }
        flags = flags_for(configs, "generic_clang")
        self.assertEqual(flags[0], "--unconditional")
        self.assertIn("--cxxopt=-std=c++17", flags)
        self.assertIn("--build_tag_filters=-nobuildkite", flags)
        self.assertIn("--incompatible_disallow_empty_glob=true", flags)
        # Invariants come last so they override anything earlier.
        self.assertEqual(flags[-1], "--incompatible_autoload_externally=")


class RenderPresubmitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.rc = Path(self.tmpdir.name) / ".bazelrc"
        # Minimal but realistic-shaped rc: every config the renderer asks
        # for must be present, plus an inheritance chain to verify expansion.
        self.rc.write_text(
            "build --force_pic\n"
            "\n"
            "build:generic_clang --cxxopt=-std=c++17\n"
            "build:generic_gcc --cxxopt=-std=c++17\n"
            "build:windows --cxxopt=/std:c++17\n"
            "build:clang-cl --config=windows\n"
            "build:clang-cl --compiler=clang-cl\n"
            "build:clang-cl --repo_env=USE_CLANG_CL=1\n"
            "build:msvc --config=windows\n"
            "build:msvc --copt=/WX\n"
        )

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_emits_all_expected_tasks(self) -> None:
        out = render_presubmit(self.rc, ["debian10"], ["8.x"])
        self.assertEqual(
            set(out["tasks"].keys()),
            {
                "run_tests",
                "run_tests_gcc",
                "run_tests_macos",
                "run_tests_macos_arm64",
                "run_tests_windows_clang_cl",
                "run_tests_windows_msvc",
            },
        )

    def test_clang_cl_inherits_windows(self) -> None:
        out = render_presubmit(self.rc, ["debian10"], ["8.x"])
        flags = out["tasks"]["run_tests_windows_clang_cl"]["test_flags"]
        # Inherited from `windows`:
        self.assertIn("--cxxopt=/std:c++17", flags)
        # Direct on `clang-cl`:
        self.assertIn("--compiler=clang-cl", flags)
        # NOT the --config=windows reference itself (it was expanded):
        self.assertNotIn("--config=windows", flags)

    def test_clang_cl_task_actually_switches_the_compiler(self) -> None:
        """The clang-cl task carries the flag that really selects clang-cl.

        ``--compiler=`` is a ``cc_toolchain_suite`` selector from Bazel's
        pre-platforms toolchain resolution and is inert under platform-based
        resolution, so on its own the "clang-cl" task builds with cl.exe.
        ``--repo_env=USE_CLANG_CL=1`` is what repoints rules_cc's Windows
        toolchain at clang-cl.exe, and it has to survive rendering.
        """
        out = render_presubmit(self.rc, ["debian10"], ["8.x"])
        flags = out["tasks"]["run_tests_windows_clang_cl"]["test_flags"]
        self.assertIn("--repo_env=USE_CLANG_CL=1", flags)
        # Only the clang-cl task — the MSVC task must stay on cl.exe.
        self.assertNotIn("--repo_env=USE_CLANG_CL=1", out["tasks"]["run_tests_windows_msvc"]["test_flags"])

    def test_msvc_inherits_windows(self) -> None:
        out = render_presubmit(self.rc, ["debian10"], ["8.x"])
        flags = out["tasks"]["run_tests_windows_msvc"]["test_flags"]
        self.assertIn("--cxxopt=/std:c++17", flags)
        self.assertIn("--copt=/WX", flags)

    def test_unconditional_flags_apply_to_every_task(self) -> None:
        out = render_presubmit(self.rc, ["debian10"], ["8.x"])
        for name, task in out["tasks"].items():
            with self.subTest(task=name):
                self.assertIn("--force_pic", task["test_flags"])

    def test_windows_tasks_skip_no_windows_tagged_targets(self) -> None:
        """Windows excludes ``no_windows`` on top of ``nobuildkite``.

        ``gentbl_test`` targets from mlir/tblgen.bzl are shell scripts that
        pass ``-o /dev/null``; upstream tags them ``no_windows`` but the tag
        does nothing unless a filter names it. Both filters must carry both
        tags in a single comma-joined value -- a second
        ``--test_tag_filters`` would replace the first, not add to it.
        """
        out = render_presubmit(self.rc, ["debian10"], ["8.x"])
        for name in ("run_tests_windows_clang_cl", "run_tests_windows_msvc"):
            with self.subTest(task=name):
                flags = out["tasks"][name]["test_flags"]
                self.assertIn("--build_tag_filters=-nobuildkite,-no_windows", flags)
                self.assertIn("--test_tag_filters=-nobuildkite,-no_windows", flags)
                self.assertEqual(1, sum(f.startswith("--test_tag_filters=") for f in flags))
                self.assertEqual(1, sum(f.startswith("--build_tag_filters=") for f in flags))

    def test_non_windows_tasks_do_not_skip_no_windows(self) -> None:
        """``no_windows`` is a Windows-only exclusion; it must not leak."""
        out = render_presubmit(self.rc, ["debian10"], ["8.x"])
        for name in ("run_tests", "run_tests_gcc", "run_tests_macos", "run_tests_macos_arm64"):
            with self.subTest(task=name):
                flags = out["tasks"][name]["test_flags"]
                self.assertIn("--build_tag_filters=-nobuildkite", flags)
                self.assertIn("--test_tag_filters=-nobuildkite", flags)
                self.assertNotIn("no_windows", " ".join(flags))

    def test_matrix_uses_provided_values(self) -> None:
        out = render_presubmit(self.rc, ["debian10", "ubuntu2404"], ["7.x", "9.x"])
        self.assertEqual(out["matrix"]["platform"], ["debian10", "ubuntu2404"])
        self.assertEqual(out["matrix"]["bazel"], ["7.x", "9.x"])

    def test_default_linux_platforms_are_not_bazelci_eol(self) -> None:
        """No default matrix platform may be one bazelci has retired.

        bazelci keeps retired names in ``EOL_PLATFORMS`` (bazelci.py); they
        stay resolvable from the artifact registry, so a job scheduled on one
        does not fail loudly -- it just runs on an image nobody patches any
        more. Pin the list here so a retirement is caught by a test rather
        than by a stale CVE.
        """
        eol = {
            "ubuntu1604",
            "ubuntu1804",
            "ubuntu2004",
            "ubuntu2004_arm64",
            "ubuntu2004_java11",
            "kythe_ubuntu2004",
        }
        self.assertEqual(set(), eol & set(DEFAULT_LINUX_PLATFORMS))

    def test_linux_clang_task_installs_lld(self) -> None:
        """The linux clang task runs `apt-get install lld` so `-fuse-ld=lld` resolves."""
        out = render_presubmit(self.rc, ["debian10"], ["8.x"])
        shell_commands = out["tasks"]["run_tests"].get("shell_commands", [])
        self.assertTrue(any("lld" in cmd for cmd in shell_commands))
        self.assertTrue(any("apt-get" in cmd for cmd in shell_commands))

    def test_other_tasks_have_no_shell_commands(self) -> None:
        """shell_commands is omitted (not just empty) on tasks that don't need it."""
        out = render_presubmit(self.rc, ["debian10"], ["8.x"])
        for name in ("run_tests_gcc", "run_tests_windows_clang_cl", "run_tests_windows_msvc"):
            with self.subTest(task=name):
                self.assertNotIn("shell_commands", out["tasks"][name])

    def test_windows_tasks_override_force_pic_rather_than_dropping_it(self) -> None:
        """Windows keeps ``--force_pic`` and lets ``--noforce_pic`` win.

        Bazel's auto-configured Windows toolchain never declares the
        ``supports_pic`` feature, so ``--force_pic`` aborts analysis
        outright for both MSVC and clang-cl. The fix belongs in the
        ``.bazelrc`` (``build:windows --noforce_pic``, mirroring CMake's
        ``WIN32`` carve-out in ``HandleLLVMOptions.cmake``), not in this
        renderer — so the unconditional ``--force_pic`` stays in the list
        and the later, Windows-specific negation overrides it.
        """
        self.rc.write_text(self.rc.read_text() + "build:windows --noforce_pic\n")
        out = render_presubmit(self.rc, ["debian10"], ["8.x"])
        for name in ("run_tests_windows_clang_cl", "run_tests_windows_msvc"):
            with self.subTest(task=name):
                flags = out["tasks"][name]["test_flags"]
                self.assertIn("--force_pic", flags)
                self.assertIn("--noforce_pic", flags)
                # Bazel takes the last occurrence, so the negation must
                # come after the unconditional flag it cancels.
                self.assertGreater(flags.index("--noforce_pic"), flags.index("--force_pic"))
        # Non-Windows tasks see only the unconditional --force_pic.
        for name in ("run_tests", "run_tests_gcc", "run_tests_macos"):
            with self.subTest(task=name):
                flags = out["tasks"][name]["test_flags"]
                self.assertIn("--force_pic", flags)
                self.assertNotIn("--noforce_pic", flags)

    def test_windows_tasks_keep_default_runfiles_behavior(self) -> None:
        """No task overrides Windows' default runfiles behavior.

        Windows defaults to copied runfiles (no symlink trees) and
        ``--build_python_zip``. Redistributing llvm-project means testing
        the configuration downstream consumers actually get, so these
        defaults are deliberately left alone even though they cost disk.

        That includes ``--windows_enable_symlinks``, which would make the
        runfiles trees symlink farms instead of copies (531 MB -> 912 KB
        for one MLIR lit test, measured on 9.2.0). It fixes the "not
        enough space on the disk" failures, but it is a startup option, so
        the only way to set it under bazelci is to append it to the
        workspace bazelrc from ``batch_commands`` -- and a redistribution
        should not be quietly testing itself under a non-default Windows
        filesystem mode. No task emits ``batch_commands`` at all.

        ``--enable_runfiles=yes`` is excluded for the same reason, even
        though it would also shorten every test path by the 16 characters
        rules_python's per-target transition costs. The two lit tests that
        overrun the `CreateProcessW` cwd limit are dropped instead -- see
        ``test_windows_tasks_drop_overlong_lit_tests``.

        The disk cost is paid with shards instead -- see
        ``test_windows_tasks_are_sharded``.
        """
        out = render_presubmit(self.rc, ["debian10"], ["8.x"])
        for name, task in out["tasks"].items():
            with self.subTest(task=name):
                flags = task["test_flags"]
                self.assertNotIn("--enable_runfiles", flags)
                self.assertNotIn("--enable_runfiles=yes", flags)
                self.assertNotIn("--nobuild_python_zip", flags)
                self.assertNotIn("batch_commands", task)

    def test_windows_tasks_drop_overlong_lit_tests(self) -> None:
        """Only the Windows tasks exclude the two over-long lit tests.

        lit sets each RUN line's cwd to the test's exec directory, and
        ``CreateProcessW`` rejects an ``lpCurrentDirectory`` over 258
        characters outright. These two MLIR tests are the only ones in the
        suite past that line; everywhere the path fits, they pass. bazelci
        reads a leading ``-`` as an exclusion when it expands
        ``test_targets`` via ``bazel query``.
        """
        out = render_presubmit(self.rc, ["debian10"], ["8.x"])
        for name, task in out["tasks"].items():
            with self.subTest(task=name):
                targets = task["test_targets"]
                self.assertEqual(targets[0], "@llvm-project//...")
                expected = WINDOWS_EXCLUDED_TARGETS if task["platform"] == "windows" else []
                self.assertEqual(targets[1:], expected)
                for target in targets[1:]:
                    self.assertTrue(target.startswith("-@llvm-project//"))

    def test_windows_tasks_are_sharded(self) -> None:
        """Only the Windows tasks shard, and they shard identically.

        Sharding is how the copied-runfiles disk cost is absorbed without
        changing the runfiles mode: each shard lands on its own agent and
        materializes only ``sorted(test_targets)[shard_id::shard_count]``.
        No other platform needs it -- Linux and macOS use symlink trees --
        and spending extra agents there would buy nothing.
        """
        out = render_presubmit(self.rc, ["debian10"], ["8.x"])
        for name, task in out["tasks"].items():
            with self.subTest(task=name):
                if task["platform"] == "windows":
                    self.assertEqual(task["shards"], WINDOWS_SHARDS)
                    self.assertGreater(task["shards"], 1)
                else:
                    self.assertNotIn("shards", task)

    def test_clang_cl_gets_its_own_bazel_matrix_attribute(self) -> None:
        """Bazel versions clang-cl can't run on are dropped from that task only.

        ``matrix.exclude`` is keyed on matrix attributes with no notion of
        which task is being expanded, so excluding 7.x there would take it
        away from Linux and msvc too. A second attribute scopes it.
        """
        out = render_presubmit(self.rc, ["debian10"], ["7.x", "8.x", "9.x"])
        self.assertEqual(out["matrix"]["bazel"], ["7.x", "8.x", "9.x"])
        self.assertEqual(out["matrix"]["bazel_clang_cl"], ["8.x", "9.x"])
        self.assertEqual(out["tasks"]["run_tests_windows_clang_cl"]["bazel"], "${{ bazel_clang_cl }}")
        for name, task in out["tasks"].items():
            if name != "run_tests_windows_clang_cl":
                with self.subTest(task=name):
                    self.assertEqual(task["bazel"], "${{ bazel }}")

    def test_no_extra_matrix_attribute_when_nothing_is_excluded(self) -> None:
        """With no unsupported version in play, clang-cl stays on ``${{ bazel }}``.

        A ``bazel_clang_cl`` identical to ``bazel`` would be noise, and an
        empty one would silently expand to zero tasks.
        """
        out = render_presubmit(self.rc, ["debian10"], ["8.x", "9.x"])
        self.assertNotIn("bazel_clang_cl", out["matrix"])
        self.assertEqual(out["tasks"]["run_tests_windows_clang_cl"]["bazel"], "${{ bazel }}")

    def test_all_versions_unsupported_leaves_the_task_on_the_default_matrix(self) -> None:
        """Filtering everything out must not produce an empty matrix attribute.

        bazelci expands a task once per combination, so an empty list would
        drop ``run_tests_windows_clang_cl`` from the pipeline with no
        indication that it had been dropped. Running it and failing is the
        more honest outcome.
        """
        out = render_presubmit(self.rc, ["debian10"], ["7.x"])
        self.assertNotIn("bazel_clang_cl", out["matrix"])
        self.assertEqual(out["tasks"]["run_tests_windows_clang_cl"]["bazel"], "${{ bazel }}")

    def test_macos_tasks_drop_fuse_ld_lld(self) -> None:
        """macOS clang tasks strip ``-fuse-ld=lld`` (Apple's ld64 is used instead)."""
        # Point generic_clang at the flag we expect to be stripped.
        self.rc.write_text(
            self.rc.read_text() + "build:generic_clang --linkopt=-fuse-ld=lld --host_linkopt=-fuse-ld=lld\n"
        )
        out = render_presubmit(self.rc, ["debian10"], ["8.x"])
        for name in ("run_tests_macos", "run_tests_macos_arm64"):
            with self.subTest(task=name):
                flags = out["tasks"][name]["test_flags"]
                self.assertNotIn("--linkopt=-fuse-ld=lld", flags)
                self.assertNotIn("--host_linkopt=-fuse-ld=lld", flags)
        # Linux clang keeps the flag — it installs lld via shell_commands.
        linux_flags = out["tasks"]["run_tests"]["test_flags"]
        self.assertIn("--linkopt=-fuse-ld=lld", linux_flags)


if __name__ == "__main__":
    unittest.main()
