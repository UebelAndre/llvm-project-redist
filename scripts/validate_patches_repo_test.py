"""Integration test: validate the actual releases/ directory in this repo."""

import os
import unittest
from pathlib import Path

from scripts.validate_patches import validate


def _find_releases_dir() -> Path:
    """Locate releases/ via runfiles (Bazel) or relative path (direct)."""
    runfiles_dir = os.environ.get("TEST_SRCDIR")
    if runfiles_dir:
        workspace = os.environ.get("TEST_WORKSPACE", "")
        return Path(runfiles_dir) / workspace / "releases"
    return Path(__file__).resolve().parent.parent / "releases"


RELEASES_DIR = _find_releases_dir()


class ValidatePatchesRepoTest(unittest.TestCase):
    def test_releases_directory_patches_are_valid(self):
        errors = validate(RELEASES_DIR)
        self.assertEqual(errors, [], f"Patch validation errors:\n" + "\n".join(errors))


if __name__ == "__main__":
    unittest.main()
