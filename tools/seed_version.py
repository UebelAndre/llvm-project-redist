#!/usr/bin/env python3
"""Create ``versions/<llvm_version>/`` for a newly released LLVM version.

What a new version starts from depends on how far it is from what we already
track:

* **Same major line** (17.0.6 after 17.0.5; 17.1.0 after 17.0.5): copy the
  nearest lower version we have -- its ``patches/`` and its ``presubmit.yml``
  -- since both almost certainly still apply, and anything that no longer
  applies is better fixed by a human looking at a diff than regenerated from
  nothing. ``version.txt`` is written fresh.
* **New major line** (18.0.0): start clean. No patches, and ``presubmit.yml``
  rendered from ``tools/presubmit.template.yml`` against upstream's
  ``.bazelrc`` for the tag -- which, with no patches yet, is the post-patch
  file.

Either way the result is a starting point for review, not a finished release:
the PR CI prepares the source (so patches that stopped applying fail there)
and runs the version's presubmit.

Usage:
    bazel run //tools:seed_version -- --llvm-version 17.0.6
    bazel run //tools:seed_version -- --llvm-version 18.1.0 --bazelrc /tmp/upstream.bazelrc
    bazel run //tools:seed_version -- --llvm-version 17.1.0 --from 17.0.3
"""

from __future__ import annotations

import argparse
import logging as std_logging
import os
import re
import shutil
import tempfile
import urllib.request
from pathlib import Path

from tools.render_presubmit import TEMPLATE_RELATIVE_PATH, _user_cwd_path, _workspace_root, render

logging = std_logging.getLogger(__name__)

_UPSTREAM_BAZELRC_URL = "https://raw.githubusercontent.com/llvm/llvm-project/llvmorg-{version}/utils/bazel/.bazelrc"
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def parse_version(text: str) -> tuple[int, int, int]:
    m = _VERSION_RE.match(text)
    if not m:
        raise SystemExit(f"ERROR: {text!r} is not an LLVM release version (expected MAJOR.MINOR.PATCH)")
    major, minor, patch = m.groups()
    return int(major), int(minor), int(patch)


def nearest_lower_version(new: str, existing: list[str]) -> str | None:
    """The highest tracked version below *new* in the same major line, if any.

    17.0.6 -> 17.0.5 (or 17.0.4 if 17.0.5 was skipped); 17.1.0 -> the latest
    17.0.*; 18.0.0 -> None.
    """
    target = parse_version(new)
    candidates = []
    for v in existing:
        if not _VERSION_RE.match(v):
            continue
        t = parse_version(v)
        if t[0] == target[0] and t < target:
            candidates.append((t, v))
    if not candidates:
        return None
    return max(candidates)[1]


def _retarget_header(presubmit: str, old: str, new: str) -> str:
    """Point the copied file's header comment at the new version."""
    lines = presubmit.splitlines()
    i = 0
    while i < len(lines) and (not lines[i].strip() or lines[i].startswith("#")):
        lines[i] = lines[i].replace(old, new)
        i += 1
    return "\n".join(lines) + "\n"


def seed_from(versions_dir: Path, new: str, source: str) -> None:
    src = versions_dir / source
    dst = versions_dir / new
    dst.mkdir(parents=True)
    if (src / "patches").is_dir():
        shutil.copytree(src / "patches", dst / "patches")
        count = len(list((dst / "patches").glob("*.patch")))
        logging.info("Copied %d patch(es) from %s", count, source)
    presubmit = (src / "presubmit.yml").read_text(encoding="utf-8")
    (dst / "presubmit.yml").write_text(_retarget_header(presubmit, source, new), encoding="utf-8")
    logging.info("Copied presubmit.yml from %s", source)


def seed_fresh(repo_root: Path, versions_dir: Path, new: str, bazelrc: Path | None) -> None:
    dst = versions_dir / new
    dst.mkdir(parents=True)
    (dst / "patches").mkdir()
    (dst / "patches" / ".gitkeep").touch()  # git does not track empty directories
    if bazelrc is None:
        url = _UPSTREAM_BAZELRC_URL.format(version=new)
        logging.info("Fetching %s", url)
        with (
            urllib.request.urlopen(url) as resp,
            tempfile.NamedTemporaryFile("wb", suffix=".bazelrc", delete=False) as tmp,
        ):
            tmp.write(resp.read())
            bazelrc = Path(tmp.name)
    template = repo_root / TEMPLATE_RELATIVE_PATH
    (dst / "presubmit.yml").write_text(render(template, bazelrc, new), encoding="utf-8")
    logging.info("Rendered presubmit.yml from %s", TEMPLATE_RELATIVE_PATH.as_posix())


def main() -> None:
    std_logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=std_logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--llvm-version", required=True, help="New upstream release, e.g. 17.0.6")
    parser.add_argument("--versions-dir", type=_user_cwd_path, help="Path to versions/ (default: <repo>/versions)")
    parser.add_argument(
        "--from",
        dest="source",
        help="Copy this tracked version instead of the nearest lower one in the same major line",
    )
    parser.add_argument(
        "--fresh", action="store_true", help="Start clean even if a version in the same major line exists"
    )
    parser.add_argument(
        "--bazelrc",
        type=_user_cwd_path,
        help="Upstream .bazelrc to render a fresh version from (default: fetched for the tag)",
    )
    args = parser.parse_args()

    repo_root = _workspace_root()
    versions_dir = args.versions_dir or repo_root / "versions"
    new = args.llvm_version
    parse_version(new)
    if (versions_dir / new).exists():
        raise SystemExit(f"ERROR: {versions_dir / new} already exists")

    existing = [p.name for p in versions_dir.iterdir() if p.is_dir()]
    source = None if args.fresh else (args.source or nearest_lower_version(new, existing))
    if source is not None and not (versions_dir / source / "presubmit.yml").is_file():
        raise SystemExit(f"ERROR: {versions_dir / source} has no presubmit.yml to copy")

    if source is None:
        logging.info("No tracked version in the %s.x line; starting fresh", new.split(".")[0])
        seed_fresh(repo_root, versions_dir, new, args.bazelrc)
    else:
        seed_from(versions_dir, new, source)
    (versions_dir / new / "version.txt").write_text(new + "\n", encoding="utf-8")
    logging.info("Seeded %s", versions_dir / new)


if __name__ == "__main__":
    _cwd = os.environ.get("BUILD_WORKING_DIRECTORY")
    if _cwd:
        os.chdir(_cwd)
    main()
