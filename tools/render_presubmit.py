#!/usr/bin/env python3
"""Render a version's presubmit.yml from ``tools/presubmit.template.yml``.

CI and bazel-central-registry run llvm-project as a *dependency* of a
synthetic test workspace, and Bazel only reads the root module's ``.bazelrc``.
So the named configs upstream defines there -- ``generic_clang``, ``msvc``,
``clang-cl``, ... -- are not resolvable from a ``presubmit.yml``: a bare
``--config=msvc`` fails with "Config value 'msvc' is not defined in any .rc
file". The flags have to be spelled out.

This tool does exactly one thing: it copies the template and replaces every
``--config=NAME`` list entry with the flags NAME selects in the version's
prepared (post-patch) ``.bazelrc`` -- the ``.bazelrc``'s unconditional flags
once per flag list, then NAME flattened recursively, in ``.bazelrc`` order.
Everything else in the template (comments, task names, matrix, literal flags)
is copied as written, so the template is the single place that says what
*this* CI runs, and upstream's ``.bazelrc`` is the single place that says how
LLVM builds.

Usage:
    bazel run //tools:render_presubmit -- --llvm-version 17.0.5
    bazel run //tools:render_presubmit -- --llvm-version 17.0.5 --check   # diff only
    bazel run //tools:render_presubmit -- --llvm-version 18.1.0 --bazelrc /tmp/upstream.bazelrc
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

import yaml

logging = std_logging.getLogger(__name__)

_SCRIPTS_DIR = Path(__file__).absolute().parent
_REPO_ROOT = _SCRIPTS_DIR.parent

TEMPLATE_RELATIVE_PATH = Path("tools") / "presubmit.template.yml"


def _workspace_root() -> Path:
    """Return the source tree this invocation should read from and write to.

    ``bazel run`` execs the script out of the runfiles tree, so a
    ``__file__``-relative root would point into runfiles rather than the
    checkout. ``BUILD_WORKSPACE_DIRECTORY`` -- set by ``bazel run`` to the
    workspace root -- is the right anchor; fall back to the ``__file__``
    -relative root so running the script directly still works.
    """
    env = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    if env:
        return Path(env)
    return _REPO_ROOT


def _user_cwd_path(s: str) -> Path:
    """Resolve a relative path against the shell's working directory.

    ``bazel run`` changes the process CWD before exec-ing the script, which
    would break relative paths the user typed. Anchor them to
    ``BUILD_WORKING_DIRECTORY`` (the invoking shell's CWD) when set.
    """
    p = Path(s)
    if p.is_absolute():
        return p
    base = os.environ.get("BUILD_WORKING_DIRECTORY") or os.getcwd()
    return Path(base) / p


# ---------------------------------------------------------------------------
# .bazelrc parsing
# ---------------------------------------------------------------------------

# Lines in .bazelrc look like:
#   <cmd>[:<config>] <flag> [<flag> ...]
# where <cmd> is one of: build, common, test, run, query, fetch, sync, etc.
_RC_LINE_RE = re.compile(r"^\s*(common|build|test)(?::([A-Za-z0-9_.-]+))?\s+(.+?)\s*$")

# Commands whose flags reach `bazel test`: `common` applies to every command,
# `build` flags propagate to `test`, `test` is the most specific layer.
_AGGREGATED_COMMANDS = frozenset({"common", "build", "test"})

Configs = dict[str | None, list[str]]


def parse_bazelrc(path: Path) -> Configs:
    """Parse a .bazelrc into a map of config name -> flags, in source order.

    The key ``None`` collects unconditional flags (``build --flag`` with no
    ``:config`` suffix). Multiple lines for one config concatenate. Backslash
    continuations are joined first; comments and blank lines are skipped.
    Only ``common``/``build``/``test`` directives are kept -- ``run``,
    ``query`` etc. never reach ``bazel test``. ``import``/``try-import`` are
    not followed; llvm-project's .bazelrc only try-imports an optional
    user.bazelrc.
    """
    raw = path.read_text(encoding="utf-8")
    joined = re.sub(r"\\\n[ \t]*", " ", raw)

    configs: Configs = collections.defaultdict(list)
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
        # shlex handles quoted args and (comments=True) strips the trailing
        # `# ...` that llvm-project's .bazelrc puts after many flags.
        configs[config_name].extend(shlex.split(flags_str, comments=True))
    return dict(configs)


def expand_config(configs: Configs, config_name: str, _seen: frozenset[str] = frozenset()) -> list[str]:
    """Flatten a named config, recursively replacing nested ``--config=Y``."""
    if config_name in _seen:
        chain = " -> ".join([*_seen, config_name])
        raise ValueError(f"Circular --config reference: {chain}")
    if config_name not in configs:
        raise KeyError(config_name)

    result: list[str] = []
    for flag in configs[config_name]:
        if flag.startswith("--config="):
            result.extend(expand_config(configs, flag[len("--config=") :], _seen | {config_name}))
        else:
            result.append(flag)
    return result


# ---------------------------------------------------------------------------
# Template expansion
# ---------------------------------------------------------------------------

# A flag-list key in the template: `test_flags:` / `build_flags:`.
_FLAG_LIST_KEY_RE = re.compile(r"^(?P<indent>[ ]*)(?:test_flags|build_flags):[ ]*$")
# A `--config=NAME` list entry, optionally quoted, optionally commented.
_CONFIG_ITEM_RE = re.compile(
    r"^(?P<indent>[ ]*)-[ ]+(?P<q>['\"]?)--config=(?P<name>[A-Za-z0-9_.-]+)(?P=q)[ ]*(?:#.*)?$"
)
# Flags that can be written as a YAML plain scalar. Everything else is
# single-quoted. Every flag starts with `-`, which YAML only treats specially
# when followed by a space, and the character class excludes spaces.
_PLAIN_FLAG_RE = re.compile(r"--?[A-Za-z0-9_./:=+,%@-]*")


def _yaml_scalar(flag: str) -> str:
    if _PLAIN_FLAG_RE.fullmatch(flag):
        return flag
    return "'" + flag.replace("'", "''") + "'"


def render_template(template: str, configs: Configs) -> str:
    """Return *template* with every ``--config=NAME`` entry expanded.

    Within each ``test_flags``/``build_flags`` list the first expansion is
    preceded by the .bazelrc's unconditional flags -- what Bazel would have
    applied had it read the file -- and later expansions are not, so they are
    never repeated. Each expansion is introduced by a comment naming the
    config it came from.
    """
    out: list[str] = []
    in_list = False
    list_indent = 0
    unconditional_emitted = False

    for line in template.splitlines():
        key = _FLAG_LIST_KEY_RE.match(line)
        if key:
            in_list = True
            list_indent = len(key.group("indent"))
            unconditional_emitted = False
            out.append(line)
            continue

        if in_list:
            stripped = line.lstrip(" ")
            indent = len(line) - len(stripped)
            is_item = stripped.startswith("- ") and indent >= list_indent
            if stripped and not stripped.startswith("#") and not is_item:
                in_list = False  # the list ended; fall through to copy the line

        item = _CONFIG_ITEM_RE.match(line) if in_list else None
        if not item:
            if in_list and "--config=" in line:
                raise ValueError(f"unrecognised --config entry in template: {line.strip()!r}")
            out.append(line)
            continue

        pad = item.group("indent")
        name = item.group("name")
        try:
            flags = expand_config(configs, name)
        except KeyError as e:
            raise SystemExit(
                f"ERROR: template references --config={e.args[0]}, which the .bazelrc does not define"
            ) from None
        if not unconditional_emitted:
            unconditional = configs.get(None, [])
            if unconditional:
                out.append(f"{pad}# .bazelrc: unconditional flags")
                out.extend(f"{pad}- {_yaml_scalar(f)}" for f in unconditional)
            unconditional_emitted = True
        out.append(f"{pad}# .bazelrc: --config={name}")
        out.extend(f"{pad}- {_yaml_scalar(f)}" for f in flags)

    rendered = "\n".join(out) + "\n"
    # The output must stand on its own: parse it, and make sure no config
    # reference survived (a nested one inside a string, say).
    doc = yaml.safe_load(rendered)
    for task_name, task in (doc.get("tasks") or {}).items():
        for key_name in ("test_flags", "build_flags"):
            for flag in task.get(key_name) or []:
                if isinstance(flag, str) and flag.startswith("--config="):
                    raise ValueError(f"{task_name}.{key_name}: {flag} was not expanded")
    return rendered


_HEADER = """\
# Rendered by `bazel run //tools:render_presubmit -- --llvm-version {llvm_version}`
# from tools/presubmit.template.yml and this version's prepared .bazelrc. Every
# `--config=NAME` entry in the template was replaced by the flags NAME selects
# there; everything else is copied from the template as written.
#
# This file belongs to {llvm_version}: hand edits it needs are fine. Re-render
# after a patch changes the .bazelrc, or run with `--check` to see the drift.
"""


def _strip_leading_comment(text: str) -> str:
    """Drop the template's own header so the rendered header replaces it."""
    lines = text.splitlines()
    i = 0
    while i < len(lines) and (not lines[i].strip() or lines[i].lstrip().startswith("#")):
        i += 1
    return "\n".join(lines[i:]) + "\n"


def render(template_path: Path, bazelrc_path: Path, llvm_version: str) -> str:
    configs = parse_bazelrc(bazelrc_path)
    body = _strip_leading_comment(template_path.read_text(encoding="utf-8"))
    return _HEADER.format(llvm_version=llvm_version) + render_template(body, configs)


def _resolve_prepared_bazelrc(repo_root: Path, llvm_version: str, versions_dir: Path) -> Path:
    """Locate the prepared source's .bazelrc for *llvm_version*.

    That is ``<repo>/build/<llvm_version>/llvm-project-<version>.bzl/.bazelrc``
    with ``version`` from ``versions/<llvm_version>/version.txt``; it exists
    after ``cherry_pick prepare`` / ``build --prepare-only`` has run.
    """
    version_file = versions_dir / llvm_version / "version.txt"
    if not version_file.is_file():
        raise SystemExit(f"ERROR: missing {version_file}")
    version = version_file.read_text(encoding="utf-8").strip()
    bazelrc = repo_root / "build" / llvm_version / f"llvm-project-{version}.bzl" / ".bazelrc"
    if not bazelrc.is_file():
        raise SystemExit(
            f"ERROR: {bazelrc} not found. Run:\n"
            f"  bazel run //tools:cherry_pick -- prepare --llvm-version {llvm_version}"
        )
    return bazelrc


def main() -> None:
    std_logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=std_logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--llvm-version", required=True, help="Version directory under versions/ (e.g. 17.0.5)")
    parser.add_argument("--versions-dir", type=_user_cwd_path, help="Path to versions/ (default: <repo>/versions)")
    parser.add_argument(
        "--template",
        type=_user_cwd_path,
        help=f"Template to render (default: <repo>/{TEMPLATE_RELATIVE_PATH.as_posix()})",
    )
    parser.add_argument(
        "--bazelrc",
        type=_user_cwd_path,
        help=(
            "The .bazelrc to expand --config= entries from (default: the prepared "
            "source's, which requires `cherry_pick prepare` to have run). Seeding a "
            "brand-new version passes upstream's .bazelrc for the tag here; with no "
            "patches yet, that is the post-patch file."
        ),
    )
    parser.add_argument(
        "--output", "-o", type=_user_cwd_path, help="Write here (default: versions/<llvm_version>/presubmit.yml)"
    )
    parser.add_argument(
        "--check", action="store_true", help="Diff against the existing file and exit non-zero on drift; do not write."
    )
    args = parser.parse_args()

    repo_root = _workspace_root()
    versions_dir = args.versions_dir or repo_root / "versions"
    template = args.template or repo_root / TEMPLATE_RELATIVE_PATH
    if not template.is_file():
        raise SystemExit(f"ERROR: template {template} does not exist")
    if args.bazelrc is not None:
        bazelrc = args.bazelrc
        if not bazelrc.is_file():
            raise SystemExit(f"ERROR: --bazelrc {bazelrc} does not exist")
    else:
        bazelrc = _resolve_prepared_bazelrc(repo_root, args.llvm_version, versions_dir)

    output_text = render(template, bazelrc, args.llvm_version)
    target = args.output or versions_dir / args.llvm_version / "presubmit.yml"

    if args.check:
        existing = target.read_text(encoding="utf-8") if target.is_file() else ""
        if existing == output_text:
            logging.info("%s matches a fresh render.", target)
            return
        sys.stdout.write(
            "\n".join(
                difflib.unified_diff(
                    existing.splitlines(),
                    output_text.splitlines(),
                    fromfile=str(target),
                    tofile=str(target) + " (rendered)",
                    lineterm="",
                )
            )
            + "\n"
        )
        raise SystemExit(f"{target} differs from a fresh render (see diff above).")

    target.parent.mkdir(parents=True, exist_ok=True)
    # Always UTF-8: Python's default on Windows is the locale codepage.
    target.write_text(output_text, encoding="utf-8")
    logging.info("Wrote %s", target)


if __name__ == "__main__":
    main()
