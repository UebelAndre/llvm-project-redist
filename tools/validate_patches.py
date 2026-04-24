#!/usr/bin/env python3
"""Validate version directories under versions/.

Each versions/{version}/ directory may contain:
  - ``version.txt`` (required): version string matching the directory name,
    optionally with a ``.bcr.N`` suffix (e.g. ``17.0.3`` or ``17.0.3.bcr.1``).
  - ``presubmit.yml`` (required): BCR presubmit test configuration.
  - ``patches/`` (optional): patch files matching ``NNN_description.patch``
    (three-digit zero-padded prefix, sequential starting at 001).

Usage:
    python3 validate_patches.py <versions_dir>
"""

import argparse
import re
import sys
from pathlib import Path

PATCH_RE = re.compile(r"^(\d{3})[_-].+\.patch$")

IGNORED_FILES = {".gitkeep"}


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("versions_dir", type=Path, help="Path to versions/ directory")
    return parser.parse_args()


def _is_non_empty(version_dir: Path) -> bool:
    """True if the directory has meaningful content beyond placeholders."""
    for item in version_dir.iterdir():
        if item.is_file() and item.name not in IGNORED_FILES:
            return True
        if item.is_dir() and any(item.iterdir()):
            return True
    return False


def validate_version_txt(version_dir: Path) -> list[str]:
    """Validate the version.txt file in a version directory.

    The content must be exactly the directory name, optionally followed
    by ``.bcr.N`` where N is one or more digits.

    Returns a list of error strings (empty if valid).
    """
    errors: list[str] = []
    version_file = version_dir / "version.txt"
    dir_name = version_dir.name

    if not version_file.is_file():
        if _is_non_empty(version_dir):
            errors.append(f"{dir_name}: missing required version.txt")
        return errors

    version = version_file.read_text().strip()
    if not version:
        errors.append(f"{dir_name}: version.txt is empty")
        return errors

    pattern = re.compile(rf"^{re.escape(dir_name)}(\.bcr\.\d+)?$")
    if not pattern.match(version):
        errors.append(
            f"{dir_name}: version.txt contains '{version}' "
            f"but must be '{dir_name}' or '{dir_name}.bcr.N'"
        )

    return errors


def validate_version(version_dir: Path) -> list[str]:
    """Validate a single version directory.

    Checks version.txt, presubmit.yml presence, and patches/ subdirectory
    for naming/sequencing.

    Returns a list of error strings (empty if valid).
    """
    errors: list[str] = []

    if _is_non_empty(version_dir) and not (version_dir / "presubmit.yml").exists():
        errors.append(f"{version_dir.name}: missing required presubmit.yml")

    errors.extend(validate_version_txt(version_dir))

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
                f"{version_dir.name}/patches/{patch.name}: does not match NNN_description.patch or NNN-description.patch"
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


def validate(versions_dir: Path) -> list[str]:
    """Validate all version directories under a versions/ root.

    Returns a list of error strings (empty if everything is valid).
    """
    errors: list[str] = []

    if not versions_dir.is_dir():
        return errors

    for entry in sorted(versions_dir.iterdir()):
        if not entry.is_dir():
            continue
        errors.extend(validate_version(entry))

    return errors


def main() -> None:
    args = parse_args()

    errors = validate(args.versions_dir)
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    else:
        print("All version directories valid.")


if __name__ == "__main__":
    main()
