#!/usr/bin/env python3
"""Build an LLVM redistribution artifact with Bazel overlay.

Unifies CI and local workflows into a single cross-platform Python script.
Downloads the upstream source, applies the Bazel overlay and patches,
transforms MODULE.bazel / extensions.bzl, generates build files, and
repackages everything into a deterministic .tar.xz archive.

Usage:
    # Local build (output in build/17.0.3/)
    python3 scripts/build.py --llvm-version 17.0.3

    # BCR patch release
    python3 scripts/build.py --llvm-version 17.0.3 --bcr-version 1

    # CI build with explicit paths
    python3 scripts/build.py \\
        --llvm-version 17.0.3 \\
        --version 17.0.3.bcr.preview \\
        --versions-dir redist-repo/versions \\
        --output-dir . \\
        --metadata-dir metadata
"""

import argparse
import hashlib
import lzma
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from urllib.request import urlretrieve

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR.parent))

from tools.transform_extensions_bzl import transform as _transform_extensions_source
from tools.transform_module_bazel import transform as _transform_module_source

UPSTREAM_URL_TEMPLATE = (
    "https://github.com/llvm/llvm-project/releases/download/"
    "llvmorg-{version}/llvm-project-{version}.src.tar.xz"
)

OVERLAY_EXTRA_FILES = ("vulkan_sdk.bzl", "BUILD.bazel", ".bazelrc", ".bazelversion")

LLVM_TARGETS = [
    "AArch64", "AMDGPU", "ARM", "AVR", "BPF", "Hexagon",
    "Lanai", "LoongArch", "Mips", "MSP430", "NVPTX", "PowerPC",
    "RISCV", "Sparc", "SPIRV", "SystemZ", "VE", "WebAssembly",
    "X86", "XCore",
]

BOLT_SUPPORTED = frozenset(("AArch64", "X86", "RISCV"))


# ── Pure helpers ──────────────────────────────────────────────────────────


def compute_sha256(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_integrity(actual: str, integrity_file: Path) -> None:
    """Verify SHA-256 against a stored value.

    Raises SystemExit on mismatch.  Does nothing if the file is absent.
    """
    if integrity_file.is_file():
        expected = integrity_file.read_text().strip()
        if actual != expected:
            print("ERROR: upstream tarball SHA-256 mismatch", file=sys.stderr)
            print(f"  expected: {expected}", file=sys.stderr)
            print(f"  actual:   {actual}", file=sys.stderr)
            sys.exit(1)
        print(f"    Integrity verified: {actual}")
    else:
        print("    No source.sha256 found (will not verify)")


def extract_cmake_var(filepath: Path, varname: str) -> str | None:
    """Extract a variable value from a CMake ``set()`` command."""
    pattern = re.compile(rf"\s*set\s*\(\s*{re.escape(varname)}\s+(\S+)")
    with open(filepath) as f:
        for line in f:
            m = pattern.match(line)
            if m:
                return m.group(1).rstrip(")")
    return None


def _make_deterministic(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """Zero out non-deterministic tar entry metadata."""
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    return info


# ── Build pipeline steps ─────────────────────────────────────────────────


def _download_progress(block_num: int, block_size: int, total_size: int) -> None:
    downloaded = block_num * block_size
    if total_size > 0:
        pct = min(100, downloaded * 100 // total_size)
        mb = downloaded / (1 << 20)
        total_mb = total_size / (1 << 20)
        print(f"\r    {mb:.1f}/{total_mb:.1f} MB ({pct}%)", end="", flush=True)


def download_tarball(llvm_version: str, dest_dir: Path) -> tuple[Path, str]:
    """Download the upstream LLVM source tarball.

    Returns ``(tarball_path, sha256)``.
    Reuses a cached file when present.
    """
    url = UPSTREAM_URL_TEMPLATE.format(version=llvm_version)
    tarball = dest_dir / f"llvm-project-{llvm_version}.src.tar.xz"

    if tarball.is_file():
        print(f"==> Using cached {tarball.name}")
    else:
        print(f"==> Downloading {url}...")
        dest_dir.mkdir(parents=True, exist_ok=True)
        urlretrieve(url, tarball, reporthook=_download_progress)
        print()

    sha256 = compute_sha256(tarball)
    print(f"    SHA-256: {sha256}")
    return tarball, sha256


def extract_tarball(tarball: Path, dest_dir: Path) -> Path:
    """Extract a ``.tar.xz`` archive and return the top-level directory."""
    print(f"==> Extracting {tarball.name}...")
    with tarfile.open(tarball, "r:xz") as tar:
        top_level = tar.getnames()[0].split("/")[0]
        target = dest_dir / top_level
        if target.exists():
            shutil.rmtree(target)
        if sys.version_info >= (3, 12):
            tar.extractall(path=dest_dir, filter="data")
        else:
            tar.extractall(path=dest_dir)
    return target


def apply_overlay(src_dir: Path) -> None:
    """Copy the Bazel overlay files into the source root."""
    print("==> Applying Bazel overlay...")
    overlay = src_dir / "utils" / "bazel" / "llvm-project-overlay"
    if not overlay.is_dir():
        print(
            f"    WARNING: overlay directory not found at {overlay}",
            file=sys.stderr,
        )
        return

    shutil.copytree(overlay, src_dir, dirs_exist_ok=True)

    bazel_utils = src_dir / "utils" / "bazel"
    for name in OVERLAY_EXTRA_FILES:
        src_file = bazel_utils / name
        if src_file.is_file():
            shutil.copy2(src_file, src_dir / name)


def apply_patches(src_dir: Path, patch_dir: Path) -> int:
    """Apply all ``*.patch`` files from *patch_dir* to *src_dir*.

    Returns the number of patches applied.
    """
    if not patch_dir.is_dir():
        return 0

    patches = sorted(patch_dir.glob("*.patch"))
    if not patches:
        return 0

    print(f"==> Applying {len(patches)} patch(es)...")
    for p in patches:
        print(f"    {p.name}")
        subprocess.run(
            ["patch", "-p1", "-d", str(src_dir), "-i", str(p.resolve())],
            check=True,
        )
    return len(patches)


def transform_module(src_dir: Path, version: str) -> None:
    """Transform the upstream ``MODULE.bazel`` or generate a minimal one."""
    upstream = src_dir / "utils" / "bazel" / "MODULE.bazel"
    output = src_dir / "MODULE.bazel"

    if upstream.is_file():
        print("==> Transforming MODULE.bazel...")
        result = _transform_module_source(upstream.read_text(), version)
        output.write_text(result)
    else:
        print("==> No upstream MODULE.bazel, generating minimal one...")
        output.write_text(
            f'module(name = "llvm-project", version = "{version}")\n'
        )


def transform_extensions(src_dir: Path) -> None:
    """Transform ``extensions.bzl`` if present in the upstream Bazel files."""
    upstream = src_dir / "utils" / "bazel" / "extensions.bzl"
    output = src_dir / "extensions.bzl"

    if upstream.is_file():
        print("==> Transforming extensions.bzl...")
        result = _transform_extensions_source(upstream.read_text())
        output.write_text(result)
    else:
        print("    No upstream extensions.bzl, skipping")


def generate_vars_bzl(src_dir: Path) -> None:
    """Generate ``vars.bzl`` from CMake version variables."""
    print("==> Generating vars.bzl...")

    version_file = src_dir / "cmake" / "Modules" / "LLVMVersion.cmake"
    if not version_file.is_file():
        version_file = src_dir / "llvm" / "CMakeLists.txt"

    cmake_file = src_dir / "llvm" / "CMakeLists.txt"

    major = extract_cmake_var(version_file, "LLVM_VERSION_MAJOR") or "0"
    minor = extract_cmake_var(version_file, "LLVM_VERSION_MINOR") or "0"
    patch = extract_cmake_var(version_file, "LLVM_VERSION_PATCH") or "0"
    suffix = extract_cmake_var(version_file, "LLVM_VERSION_SUFFIX") or ""

    cxx_std = (
        extract_cmake_var(cmake_file, "LLVM_REQUIRED_CXX_STANDARD")
        or extract_cmake_var(cmake_file, "CMAKE_CXX_STANDARD")
        or "17"
    )

    llvm_ver = f"{major}.{minor}.{patch}"
    package_version = f"{llvm_ver}{suffix}"

    variables = {
        "CMAKE_CXX_STANDARD": cxx_std,
        "LLVM_VERSION_MAJOR": major,
        "LLVM_VERSION_MINOR": minor,
        "LLVM_VERSION_PATCH": patch,
        "LLVM_VERSION_SUFFIX": suffix,
        "LLVM_VERSION": llvm_ver,
        "PACKAGE_VERSION": package_version,
    }

    rel_cmake = cmake_file.relative_to(src_dir)
    lines = [f"# Generated from {rel_cmake}\n"]
    for k, v in variables.items():
        lines.append(f'{k} = "{v}"')
    lines.append("")
    lines.append("llvm_vars = {")
    for k, v in variables.items():
        lines.append(f'    "{k}": "{v}",')
    lines.append("}")
    lines.append("")

    (src_dir / "vars.bzl").write_text("\n".join(lines))


def generate_targets_bzl(src_dir: Path) -> None:
    """Generate ``llvm/targets.bzl`` and ``bolt/targets.bzl``."""
    print("==> Generating targets.bzl files...")

    bolt_targets = [t for t in LLVM_TARGETS if t in BOLT_SUPPORTED]

    llvm_dir = src_dir / "llvm"
    bolt_dir = src_dir / "bolt"
    llvm_dir.mkdir(parents=True, exist_ok=True)
    bolt_dir.mkdir(parents=True, exist_ok=True)

    (llvm_dir / "targets.bzl").write_text(f"llvm_targets = {LLVM_TARGETS!r}\n")
    (bolt_dir / "targets.bzl").write_text(f"bolt_targets = {bolt_targets!r}\n")


def create_archive(src_dir: Path, output: Path) -> str:
    """Create a deterministic ``.tar.xz`` archive.

    Entries are sorted by name, with uid/gid/mtime zeroed.
    Returns the SHA-256 of the written file.
    """
    print(f"==> Repackaging as {output.name} (deterministic)...")
    parent = src_dir.parent

    with lzma.open(output, "wb") as xz:
        with tarfile.open(fileobj=xz, mode="w") as tar:
            for dirpath, dirnames, filenames in os.walk(src_dir):
                dirnames.sort()
                rel_dir = os.path.relpath(dirpath, parent).replace(os.sep, "/")

                info = tar.gettarinfo(dirpath, arcname=rel_dir)
                _make_deterministic(info)
                tar.addfile(info)

                for fname in sorted(filenames):
                    fpath = os.path.join(dirpath, fname)
                    arcname = f"{rel_dir}/{fname}"
                    info = tar.gettarinfo(fpath, arcname=arcname)
                    _make_deterministic(info)

                    if info.issym() or info.islnk():
                        tar.addfile(info)
                    else:
                        with open(fpath, "rb") as f:
                            tar.addfile(info, f)

    sha256 = compute_sha256(output)
    mb = output.stat().st_size / (1 << 20)
    print(f"    Size: {mb:.1f} MB")
    print(f"    SHA-256: {sha256}")
    return sha256


# ── Orchestration ─────────────────────────────────────────────────────────


def build(
    *,
    llvm_version: str,
    version: str,
    versions_dir: Path,
    output_dir: Path,
    metadata_dir: Path | None = None,
) -> dict:
    """Run the full build pipeline and return a results dict."""
    output_dir.mkdir(parents=True, exist_ok=True)

    tarball, upstream_sha256 = download_tarball(llvm_version, output_dir)

    integrity_file = versions_dir / llvm_version / "source.sha256"
    verify_integrity(upstream_sha256, integrity_file)

    src_dir = extract_tarball(tarball, output_dir)

    apply_overlay(src_dir)

    patch_dir = versions_dir / llvm_version / "patches"
    apply_patches(src_dir, patch_dir)

    transform_module(src_dir, version)
    transform_extensions(src_dir)

    generate_vars_bzl(src_dir)
    generate_targets_bzl(src_dir)

    final_name = f"llvm-project-{version}.bzl"
    final_dir = output_dir / final_name
    if final_dir.exists() and final_dir != src_dir:
        shutil.rmtree(final_dir)
    src_dir.rename(final_dir)

    artifact = output_dir / f"{final_name}.tar.xz"
    artifact_sha256 = create_archive(final_dir, artifact)

    if metadata_dir is not None:
        metadata_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(final_dir / "MODULE.bazel", metadata_dir / "MODULE.bazel")
        src_sha = versions_dir / llvm_version / "source.sha256"
        if src_sha.is_file():
            shutil.copy2(src_sha, metadata_dir / "source.sha256")

    result = {
        "llvm_version": llvm_version,
        "version": version,
        "artifact": str(artifact),
        "artifact_sha256": artifact_sha256,
        "upstream_sha256": upstream_sha256,
        "source_dir": str(final_dir),
    }

    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a") as f:
            for k, v in result.items():
                f.write(f"{k}={v}\n")

    return result


# ── CLI ───────────────────────────────────────────────────────────────────


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build an LLVM redistribution artifact with Bazel overlay.",
    )
    parser.add_argument(
        "--llvm-version",
        required=True,
        help="LLVM version (e.g. 17.0.3)",
    )
    parser.add_argument(
        "--version",
        help="Output version string (default: same as --llvm-version)",
    )
    parser.add_argument(
        "--bcr-version",
        help="BCR patch version (e.g. 1 → 17.0.3.bcr.1)",
    )
    parser.add_argument(
        "--versions-dir",
        type=Path,
        help="Path to versions/ directory (default: <repo>/versions)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Output directory (default: build/<llvm-version>)",
    )
    parser.add_argument(
        "--metadata-dir",
        type=Path,
        help="Copy MODULE.bazel and source.sha256 to this directory",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()

    repo_root = _SCRIPTS_DIR.parent
    versions_dir = args.versions_dir or repo_root / "versions"
    output_dir = args.output_dir or repo_root / "build" / args.llvm_version

    if args.version:
        version = args.version
    elif args.bcr_version:
        version = f"{args.llvm_version}.bcr.{args.bcr_version}"
    else:
        version = args.llvm_version

    result = build(
        llvm_version=args.llvm_version,
        version=version,
        versions_dir=versions_dir,
        output_dir=output_dir,
        metadata_dir=args.metadata_dir,
    )

    print()
    print(f"Done! Artifact: {result['artifact']}")
    print(f"SHA-256: {result['artifact_sha256']}")

    if not os.environ.get("GITHUB_OUTPUT"):
        print()
        print("To test with Bazel, point a local_path_override at:")
        print(f"  {result['source_dir']}")


if __name__ == "__main__":
    main()
