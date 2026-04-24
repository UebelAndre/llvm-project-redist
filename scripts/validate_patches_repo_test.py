"""Integration test: validate the actual versions/ directory in this repo."""

import os
import unittest
from pathlib import Path

from scripts.validate_patches import validate


def _find_versions_dir() -> Path:
    """Locate versions/ via runfiles (Bazel) or relative path (direct)."""
    runfiles_dir = os.environ.get("TEST_SRCDIR")
    if runfiles_dir:
        workspace = os.environ.get("TEST_WORKSPACE", "")
        return Path(runfiles_dir) / workspace / "versions"
    return Path(__file__).resolve().parent.parent / "versions"


VERSIONS_DIR = _find_versions_dir()


class ValidatePatchesRepoTest(unittest.TestCase):
    def test_versions_directory_patches_are_valid(self):
        errors = validate(VERSIONS_DIR)
        self.assertEqual(errors, [], f"Patch validation errors:\n" + "\n".join(errors))


if __name__ == "__main__":
    unittest.main()
