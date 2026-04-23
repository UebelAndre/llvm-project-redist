#!/usr/bin/env python3
"""Unit tests for validate_patches.py."""

import tempfile
import unittest
from pathlib import Path

from scripts.validate_patches import validate, validate_version


class ValidateVersionTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.version_dir = Path(self.tmpdir.name) / "20.0.0"
        self.version_dir.mkdir()
        self.patches_dir = self.version_dir / "patches"

    def tearDown(self):
        self.tmpdir.cleanup()

    def _add_presubmit(self):
        (self.version_dir / "presubmit.yml").write_text("tasks: {}\n")

    def _add_patch(self, name):
        self.patches_dir.mkdir(exist_ok=True)
        (self.patches_dir / name).touch()

    def test_empty_dir_is_valid(self):
        self.assertEqual(validate_version(self.version_dir), [])

    def test_gitkeep_only_is_valid(self):
        (self.version_dir / ".gitkeep").touch()
        self.assertEqual(validate_version(self.version_dir), [])

    def test_single_patch_starting_at_001(self):
        self._add_presubmit()
        self._add_patch("001_fix_build.patch")
        self.assertEqual(validate_version(self.version_dir), [])

    def test_sequential_patches(self):
        self._add_presubmit()
        self._add_patch("001_first.patch")
        self._add_patch("002_second.patch")
        self._add_patch("003_third.patch")
        self.assertEqual(validate_version(self.version_dir), [])

    def test_gap_in_sequence(self):
        self._add_presubmit()
        self._add_patch("001_first.patch")
        self._add_patch("003_third.patch")
        errors = validate_version(self.version_dir)
        self.assertEqual(len(errors), 1)
        self.assertIn("expected patch 002", errors[0])

    def test_not_starting_at_001(self):
        self._add_presubmit()
        self._add_patch("002_second.patch")
        errors = validate_version(self.version_dir)
        self.assertEqual(len(errors), 1)
        self.assertIn("expected patch 001", errors[0])

    def test_bad_naming_no_prefix(self):
        self._add_presubmit()
        self._add_patch("fix.patch")
        errors = validate_version(self.version_dir)
        self.assertEqual(len(errors), 1)
        self.assertIn("does not match NNN_description.patch", errors[0])

    def test_bad_naming_two_digit_prefix(self):
        self._add_presubmit()
        self._add_patch("01_fix.patch")
        errors = validate_version(self.version_dir)
        self.assertEqual(len(errors), 1)
        self.assertIn("does not match", errors[0])

    def test_bad_naming_no_underscore(self):
        self._add_presubmit()
        self._add_patch("001fix.patch")
        errors = validate_version(self.version_dir)
        self.assertEqual(len(errors), 1)
        self.assertIn("does not match", errors[0])

    def test_non_patch_files_in_patches_dir_ignored(self):
        self._add_presubmit()
        self.patches_dir.mkdir(exist_ok=True)
        (self.patches_dir / "README.md").touch()
        self._add_patch("001_fix.patch")
        self.assertEqual(validate_version(self.version_dir), [])

    def test_duplicate_numbers(self):
        self._add_presubmit()
        self._add_patch("001_alpha.patch")
        self._add_patch("001_bravo.patch")
        errors = validate_version(self.version_dir)
        self.assertEqual(len(errors), 1)
        self.assertIn("expected patch 002", errors[0])

    def test_missing_presubmit_with_patches(self):
        self._add_patch("001_fix.patch")
        errors = validate_version(self.version_dir)
        self.assertTrue(any("missing required presubmit.yml" in e for e in errors))

    def test_missing_presubmit_with_source_sha256(self):
        (self.version_dir / "source.sha256").write_text("abc123\n")
        errors = validate_version(self.version_dir)
        self.assertEqual(len(errors), 1)
        self.assertIn("missing required presubmit.yml", errors[0])

    def test_presubmit_only_is_valid(self):
        self._add_presubmit()
        self.assertEqual(validate_version(self.version_dir), [])

    def test_no_patches_dir_is_valid(self):
        self._add_presubmit()
        (self.version_dir / "source.sha256").write_text("abc123\n")
        self.assertEqual(validate_version(self.version_dir), [])

    def test_empty_patches_dir_is_valid(self):
        self._add_presubmit()
        self.patches_dir.mkdir()
        self.assertEqual(validate_version(self.version_dir), [])

    def test_error_includes_patches_subpath(self):
        self._add_presubmit()
        self._add_patch("fix.patch")
        errors = validate_version(self.version_dir)
        self.assertIn("patches/fix.patch", errors[0])


class ValidateTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.releases_dir = Path(self.tmpdir.name) / "releases"
        self.releases_dir.mkdir()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_empty_releases_dir(self):
        self.assertEqual(validate(self.releases_dir), [])

    def test_nonexistent_dir(self):
        absent = Path(self.tmpdir.name) / "nope"
        self.assertEqual(validate(absent), [])

    def test_valid_multiple_versions(self):
        v1 = self.releases_dir / "20.0.0"
        v1.mkdir()
        (v1 / "presubmit.yml").write_text("tasks: {}\n")
        (v1 / "patches").mkdir()
        (v1 / "patches" / "001_fix.patch").touch()

        v2 = self.releases_dir / "21.0.0"
        v2.mkdir()
        (v2 / "presubmit.yml").write_text("tasks: {}\n")
        (v2 / "patches").mkdir()
        (v2 / "patches" / "001_a.patch").touch()
        (v2 / "patches" / "002_b.patch").touch()

        self.assertEqual(validate(self.releases_dir), [])

    def test_errors_across_versions(self):
        v1 = self.releases_dir / "20.0.0"
        v1.mkdir()
        (v1 / "presubmit.yml").write_text("tasks: {}\n")
        (v1 / "patches").mkdir()
        (v1 / "patches" / "002_bad_start.patch").touch()

        v2 = self.releases_dir / "21.0.0"
        v2.mkdir()
        (v2 / "presubmit.yml").write_text("tasks: {}\n")
        (v2 / "patches").mkdir()
        (v2 / "patches" / "bad.patch").touch()

        errors = validate(self.releases_dir)
        self.assertEqual(len(errors), 2)

    def test_ignores_files_in_releases_root(self):
        (self.releases_dir / ".gitkeep").touch()
        self.assertEqual(validate(self.releases_dir), [])

    def test_missing_presubmit_across_versions(self):
        v1 = self.releases_dir / "20.0.0"
        v1.mkdir()
        (v1 / "patches").mkdir()
        (v1 / "patches" / "001_fix.patch").touch()

        v2 = self.releases_dir / "21.0.0"
        v2.mkdir()
        (v2 / "presubmit.yml").write_text("tasks: {}\n")
        (v2 / "patches").mkdir()
        (v2 / "patches" / "001_a.patch").touch()

        errors = validate(self.releases_dir)
        self.assertEqual(len(errors), 1)
        self.assertIn("20.0.0", errors[0])
        self.assertIn("missing required presubmit.yml", errors[0])


if __name__ == "__main__":
    unittest.main()
