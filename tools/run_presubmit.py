#!/usr/bin/env python3
"""Validate and run BCR-style ``presubmit.yml`` against a built llvm-project tree.

Modes:
  * ``--validate``: structural checks only (no Bazel).
  * ``--run-task``: run one matrix-expanded task (``--platform`` / ``--bazel`` disambiguate).
  * ``--run-host``: run all tasks matching this machine's OS family (linux/macos/windows).
  * ``--pipeline``: emit Buildkite steps (``--dry-run`` prints JSON; else ``buildkite-agent pipeline upload``).

When run via Bazel, PyYAML comes from ``@pip_deps`` (see ``bazel run //tools:requirements.update``). Otherwise install from ``tools/requirements.in``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from shutil import which

_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.presubmit_logic import (
    ExpandedTask,
    agent_queue,
    changed_version_dirs,
    expand_presubmit,
    filter_by_platform_family,
    find_expanded_task,
    host_family,
    platform_family,
    read_version_string,
    rewrite_llvm_project_label,
    validate_presubmit,
)


def _load_yaml(path: Path) -> dict:
    try:
        import yaml  # type: ignore
    except ImportError as e:  # pragma: no cover
        print(
            "ERROR: PyYAML is required. Use:\n"
            "  bazel run //tools:run_presubmit -- ...\n"
            "or: python3 -m venv .venv && .venv/bin/pip install -r tools/requirements.in",
            file=sys.stderr,
        )
        raise SystemExit(1) from e
    doc = yaml.safe_load(path.read_text())
    if not isinstance(doc, dict):
        raise SystemExit(f"Expected YAML mapping at root in {path}")
    return doc


def _repo_root() -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(out.stdout.strip())


def _bazel_cmd(
    et: ExpandedTask,
    source_dir: Path,
    *,
    extra_bazel_args: list[str] | None = None,
) -> None:
    env = os.environ.copy()
    if et.bazel:
        env["USE_BAZEL_VERSION"] = et.bazel

    extra = list(extra_bazel_args or [])
    base = ["bazel", "--nosystem_rc", "--nohome_rc"]

    def rewrite_labels(labels: tuple[str, ...]) -> list[str]:
        return [rewrite_llvm_project_label(l) for l in labels]

    if et.build_targets:
        cmd = [*base, "build", *et.build_flags, *extra, "--", *rewrite_labels(et.build_targets)]
        print("+", " ".join(cmd), flush=True)
        subprocess.run(cmd, cwd=source_dir, check=True, env=env)

    if et.test_targets:
        cmd = [*base, "test", *et.test_flags, *extra, "--", *rewrite_labels(et.test_targets)]
        print("+", " ".join(cmd), flush=True)
        subprocess.run(cmd, cwd=source_dir, check=True, env=env)

    if not et.build_targets and not et.test_targets:
        raise SystemExit(f"Task {et.task_id!r} has no build_targets or test_targets")


def cmd_validate(presubmit: Path) -> int:
    doc = _load_yaml(presubmit)
    errors = validate_presubmit(doc)
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"OK: {presubmit} is structurally valid")
    return 0


def cmd_run_task(
    presubmit: Path,
    source_dir: Path,
    task_id: str,
    platform: str | None,
    bazel: str | None,
    extra_bazel_args: list[str] | None,
) -> int:
    doc = _load_yaml(presubmit)
    errors = validate_presubmit(doc)
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1
    expanded = expand_presubmit(doc)
    et = find_expanded_task(expanded, task_id, platform, bazel)
    if platform_family(et.platform) != host_family():
        print(
            f"WARNING: task platform {et.platform!r} family differs from host "
            f"({host_family()}); continuing anyway.",
            file=sys.stderr,
        )
    _bazel_cmd(et, source_dir, extra_bazel_args=extra_bazel_args)
    return 0


def cmd_run_host(
    presubmit: Path,
    source_dir: Path,
    extra_bazel_args: list[str] | None,
) -> int:
    doc = _load_yaml(presubmit)
    errors = validate_presubmit(doc)
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1
    expanded = expand_presubmit(doc)
    host = host_family()
    to_run = filter_by_platform_family(expanded, host)
    if not to_run:
        print(f"No expanded tasks for host family {host!r} in {presubmit}")
        return 0
    to_run = sorted(to_run, key=lambda et: (et.task_id, et.platform, et.bazel))
    for et in to_run:
        print(f"==> {et.task_id} platform={et.platform!r} bazel={et.bazel!r}", flush=True)
        _bazel_cmd(et, source_dir, extra_bazel_args=extra_bazel_args)
    return 0


def _build_step_commands(
    repo_root: Path,
    llvm_version: str,
    version: str,
    presubmit_rel: Path,
    et: ExpandedTask,
    source_dir_name: str,
) -> list[str]:
    """Shell commands for one Buildkite step (POSIX)."""
    root_s = str(repo_root).replace("'", "'\\''")
    pres_s = str(presubmit_rel).replace("'", "'\\''")
    return [
        f"cd '{root_s}'",
        " ".join(
            [
                "bazel",
                "run",
                "//tools:build",
                "--",
                f"--llvm-version={llvm_version}",
                f"--version={version}",
                "--versions-dir=versions",
                "--output-dir=.",
            ]
        ),
        " ".join(
            [
                "bazel",
                "run",
                "//tools:run_presubmit",
                "--",
                f"--presubmit={pres_s}",
                f"--run-task={et.task_id}",
                f"--source-dir={source_dir_name}",
                f"--platform={et.platform}",
                f"--bazel={et.bazel}",
            ]
        ),
    ]


def cmd_pipeline(
    versions_dir: Path,
    git_base_ref: str,
    dry_run: bool,
) -> int:
    repo_root = _repo_root()
    rel_versions = versions_dir
    if not rel_versions.is_absolute():
        rel_versions = (repo_root / rel_versions).resolve()

    try:
        changed = changed_version_dirs(git_base_ref, str(repo_root))
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if not changed:
        print("No changed versions/ directories; uploading no-op pipeline.")
        steps = [
            {
                "label": "No versions/ changes in this revision",
                "agents": {"queue": "default"},
                "commands": ["echo 'No versions/ changes; skipping presubmit matrix.'"],
            }
        ]
    else:
        steps = []
        for llvm_version in changed:
            presubmit = repo_root / "versions" / llvm_version / "presubmit.yml"
            if not presubmit.is_file():
                print(f"ERROR: missing {presubmit}", file=sys.stderr)
                return 1
            doc = _load_yaml(presubmit)
            errors = validate_presubmit(doc)
            if errors:
                for e in errors:
                    print(f"ERROR: {presubmit}: {e}", file=sys.stderr)
                return 1
            version = read_version_string(llvm_version, str(rel_versions))
            source_dir_name = f"llvm-project-{version}.bzl"
            presubmit_rel = Path("versions") / llvm_version / "presubmit.yml"
            expanded = expand_presubmit(doc)
            for et in expanded:
                label = f"{llvm_version} / {et.task_id} ({et.platform}, {et.bazel})"
                steps.append(
                    {
                        "label": label,
                        "agents": {"queue": agent_queue(et.platform)},
                        "commands": _build_step_commands(
                            repo_root,
                            llvm_version,
                            version,
                            presubmit_rel,
                            et,
                            source_dir_name,
                        ),
                    }
                )

    payload = {"steps": steps}
    text = json.dumps(payload, indent=2)
    if dry_run:
        print(text)
        return 0

    agent = which("buildkite-agent")
    if not agent:
        print("ERROR: buildkite-agent not found in PATH", file=sys.stderr)
        return 1
    print("Uploading dynamic pipeline to Buildkite...", flush=True)
    subprocess.run(
        [agent, "pipeline", "upload"],
        input=text.encode(),
        cwd=repo_root,
        check=True,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--presubmit",
        type=Path,
        help="Path to presubmit.yml (required for validate/run-task/run-host)",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Structural validation only",
    )
    parser.add_argument(
        "--run-task",
        dest="task_id",
        metavar="TASK_ID",
        help="Run a single expanded task id (disambiguate with --platform / --bazel)",
    )
    parser.add_argument(
        "--run-host",
        action="store_true",
        help="Run all tasks for this host OS family (linux/macos/windows)",
    )
    parser.add_argument(
        "--pipeline",
        action="store_true",
        help="Generate and upload Buildkite dynamic pipeline steps",
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        help="Path to extracted llvm-project-* .bzl source directory",
    )
    parser.add_argument(
        "--platform",
        help="Presubmit platform string (e.g. debian10) to select one matrix expansion",
    )
    parser.add_argument(
        "--bazel",
        help="Bazel version string (e.g. 7.x) to select one matrix expansion",
    )
    parser.add_argument(
        "--versions-dir",
        type=Path,
        default=Path("versions"),
        help="versions/ directory (for pipeline / version.txt)",
    )
    parser.add_argument(
        "--git-base-ref",
        default=(os.environ.get("BUILDKITE_PULL_REQUEST_BASE_BRANCH") or "main"),
        help="Git ref for detecting changed versions (default: BUILDKITE_PULL_REQUEST_BASE_BRANCH or main)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="With --pipeline, print JSON instead of uploading",
    )
    parser.add_argument(
        "extra_bazel_args",
        nargs="*",
        help="Extra args passed to bazel build/test after flags and before --",
    )

    args = parser.parse_args(argv)

    modes = int(args.validate) + int(args.task_id is not None) + int(args.run_host) + int(args.pipeline)
    if modes != 1:
        parser.error("Specify exactly one of --validate, --run-task, --run-host, or --pipeline")

    if args.pipeline:
        return cmd_pipeline(args.versions_dir, args.git_base_ref, args.dry_run)

    if not args.presubmit:
        parser.error("--presubmit is required")

    if args.validate:
        return cmd_validate(args.presubmit)

    if not args.source_dir:
        parser.error("--source-dir is required for --run-task / --run-host")

    src = args.source_dir.resolve()
    if not src.is_dir():
        print(f"ERROR: source dir not found: {src}", file=sys.stderr)
        return 1

    extra = list(args.extra_bazel_args or [])

    if args.task_id is not None:
        return cmd_run_task(
            args.presubmit,
            src,
            args.task_id,
            args.platform,
            args.bazel,
            extra,
        )

    return cmd_run_host(args.presubmit, src, extra)


if __name__ == "__main__":
    _cwd = os.environ.get("BUILD_WORKING_DIRECTORY")
    if _cwd:
        os.chdir(_cwd)
    raise SystemExit(main())
