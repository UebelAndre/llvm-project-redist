#!/usr/bin/env python3
"""Unit tests for render_presubmit.py."""

import tempfile
import unittest
from pathlib import Path

import yaml

from tools.render_presubmit import (
    expand_config,
    parse_bazelrc,
    render_template,
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


class RenderTemplateTest(unittest.TestCase):
    """`--config=NAME` entries become the .bazelrc flags; nothing else moves."""

    configs = {
        None: ["--unconditional"],
        "base": ["--from-base"],
        "derived": ["--config=base", "--from-derived"],
    }

    def test_expands_config_with_unconditional_flags_once_per_list(self) -> None:
        template = (
            "tasks:\n"
            "  t:\n"
            "    test_flags:\n"
            "      - --config=derived\n"
            "      - --literal\n"
            "      - --config=base\n"
            "    test_targets:\n"
            "      - '//...'\n"
        )
        out = render_template(template, self.configs)
        flags = yaml.safe_load(out)["tasks"]["t"]["test_flags"]
        self.assertEqual(flags, ["--unconditional", "--from-base", "--from-derived", "--literal", "--from-base"])
        self.assertIn("# .bazelrc: --config=derived", out)

    def test_each_flag_list_gets_unconditional_flags(self) -> None:
        template = "tasks:\n  a:\n    test_flags:\n    - --config=base\n  b:\n    test_flags:\n    - --config=base\n"
        doc = yaml.safe_load(render_template(template, self.configs))
        self.assertEqual(doc["tasks"]["a"]["test_flags"], ["--unconditional", "--from-base"])
        self.assertEqual(doc["tasks"]["b"]["test_flags"], ["--unconditional", "--from-base"])

    def test_comments_and_other_keys_are_copied_verbatim(self) -> None:
        template = (
            "# keep me\n"
            "tasks:\n"
            "  t:\n"
            "    name: bazel test //... (x)  # and me\n"
            "    test_flags:\n"
            "      # inside the list\n"
            "      - --config=base\n"
        )
        out = render_template(template, self.configs)
        for line in ("# keep me", "    name: bazel test //... (x)  # and me", "      # inside the list"):
            self.assertIn(line, out)

    def test_unknown_config_errors(self) -> None:
        with self.assertRaises(SystemExit):
            render_template("tasks:\n  t:\n    test_flags:\n      - --config=nope\n", self.configs)

    def test_flags_needing_quotes_survive_a_yaml_round_trip(self) -> None:
        configs = {None: [], "q": ['--copt=-DX="a b"', "--action_env=FOO=bar baz"]}
        doc = yaml.safe_load(render_template("tasks:\n  t:\n    test_flags:\n      - --config=q\n", configs))
        self.assertEqual(doc["tasks"]["t"]["test_flags"], configs["q"])


if __name__ == "__main__":
    unittest.main()
