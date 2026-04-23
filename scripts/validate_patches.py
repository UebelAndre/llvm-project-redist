#!/usr/bin/env python3
"""Validate version directories under releases/.

Each releases/{version}/patches/ directory may contain patch files matching
the pattern NNN_description.patch (three-digit zero-padded prefix). This
module validates that:
  - All .patch files follow the NNN_*.patch naming convention.
  - Numbers start at 001 and are strictly sequential with no gaps or duplicates.
  - A presubmit.yml file is present in every non-empty version directory.

Usage:
    python3 validate_patches.py <releases_dir>
"""

import argparse
import re
import sys
from pathlib import Path

PATCH_RE = re.compile(r"^(\d{3})_.+\.patch$")

IGNORED_FILES = {".gitkeep"}


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("releases_dir", type=Path, help="Path to releases/ directory")
    return parser.parse_args()


def _is_non_empty(version_dir: Path) -> bool:
    """True if the directory has meaningful content beyond placeholders."""
    for item in version_dir.iterdir():
        if item.is_file() and item.name not in IGNORED_FILES:
            return True
        if item.is_dir() and any(item.iterdir()):
            return True
    return False


def validate_version(version_dir: Path) -> list[str]:
    """Validate a single version directory.

    Checks patches/ subdirectory for naming/sequencing and requires
    presubmit.yml when the directory has meaningful content.

    Returns a list of error strings (empty if valid).
    """
    errors: list[str] = []

    if _is_non_empty(version_dir) and not (version_dir / "presubmit.yml").exists():
        errors.append(f"{version_dir.name}: missing required presubmit.yml")

    patches_dir = version_dir / "patches"
    if not patches_dir.is_dir():
        return errors

    patches = sorted(p for p in patches_dir.iterdir() if p.suffix == ".patch")

    if not patches:
        return errors

    numbers: list[int] = []
    for patch in patches:
        m = PATCH_RE.match(patch.name)
        if not m:
            errors.append(
                f"{version_dir.name}/patches/{patch.name}: does not match NNN_description.patch"
            )
            continue
        numbers.append(int(m.group(1)))

    if len(numbers) != len(patches):
        return errors

    for i, n in enumerate(numbers):
        expected = i + 1
        if n != expected:
            errors.append(
                f"{version_dir.name}: expected patch {expected:03d} but found {n:03d} "
                f"(gap or out-of-order sequence)"
            )
            return errors

    return errors


def validate(releases_dir: Path) -> list[str]:
    """Validate all version directories under a releases/ root.

    Returns a list of error strings (empty if everything is valid).
    """
    errors: list[str] = []

    if not releases_dir.is_dir():
        return errors

    for entry in sorted(releases_dir.iterdir()):
        if not entry.is_dir():
            continue
        errors.extend(validate_version(entry))

    return errors


def main() -> None:
    args = parse_args()

    errors = validate(args.releases_dir)
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    else:
        print("All version directories valid.")


if __name__ == "__main__":
    main()
