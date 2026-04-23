#!/usr/bin/env python3
"""Transform the upstream llvm-project-overlay MODULE.bazel for redistribution.

Reads the overlay MODULE.bazel and produces a self-contained version with the
@llvm-raw / llvm_configure indirection removed. All dependency pins and
use_repo entries are retained from upstream (only "llvm-raw" is dropped from
use_repo since the source is already present in the tarball).

Usage:
    python3 transform_module_bazel.py <input> <output> <version>
"""

import argparse
import re
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Path to upstream MODULE.bazel")
    parser.add_argument(
        "output", type=Path, help="Path to write transformed MODULE.bazel"
    )
    parser.add_argument("version", type=str, help="Module version string (e.g. 22.1.4)")
    return parser.parse_args()


def transform(source: str, version: str) -> str:
    lines = source.splitlines(keepends=True)
    out: list[str] = []
    skip_until_close_paren = False
    inside_use_repo = False
    removed_llvm_raw_repo_entry = False

    for line in lines:
        stripped = line.strip()

        # --- module() declaration: rename and inject version ---------------
        if stripped.startswith("module("):
            out.append(f'module(name = "llvm-project", version = "{version}")\n')
            continue

        # --- Remove use_repo_rule for llvm_configure -----------------------
        if "use_repo_rule(" in stripped and "llvm_configure" in stripped:
            skip_until_close_paren = not stripped.endswith(")")
            continue

        # --- Remove llvm_configure(...) invocation -------------------------
        if stripped.startswith("llvm_configure("):
            skip_until_close_paren = not stripped.endswith(")")
            continue

        # --- Skip continuation lines of a removed multi-line statement -----
        if skip_until_close_paren:
            if stripped.endswith(")"):
                skip_until_close_paren = False
            continue

        # --- Inside use_repo(): drop "llvm-raw" entry ---------------------
        if inside_use_repo:
            if stripped.endswith(")"):
                inside_use_repo = False
                out.append(line)
                continue
            if re.match(r'^"llvm-raw"', stripped):
                removed_llvm_raw_repo_entry = True
                continue
            out.append(line)
            continue

        # --- Detect start of use_repo( block -------------------------------
        if stripped.startswith("use_repo("):
            inside_use_repo = not stripped.endswith(")")
            out.append(line)
            continue

        # --- Pass everything else through ----------------------------------
        out.append(line)

    result = "".join(out)

    # Collapse runs of 3+ blank lines into 2
    result = re.sub(r"\n{3,}", "\n\n", result)

    # Ensure trailing newline
    if not result.endswith("\n"):
        result += "\n"

    if not removed_llvm_raw_repo_entry:
        print(
            "WARNING: did not find 'llvm-raw' in use_repo(); "
            "upstream MODULE.bazel format may have changed",
            file=sys.stderr,
        )

    return result


def main() -> None:
    args = parse_args()

    with open(args.input) as f:
        source = f.read()

    result = transform(source, args.version)

    with open(args.output, "w") as f:
        f.write(result)

    print(result)


if __name__ == "__main__":
    main()
