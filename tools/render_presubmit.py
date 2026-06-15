#!/usr/bin/env python3
"""Render a version's presubmit.yml with bazelrc named-configs expanded inline.

The published llvm-project tarball ships a ``.bazelrc`` at its source root
(copied from upstream's ``utils/bazel/.bazelrc`` during the overlay step).
That file defines named configs like ``generic_clang``, ``clang-cl``, ``ci``,
etc. But the synthetic test workspace ``tools/run_presubmit.py`` creates for
bazelci has no ``.bazelrc`` of its own — so a bare ``--config=X`` reference
in ``versions/{X}/presubmit.yml`` would error at bazel-test time with
"Config value 'X' is not defined in any .rc file".

This tool reads a prepared source's ``.bazelrc`` and emits a presubmit.yml
where every ``--config=X`` reference has been recursively expanded into the
literal flags it would have selected. The result is fully self-contained:
bazel doesn't need to find the config definition anywhere.

The presubmit task structure (what platforms, what targets, what configs to
exercise) is hard-coded below — it's the canonical shape from
``versions/17.0.5/presubmit.yml``. Per-version variations (different
``bazel:`` matrix, different project-invariant flags) are CLI flags.

Usage:
    bazel run //tools:render_presubmit -- --llvm-version 17.0.5
    bazel run //tools:render_presubmit -- --llvm-version 17.0.5 --bazel-versions 7.x,8.x,9.x
    bazel run //tools:render_presubmit -- --llvm-version 17.0.5 --check  # diff-only
"""

from __future__ import annotations

import argparse
import collections
import difflib
import logging as std_logging
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any

import yaml

logging = std_logging.getLogger(__name__)

_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parent


def _workspace_root() -> Path:
    """Return the source tree this invocation should read from and write to.

    ``bazel run`` execs the script out of the runfiles tree, so
    ``Path(__file__).parent.parent`` resolves to the runfiles ``_main``
    directory rather than the checkout. Nothing this tool touches is a
    runfiles entry: ``versions/<v>/presubmit.yml`` is written back to the
    source tree, and the prepared source it reads ``.bazelrc`` from is
    generated at runtime by ``cherry_pick prepare`` (so it can never be a
    build-time ``data`` dep). ``BUILD_WORKSPACE_DIRECTORY`` — set by
    ``bazel run`` to the workspace root — is the right anchor for all of
    them. Fall back to the ``__file__``-relative root so running the script
    directly still works. Mirrors ``setup_presubmit._workspace_root``.
    """
    env = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    if env:
        return Path(env)
    return _REPO_ROOT


def _user_cwd_path(s: str) -> Path:
    """Resolve a relative path against the shell's working directory.

    ``bazel run`` changes the process CWD to the runfiles dir before
    exec-ing the script, which would break relative paths the user typed on
    the command line. Anchor them to ``BUILD_WORKING_DIRECTORY`` (set by
    ``bazel run`` to the invoking shell's CWD), falling back to the current
    CWD when not under bazel. Mirrors ``release_notes._user_cwd_path``.
    """
    p = Path(s)
    if p.is_absolute():
        return p
    base = os.environ.get("BUILD_WORKING_DIRECTORY") or os.getcwd()
    return Path(base) / p


# Lines in .bazelrc look like:
#   <cmd>[:<config>] <flag> [<flag> ...]
# where <cmd> is one of: build, common, test, run, query, fetch, sync, etc.
# We collect flags from build/common/test directives (they all propagate to
# `bazel test` invocations).
_RC_LINE_RE = re.compile(r"^\s*(common|build|test)(?::([A-Za-z0-9_-]+))?\s+(.+?)\s*$")

# Commands whose flags we aggregate. `build` flags propagate to `test`; `common`
# applies to every bazel command; `test` is the most-specific layer.
_AGGREGATED_COMMANDS = frozenset({"common", "build", "test"})

# Project invariants — patches in versions/{X}/patches/ enforce these even
# when upstream's .bazelrc doesn't. Always appended after expansion.
PROJECT_INVARIANT_FLAGS: list[str] = [
    "--incompatible_disallow_empty_glob=true",
    "--incompatible_autoload_externally=",
]

# Tags excluded on every task regardless of which compiler config is in
# play. (`nobuildkite` is what makes ``@llvm-project//...`` sustainable as
# a target expression.)
COMMON_EXCLUDED_TAGS: list[str] = ["nobuildkite"]

# Additionally excluded on Windows. ``gentbl_filegroup(test = True)`` in
# mlir/tblgen.bzl emits a ``gentbl_test`` per output, and that rule writes
# its "executable" as a bare argv string and passes ``-o /dev/null`` --
# neither of which works outside a POSIX shell. Upstream already knows,
# and tags every one of them:
#     # Shell files not executable on Windows.
#     # TODO(gcmn): Support windows.
#     tags = ["no_windows"],
# The tag is inert unless something filters on it, so all 21 of these ran
# on the Windows tasks and all 21 failed. Honouring the tag is what
# upstream intends, not a workaround. It is the only use of ``no_windows``
# in the overlay, so this excludes exactly those targets.
WINDOWS_EXCLUDED_TAGS: list[str] = ["no_windows"]

# Individual test targets dropped from the Windows tasks.
#
# These two are not broken tests -- they pass everywhere the path fits. lit
# runs each RUN line with the cwd set to the test's exec directory, and
# `CreateProcessW` rejects an `lpCurrentDirectory` longer than 258 characters
# with `ERROR_DIRECTORY` regardless of the `\\?\` prefix or the long-path
# registry setting. Python's `subprocess` passes the value straight through,
# so lit reports:
#     Could not create process (...\mlir\mlir-opt.exe)
#     due to [WinError 267] The directory name is invalid
# The limit was measured directly (258 accepted, 259 rejected, same error
# code), and across the 1336 MLIR tests build 442 executed the correlation
# between exec-directory length and pass/fail is exact: these two are the
# only ones over the line, and the longest passing test clears it by six
# characters.
#
# The cwd is long because `mlir/test/lit.cfg.py` sets
# `config.test_exec_root = os.path.join(config.mlir_obj_root, "test")` and the
# overlay maps `MLIR_BINARY_DIR` to `TEST_UNDECLARED_OUTPUTS_DIR`, which
# repeats the test's relative path inside a path that already contains it.
# Sixteen of those characters are the `-ST-<hash>` output-directory suffix
# that rules_python's per-target `enable_runfiles` transition adds on Windows
# (`python/private/py_executable.bzl:1771-1788`), so `--enable_runfiles=yes`
# would make the transition a no-op and bring both tests back under the
# limit -- but a redistribution should exercise the default Windows runfiles
# behaviour, so the targets are dropped instead. See
# ``test_windows_tasks_keep_default_runfiles_behavior``.
#
# bazelci turns a leading `-` into `except tests(set(...))` in the query it
# uses to expand `test_targets` (``get_test_query`` in bazelci.py), so the
# exclusion is resolved at query time and never reaches `bazel test`.
WINDOWS_EXCLUDED_TARGETS: list[str] = [
    "-@llvm-project//mlir/test/Dialect:Bufferization/Transforms/one-shot-bufferize-analysis-empty-tensor-elimination.mlir.test",
    "-@llvm-project//mlir/test/Dialect:Bufferization/Transforms/one-shot-module-bufferize-force-copy-before-write.mlir.test",
]

# Number of bazelci shards for the Windows tasks.
#
# Windows defaults to *copied* runfiles rather than symlink trees, and Bazel
# never reclaims a runfiles tree once it has built one. Each lit test gets its
# own tree containing every tool it can invoke, so the trees are both large and
# near-identical: one MLIR lit test measures 531 MB across 3392 files, where
# the same tree under symlinks is 912 KB. Multiplied across the test suite that
# overruns the agent disk long before the run finishes -- build 438 died with
# 664 "There is not enough space on the disk" errors, roughly half way through.
#
# The cheap fixes all amount to not testing what a Windows consumer actually
# gets: ``--windows_enable_symlinks`` (a startup option, so it would have to be
# appended to the workspace bazelrc from ``batch_commands``), ``--enable_runfiles``,
# ``--nobuild_python_zip``. A redistribution should be exercising the default
# filesystem behaviour, so none of them are used here -- see
# ``test_windows_tasks_keep_default_runfiles_behavior``.
#
# Sharding is the one lever that costs disk without costing fidelity. bazelci
# maps ``shards`` onto Buildkite ``parallelism`` and hands each shard
# ``sorted(test_targets)[shard_id::shard_count]``, so every shard runs on its
# own agent with its own disk and materializes only its slice of the runfiles
# trees. The flags, the targets and the runfiles mode are untouched; only the
# number of machines changes. Three is sized off build 438 getting about half
# way: it leaves headroom without spending more agents than the problem needs.
WINDOWS_SHARDS: int = 3

# Bazel versions the Windows clang-cl task is not run against.
#
# Bazel 7 cannot autoconfigure a clang-cl toolchain against Clang >= 22. The
# official Windows build of Clang 22 appends its provenance to the version
# line:
#     clang version 22.1.1 (https://github.com/llvm/llvm-project fef02d48c08d)
# and ``_get_clang_version`` in windows_cc_configure.bzl read that line as
# ``first_line.split(" ")[-1]``, so it comes back with the trailing
# ``fef02d48c08d)`` instead of ``22.1.1``. That string is then used to locate
# the resource directory, so ``cxx_builtin_include_directories`` ends up
# pointing at ``lib\\clang\\fef02d48c08d)\\include`` and every compile fails
# include validation:
#     ERROR: Compiling llvm/lib/Support/BLAKE3/blake3_portable.c failed:
#     absolute path inclusion(s) found in rule '@@llvm-project~//llvm:Support':
#     the source file ... includes the following non-builtin files with
#     absolute paths ...
#       'C:/Program Files/LLVM/lib/clang/22/include/vadefs.h'
# Build 441 lost all 1336 buildable targets to this, before compiling a line.
#
# It is fixed in rules_cc
# https://github.com/bazelbuild/rules_cc/commit/333af1f613817f0ca2239a5f6183a57c1cf5630a
# (PR #626, 2026-03-19), which is why 8.x and 9.x are fine: from Bazel 8 the
# cc_configure extension comes from ``@rules_cc``, so the 0.2.25 floor in
# ``build._BASELINE_MODULE_BAZEL`` carries the fix in. Bazel 7 gets the
# extension from ``@bazel_tools`` instead -- the log shows
# ``@@bazel_tools~cc_configure_extension~local_config_cc`` -- and that copy is
# baked into the release, so no module-graph floor can reach it. There is no
# supported way to extend ``cxx_builtin_include_directories`` out of band on
# Windows either. Nothing downstream can fix this; only a Bazel 7.7.2+ that
# backports the parse into its embedded tools, at which point drop this.
CLANG_CL_UNSUPPORTED_BAZEL_VERSIONS: frozenset[str] = frozenset({"7.x"})

# CI-ergonomic flags we want on every task, appended after the tag filters.
COMMON_TASK_FLAGS: list[str] = [
    "--keep_going",
]


def tag_filter_flags(excluded_tags: list[str]) -> list[str]:
    """Render the build/test tag-filter pair excluding *excluded_tags*."""
    joined = ",".join(f"-{tag}" for tag in excluded_tags)
    return [f"--build_tag_filters={joined}", f"--test_tag_filters={joined}"]


def parse_bazelrc(path: Path) -> dict[str | None, list[str]]:
    """Parse a .bazelrc into a map of config_name → list of flags.

    The special key ``None`` collects unconditional flags (lines like
    ``build --some-flag`` with no ``:config`` suffix). Named-config lines
    (``build:generic_clang --some-flag``) are aggregated under their config
    name. Multiple lines for the same config are concatenated in source
    order. Backslash continuations are joined before parsing. Comment
    lines and blank lines are ignored.

    Only ``common``/``build``/``test`` directives are aggregated — ``run``
    and ``query`` flags don't propagate to ``bazel test`` invocations.
    Imports are NOT currently followed (none of llvm-project's bazelrc
    uses ``import``; if it ever does, this function will need extending).
    """
    raw = path.read_text(encoding="utf-8")
    # Join backslash-continued lines so a single logical directive ends up
    # on one parsed line. Trailing-backslash + newline + leading whitespace
    # collapses to a single space.
    joined = re.sub(r"\\\n[ \t]*", " ", raw)

    configs: dict[str | None, list[str]] = collections.defaultdict(list)
    for raw_line in joined.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        m = _RC_LINE_RE.match(line)
        if not m:
            continue
        cmd, config_name, flags_str = m.groups()
        if cmd not in _AGGREGATED_COMMANDS:
            continue
        # shlex.split handles quoted args with embedded spaces and (with
        # comments=True) trims trailing `# ...` inline comments — `.bazelrc`
        # uses these heavily (e.g. ``build:msvc --copt=/wd4141 # inline used...``).
        flags = shlex.split(flags_str, comments=True)
        configs[config_name].extend(flags)
    return dict(configs)


def expand_config(
    configs: dict[str | None, list[str]],
    config_name: str,
    _seen: frozenset[str] | None = None,
) -> list[str]:
    """Recursively expand --config=X references in a named config's flags.

    Walks the flags for *config_name*. Each ``--config=Y`` flag is replaced
    by the expanded flags of ``Y`` (transitively). Cycles raise.
    """
    if _seen is None:
        _seen = frozenset()
    if config_name in _seen:
        chain = " → ".join(list(_seen) + [config_name])
        raise ValueError(f"Circular --config reference: {chain}")
    if config_name not in configs:
        raise KeyError(f"Config '{config_name}' not defined in bazelrc")

    next_seen = _seen | {config_name}
    result: list[str] = []
    for flag in configs[config_name]:
        if flag.startswith("--config="):
            inherited = flag[len("--config=") :]
            result.extend(expand_config(configs, inherited, next_seen))
        else:
            result.append(flag)
    return result


def flags_for(
    configs: dict[str | None, list[str]],
    config_name: str,
    dropped_flags: frozenset[str] = frozenset(),
    excluded_tags: list[str] | None = None,
) -> list[str]:
    """Compose the full flag list a task should pass to ``bazel test``.

    Order: unconditional bazelrc flags → expanded named-config flags →
    tag filters → common task flags → project invariant flags. (Later
    flags override earlier ones in Bazel's command-line semantics, and
    repeating ``--test_tag_filters`` replaces rather than extends, so the
    tag filters must be emitted as one already-joined pair.) Any flag in
    *dropped_flags* is filtered out at the end — used by tasks that
    can't satisfy a specific upstream `.bazelrc` assumption.
    """
    if excluded_tags is None:
        excluded_tags = COMMON_EXCLUDED_TAGS
    unconditional = configs.get(None, [])
    expanded = expand_config(configs, config_name)
    combined = [
        *unconditional,
        *expanded,
        *tag_filter_flags(excluded_tags),
        *COMMON_TASK_FLAGS,
        *PROJECT_INVARIANT_FLAGS,
    ]
    if not dropped_flags:
        return combined
    return [f for f in combined if f not in dropped_flags]


# Linux platforms for the presubmit matrix. bazelci accepts only names it
# knows (``PLATFORMS`` in bazelci.py) and retires old ones into
# ``EOL_PLATFORMS``, which now holds ubuntu1604, ubuntu1804 and every
# ubuntu2004 variant -- so ``ubuntu2004`` is replaced here by ``ubuntu2404``,
# which is bazelci's own ``DEFAULT_PLATFORM``.
#
# ``debian10`` stays. It is still in ``PLATFORMS`` (not EOL for bazelci's
# purposes) and it is the only thing in the matrix pinning the oldest
# toolchain this release is expected to survive -- gcc 8.3.0, which is
# precisely what patches 004 and 015 exist to accommodate. Dropping it would
# leave those patches untested.
DEFAULT_LINUX_PLATFORMS: list[str] = ["debian10", "ubuntu2404"]

# Per-task shell commands to run before `bazel test`. Upstream's
# `build:generic_clang` sets ``--linkopt=-fuse-ld=lld`` with the note
# "assume that anybody using clang also has lld available"; that holds
# for llvm-zorg's Google Bazel bot (which bakes ``lld-N`` into its
# custom image — see ``google-bazel-bot/docker/Dockerfile``) but not
# for bazelci's stock ``gcr.io/bazel-public/{debian10,ubuntu2404}``
# runners, where ``clang: error: invalid linker name in argument
# '-fuse-ld=lld'`` fires on every link. Install ``lld`` before the
# build so the flag resolves. ``debian10`` and ``ubuntu2404`` are both
# Debian-based and accept the same apt-get invocation.
_LINUX_CLANG_SHELL_COMMANDS: list[str] = [
    "sudo apt-get update",
    "sudo apt-get install -y --no-install-recommends lld",
]

# Flags to strip from the macOS clang tasks. Upstream's
# ``build:generic_clang`` ships ``--linkopt=-fuse-ld=lld``, but no
# upstream CI actually exercises bazel-on-macOS with lld: llvm-zorg's
# google-bazel-bot is Linux-only (no macOS Dockerfile, no macOS refs
# under ``google-bazel-bot/``), and the LLVM buildbot masters that do
# run macOS never invoke bazel. So the flag is an unverified claim on
# Mach-O. On the bazelci macOS runners it also can't be satisfied
# without extra setup: clang's ``-fuse-ld=lld`` looks for ``ld64.lld``
# (not ``ld.lld``) on Mach-O, and homebrew's ``lld`` formula is
# keg-only, so ``ld64.lld`` isn't on the sandbox PATH even after
# ``brew install lld``. Falling back to Apple's ``ld64`` is no worse
# than what upstream tests — which is nothing — so drop the flag.
_MACOS_CLANG_DROPPED_FLAGS: frozenset[str] = frozenset(
    {
        "--linkopt=-fuse-ld=lld",
        "--host_linkopt=-fuse-ld=lld",
    }
)

# Task structure — same shape across versions; only the .bazelrc expansion
# and the matrix entries differ. Each entry is:
#   (task_name, display_name, platform_expr, config_name,
#    shell_commands, dropped_flags)
# platform_expr is either a literal platform (e.g., "windows") or the
# matrix placeholder "${{ platform }}". shell_commands is a possibly-empty
# list of pre-`bazel test` commands (bazelci runs these via the task
# config's ``shell_commands`` field on Linux/macOS; omitted from the YAML
# entirely when empty). dropped_flags is a possibly-empty set of flags
# to strip after expansion (see ``_MACOS_CLANG_DROPPED_FLAGS``).
_TASK_SPEC: list[tuple[str, str, str, str, list[str], frozenset[str]]] = [
    (
        "run_tests",
        "bazel test //... (linux, clang)",
        "${{ platform }}",
        "generic_clang",
        _LINUX_CLANG_SHELL_COMMANDS,
        frozenset(),
    ),
    ("run_tests_gcc", "bazel test //... (linux, gcc)", "${{ platform }}", "generic_gcc", [], frozenset()),
    (
        "run_tests_macos",
        "bazel test //... (macOS x86_64, clang)",
        "macos",
        "generic_clang",
        [],
        _MACOS_CLANG_DROPPED_FLAGS,
    ),
    (
        "run_tests_macos_arm64",
        "bazel test //... (macOS arm64, clang)",
        "macos_arm64",
        "generic_clang",
        [],
        _MACOS_CLANG_DROPPED_FLAGS,
    ),
    ("run_tests_windows_clang_cl", "bazel test //... (windows, clang-cl)", "windows", "clang-cl", [], frozenset()),
    ("run_tests_windows_msvc", "bazel test //... (windows, msvc)", "windows", "msvc", [], frozenset()),
]


def render_presubmit(
    bazelrc_path: Path,
    linux_platforms: list[str],
    bazel_versions: list[str],
) -> dict[str, Any]:
    """Build the presubmit.yml dict for a given prepared source's .bazelrc."""
    configs = parse_bazelrc(bazelrc_path)

    # bazelci's ``matrix.exclude`` is keyed only on matrix attributes, so it
    # cannot drop a combination from one task and keep it in the others. A
    # second attribute can: only the clang-cl task reads it.
    clang_cl_versions = [v for v in bazel_versions if v not in CLANG_CL_UNSUPPORTED_BAZEL_VERSIONS]
    split_clang_cl_matrix = bool(clang_cl_versions) and clang_cl_versions != bazel_versions

    tasks: dict[str, Any] = {}
    for task_name, display, platform, config_name, shell_commands, dropped_flags in _TASK_SPEC:
        task: dict[str, Any] = {
            "name": display,
            "platform": platform,
            "bazel": "${{ bazel }}",
        }
        if shell_commands:
            task["shell_commands"] = list(shell_commands)
        excluded_tags = [*COMMON_EXCLUDED_TAGS]
        excluded_targets: list[str] = []
        if platform == "windows":
            excluded_tags += WINDOWS_EXCLUDED_TAGS
            excluded_targets += WINDOWS_EXCLUDED_TARGETS
            task["shards"] = WINDOWS_SHARDS
            if config_name == "clang-cl" and split_clang_cl_matrix:
                task["bazel"] = "${{ bazel_clang_cl }}"
        task["test_flags"] = flags_for(configs, config_name, dropped_flags, excluded_tags)
        task["test_targets"] = ["@llvm-project//...", *excluded_targets]
        tasks[task_name] = task

    matrix: dict[str, list[str]] = {
        "platform": linux_platforms,
        "bazel": bazel_versions,
    }
    if split_clang_cl_matrix:
        matrix["bazel_clang_cl"] = clang_cl_versions

    return {"matrix": matrix, "tasks": tasks}


_HEADER = """\
# Generated by `bazel run //tools:render_presubmit -- --llvm-version {llvm_version}`.
# DO NOT EDIT BY HAND — re-run the renderer if you need to change the structure
# or to pick up upstream `.bazelrc` changes from this version's prepared source.
#
# The renderer expands `--config=X` references from this version's `.bazelrc`
# into the literal flags they select, so the test workspace bazelci synthesizes
# (which has no `.bazelrc` of its own) can resolve every flag without needing
# to find the config definition elsewhere.
"""


def emit_yaml(rendered: dict[str, Any], llvm_version: str) -> str:
    body: str = yaml.dump(rendered, sort_keys=False, default_flow_style=False, width=200)
    return _HEADER.format(llvm_version=llvm_version) + body


def _resolve_prepared_source(repo_root: Path, llvm_version: str, versions_dir: Path) -> Path:
    """Locate the prepared source tree for *llvm_version*.

    Returns ``<repo>/build/<llvm_version>/llvm-project-<version>.bzl``, where
    ``version`` is read from ``versions/<llvm_version>/version.txt``. Errors
    with a clear message if the tree doesn't exist — the user must run
    ``cherry_pick prepare --llvm-version <llvm_version>`` first.
    """
    version_file = versions_dir / llvm_version / "version.txt"
    if not version_file.is_file():
        raise SystemExit(f"ERROR: missing {version_file}")
    version = version_file.read_text().strip()

    tree = repo_root / "build" / llvm_version / f"llvm-project-{version}.bzl"
    if not (tree / ".bazelrc").is_file():
        raise SystemExit(
            f"ERROR: {tree}/.bazelrc not found. Run:\n"
            f"  bazel run //tools:cherry_pick -- prepare --llvm-version {llvm_version}"
        )
    return tree


def main() -> None:
    std_logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=std_logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--llvm-version", required=True, help="Version directory under versions/ (e.g. 17.0.5)")
    parser.add_argument(
        "--versions-dir",
        type=_user_cwd_path,
        help="Path to versions/ directory (default: <repo>/versions)",
    )
    parser.add_argument(
        "--bazelrc",
        type=_user_cwd_path,
        help=(
            "Path to a .bazelrc to render from (default: read from the prepared "
            "source at build/<llvm_version>/llvm-project-<version>.bzl/.bazelrc, "
            "which requires `cherry_pick prepare` to have been run). Use this "
            "flag for seed-time generation when no prepared source exists yet "
            "(e.g. the new-version auto-PR workflow can download upstream's "
            "utils/bazel/.bazelrc directly via curl)."
        ),
    )
    parser.add_argument(
        "--linux-platforms",
        default=",".join(DEFAULT_LINUX_PLATFORMS),
        help=f"Comma-separated linux platform values for the matrix (default: {','.join(DEFAULT_LINUX_PLATFORMS)})",
    )
    parser.add_argument(
        "--bazel-versions",
        default="7.x,8.x,9.x",
        help="Comma-separated bazel versions for the matrix (default: 7.x,8.x,9.x)",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=_user_cwd_path,
        help="Write the rendered YAML here (default: versions/<llvm_version>/presubmit.yml)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Print a diff vs. the existing file and exit non-zero on drift; do not write.",
    )
    args = parser.parse_args()

    repo_root = _workspace_root()
    versions_dir = args.versions_dir or repo_root / "versions"

    if args.bazelrc is not None:
        bazelrc_path = args.bazelrc
        if not bazelrc_path.is_file():
            raise SystemExit(f"ERROR: --bazelrc {bazelrc_path} does not exist")
    else:
        tree = _resolve_prepared_source(repo_root, args.llvm_version, versions_dir)
        bazelrc_path = tree / ".bazelrc"

    rendered = render_presubmit(
        bazelrc_path=bazelrc_path,
        linux_platforms=[p.strip() for p in args.linux_platforms.split(",") if p.strip()],
        bazel_versions=[b.strip() for b in args.bazel_versions.split(",") if b.strip()],
    )
    output_text = emit_yaml(rendered, args.llvm_version)

    target = args.output or versions_dir / args.llvm_version / "presubmit.yml"

    if args.check:
        existing = target.read_text(encoding="utf-8") if target.is_file() else ""
        if existing == output_text:
            logging.info("%s is up to date.", target)
            return
        diff = "\n".join(
            difflib.unified_diff(
                existing.splitlines(),
                output_text.splitlines(),
                fromfile=str(target),
                tofile=str(target) + " (rendered)",
                lineterm="",
            )
        )
        sys.stdout.write(diff + "\n")
        raise SystemExit(
            f"ERROR: {target} is out of date with the renderer's output. Re-run without --check to update."
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    # Always UTF-8: the rendered header contains non-ASCII punctuation, and
    # Python's default encoding is the locale codepage on Windows (cp1252),
    # which would silently write mojibake into a checked-in file.
    target.write_text(output_text, encoding="utf-8")
    logging.info("Wrote %s", target)


if __name__ == "__main__":
    _cwd = os.environ.get("BUILD_WORKING_DIRECTORY")
    if _cwd:
        os.chdir(_cwd)
    main()
