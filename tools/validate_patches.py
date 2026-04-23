#!/usr/bin/env python3
"""Validate version directories under versions/.

Each versions/{version}/ directory may contain:
  - ``version.txt`` (required): version string matching the directory name,
    optionally with a ``.bcr.N`` suffix (e.g. ``17.0.3`` or ``17.0.3.bcr.1``).
  - ``presubmit.yml`` (required): BCR presubmit test configuration.
  - ``patches/`` (optional): patch files matching ``NNN_description.patch``
    (three-digit zero-padded prefix, sequential starting at 001).
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlretrieve

PATCH_RE = re.compile(r"^(\d{3})[_-].+\.patch$")

IGNORED_FILES = {".gitkeep"}


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("versions_dir", type=Path, help="Path to versions/ directory")
    parser.add_argument(
        "--verify-upstream",
        action="store_true",
        help="Verify .sig and signing key match upstream LLVM release and keyserver",
    )
    parser.add_argument(
        "--base-ref",
        default="origin/main",
        help="Git ref to compare version.txt against for increment validation (default: origin/main)",
    )
    return parser.parse_args()


def _is_non_empty(version_dir: Path) -> bool:
    """True if the directory has meaningful content beyond placeholders."""
    for item in version_dir.iterdir():
        if item.is_file() and item.name not in IGNORED_FILES:
            return True
        if item.is_dir() and any(item.iterdir()):
            return True
    return False


def validate_version_txt(version_dir: Path, *, base_ref: str = "origin/main") -> list[str]:
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
        errors.append(f"{dir_name}: version.txt contains '{version}' but must be '{dir_name}' or '{dir_name}.bcr.N'")
        return errors

    errors.extend(_check_version_increment(version_dir, base_ref=base_ref))
    return errors


_BCR_SUFFIX_RE = re.compile(r"\.bcr\.(\d+)$")


def _parse_bcr_number(version: str) -> int:
    m = _BCR_SUFFIX_RE.search(version)
    return int(m.group(1)) if m else 0


def _check_version_increment(version_dir: Path, *, base_ref: str = "origin/main") -> list[str]:
    """Check that the .bcr.N suffix increments by exactly 1 relative to *base_ref*."""
    version_file = version_dir / "version.txt"
    rel_path = f"versions/{version_dir.name}/version.txt"
    repo_root = version_dir.parent.parent

    result = subprocess.run(
        ["git", "show", f"{base_ref}:{rel_path}"],
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    if result.returncode != 0:
        return []

    old_version = result.stdout.strip()
    new_version = version_file.read_text().strip()

    if old_version == new_version:
        return []

    old_n = _parse_bcr_number(old_version)
    new_n = _parse_bcr_number(new_version)

    if new_n != old_n + 1:
        return [
            f"{version_dir.name}: version.txt changed from '{old_version}' to "
            f"'{new_version}' but .bcr.N must increment by exactly 1"
        ]
    return []


_IMMUTABLE_PATTERNS = ("*.sig", "signing-key.asc")

_UPSTREAM_SIG_URL = "https://github.com/llvm/llvm-project/releases/download/llvmorg-{version}/{tarball}.sig"


def _check_immutable_files(version_dir: Path) -> list[str]:
    """Ensure .sig and signing-key.asc files have not been modified after initial commit."""
    errors: list[str] = []
    import subprocess

    for pattern in _IMMUTABLE_PATTERNS:
        for f in version_dir.glob(pattern):
            rel = f.relative_to(version_dir.parent.parent)
            result = subprocess.run(
                [
                    "git",
                    "log",
                    "--oneline",
                    "--follow",
                    "--diff-filter=M",
                    "--",
                    str(rel),
                ],
                capture_output=True,
                text=True,
                cwd=version_dir.parent.parent,
            )
            if result.returncode == 0 and result.stdout.strip():
                errors.append(f"{version_dir.name}/{f.name}: immutable file has been modified after initial commit")
    return errors


def _is_newly_added(filepath: Path) -> bool:
    """Return True if *filepath* is untracked or only exists in uncommitted/staged changes."""
    import subprocess

    result = subprocess.run(
        ["git", "log", "--oneline", "-1", "--", str(filepath.relative_to(filepath.parent.parent.parent))],
        capture_output=True,
        text=True,
        cwd=filepath.parent.parent.parent,
    )
    return result.returncode != 0 or not result.stdout.strip()


def _check_upstream_sig(version_dir: Path) -> list[str]:
    """Verify .sig always matches upstream LLVM release."""
    errors: list[str] = []
    dir_name = version_dir.name

    sig_files = list(version_dir.glob("*.sig"))
    if not sig_files:
        return errors

    sig_file = sig_files[0]
    tarball_name = sig_file.stem

    url = _UPSTREAM_SIG_URL.format(version=dir_name, tarball=tarball_name)
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".sig") as tmp:
            urlretrieve(url, tmp.name)
            local_sig = sig_file.read_bytes()
            upstream_sig = Path(tmp.name).read_bytes()
            if local_sig != upstream_sig:
                errors.append(f"{dir_name}/{sig_file.name}: does not match upstream at {url}")
            Path(tmp.name).unlink()
    except (URLError, OSError) as e:
        errors.append(f"{dir_name}/{sig_file.name}: could not fetch upstream sig: {e}")

    return errors


def _check_upstream_key(version_dir: Path) -> list[str]:
    """Verify signing-key.asc matches upstream release keys, but only when newly added."""
    errors: list[str] = []
    dir_name = version_dir.name

    key_file = version_dir / "signing-key.asc"
    if not key_file.is_file() or not _is_newly_added(key_file):
        return errors

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".asc") as tmp_keys:
            urlretrieve("https://releases.llvm.org/release-keys.asc", tmp_keys.name)
            upstream_keys = Path(tmp_keys.name).read_text()
            local_key = key_file.read_text().strip()
            if local_key not in upstream_keys:
                errors.append(
                    f"{dir_name}/signing-key.asc: key not found in https://releases.llvm.org/release-keys.asc"
                )
            Path(tmp_keys.name).unlink()
    except (URLError, OSError) as e:
        errors.append(f"{dir_name}/signing-key.asc: could not fetch upstream release keys: {e}")

    return errors


def validate_version(version_dir: Path, *, verify_upstream: bool = False, base_ref: str = "origin/main") -> list[str]:
    """Validate a single version directory.

    Checks version.txt, presubmit.yml presence, sig file, signing key,
    and patches/ subdirectory for naming/sequencing.

    When *verify_upstream* is True, also checks that the .sig file matches
    the upstream LLVM release and the signing key matches the keyserver.

    Returns a list of error strings (empty if valid).
    """
    errors: list[str] = []
    dir_name = version_dir.name

    if _is_non_empty(version_dir) and not (version_dir / "presubmit.yml").exists():
        errors.append(f"{dir_name}: missing required presubmit.yml")

    sig_files = list(version_dir.glob("*.sig"))
    if _is_non_empty(version_dir) and not sig_files:
        errors.append(f"{dir_name}: missing required .sig file for upstream tarball")

    if _is_non_empty(version_dir) and not (version_dir / "signing-key.asc").exists():
        errors.append(f"{dir_name}: missing required signing-key.asc")

    errors.extend(_check_immutable_files(version_dir))
    if verify_upstream:
        errors.extend(_check_upstream_sig(version_dir))
        errors.extend(_check_upstream_key(version_dir))
    errors.extend(validate_version_txt(version_dir, base_ref=base_ref))

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
                f"{version_dir.name}/patches/{patch.name}: "
                f"does not match NNN_description.patch or NNN-description.patch"
            )
            continue
        numbers.append(int(m.group(1)))

    if len(numbers) != len(patches):
        return errors

    for i, n in enumerate(numbers):
        expected = i + 1
        if n != expected:
            errors.append(
                f"{version_dir.name}: expected patch {expected:03d} but found {n:03d} (gap or out-of-order sequence)"
            )
            return errors

    return errors


def validate(versions_dir: Path, *, verify_upstream: bool = False, base_ref: str = "origin/main") -> list[str]:
    """Validate all version directories under a versions/ root.

    Returns a list of error strings (empty if everything is valid).
    """
    errors: list[str] = []

    if not versions_dir.is_dir():
        return errors

    for entry in sorted(versions_dir.iterdir()):
        if not entry.is_dir():
            continue
        errors.extend(validate_version(entry, verify_upstream=verify_upstream, base_ref=base_ref))

    return errors


def main() -> None:
    args = parse_args()

    errors = validate(args.versions_dir, verify_upstream=args.verify_upstream, base_ref=args.base_ref)
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    else:
        print("All version directories valid.")


if __name__ == "__main__":
    _cwd = os.environ.get("BUILD_WORKING_DIRECTORY")
    if _cwd:
        os.chdir(_cwd)
    main()
