#!/usr/bin/env python3
"""Transform the upstream llvm-project-overlay extensions.bzl for redistribution.

Reads the overlay extensions.bzl and produces a version where:
  - The new_local_repository for @llvm-raw is removed (source is already local).
  - The load() for local.bzl is removed.
  - All "@llvm-raw//utils/bazel/..." labels become Label("//utils/bazel/...").
  - The module name guard is updated from llvm-project-overlay to llvm-project.

Usage:
    python3 transform_extensions_bzl.py <input> <output>
"""

import argparse
import re
from pathlib import Path


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Path to upstream extensions.bzl")
    parser.add_argument(
        "output", type=Path, help="Path to write transformed extensions.bzl"
    )
    return parser.parse_args()


def transform(source: str) -> str:
    modified = source

    # Replace "@llvm-raw//utils/bazel/..." string labels with Label("//utils/bazel/...")
    modified = re.sub(
        r'"@llvm-raw//utils/bazel/([^"]+)"',
        r'Label("//utils/bazel/\1")',
        modified,
    )

    # Remove the new_local_repository block for llvm-raw
    modified = re.sub(
        r"\n\s*new_local_repository\(\s*\n\s*name\s*=\s*\"llvm-raw\".*?\)",
        "",
        modified,
        flags=re.DOTALL,
    )

    # Remove the local.bzl import
    modified = re.sub(
        r'^load\("@bazel_tools//tools/build_defs/repo:local\.bzl".*\)\n',
        "",
        modified,
        flags=re.MULTILINE,
    )

    # Update the module name check from llvm-project-overlay to llvm-project
    modified = modified.replace(
        '"llvm-project-overlay"',
        '"llvm-project"',
    )

    # Collapse runs of 3+ blank lines to 2
    modified = re.sub(r"\n{3,}", "\n\n", modified)

    return modified


def main() -> None:
    args = parse_args()

    with args.input.open() as f:
        source = f.read()

    result = transform(source)

    with open(args.output, "w") as f:
        f.write(result)

    print(result)


if __name__ == "__main__":
    main()
