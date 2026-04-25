# llvm-project-redist

Redistributes [llvm-project](https://github.com/llvm/llvm-project) source archives with the Bazel build overlay pre-applied, and publishes them to the [Bazel Central Registry](https://github.com/bazelbuild/bazel-central-registry) as the `llvm-project` module.

## How it works

When LLVM cuts a release, a GitHub Actions workflow downloads the upstream `llvm-project-*.src.tar.xz`, applies the Bazel overlay from `utils/bazel/llvm-project-overlay`, transforms `MODULE.bazel` and `extensions.bzl` for standalone use, and repackages the result as a deterministic `llvm-project-{version}.bzl.tar.xz` archive. The archive is published as a GitHub release and automatically submitted to the BCR.

## Version scheme

| Release type | Version | Tag | Example |
|---|---|---|---|
| Base release | `X.Y.Z` | `llvmorg-X.Y.Z` | `20.1.0` |
| Patched release | `X.Y.Z.bcr.N` | `llvmorg-X.Y.Z.bcr.N` | `20.1.0.bcr.1` |

Base releases match upstream LLVM versions. Patched releases (`.bcr.N`) incorporate community-contributed fixes for Bazel compatibility issues discovered after the upstream release. Each `.bcr.N` release applies **all** patches in the version's `patches/` directory -- a single BCR version can contain multiple patch files.

## Contributing patches

Each LLVM version has a directory under `versions/`:

```
versions/
  20.1.0/
    version.txt           # required: release version (e.g. 20.1.0 or 20.1.0.bcr.1)
    presubmit.yml         # required: BCR presubmit test config
    source.sha256         # auto-generated: upstream tarball integrity
    patches/
      001_fix_clang.patch  # git-formatted patches, applied in order
      002_fix_mlir.patch
```

### Adding a new LLVM version

Run the **Check LLVM Release** workflow from the Actions tab with the `llvm_version` input (e.g. `20.1.0`). It bootstraps `versions/{version}/presubmit.yml` from the template and creates a pull request for review. The same workflow runs on a cron schedule to detect new upstream releases.

To do it manually:

1. Create `versions/{version}/presubmit.yml`. Copy `.bcr/presubmit.yml` as a starting point and adjust test targets, C++ standard flags, and platform support for the specific LLVM version.
2. Run the **Release** workflow with the `llvm_version` input.

### Adding or updating patches

1. Add or modify git-formatted patch files in `versions/{version}/patches/` named `NNN_description.patch` where `NNN` is a zero-padded three-digit sequence number starting at `001`. You can add multiple patches in a single PR.
2. Update `versions/{version}/version.txt` to the desired `.bcr.N` version (e.g. `20.1.0.bcr.1`).
3. Include or update `versions/{version}/presubmit.yml` for the target version.
4. Open a pull request. CI will validate patch naming and `version.txt`, build the artifact, and upload it for review.
5. On merge to `main`, CI reads `version.txt` and dispatches a release with that version.

### Patch rules

- Files must match `NNN_description.patch` or `NNN-description.patch` (e.g. `001_fix_build.patch`, `001-fix-build.patch`).
- Numbers must start at `001` and be strictly sequential with no gaps.
- All patches are applied together with `patch -p1` from the source root in numeric order.
- Every version directory with content must include a `version.txt` and `presubmit.yml`.
- `version.txt` must contain the directory name (e.g. `20.1.0`) or the directory name with a `.bcr.N` suffix (e.g. `20.1.0.bcr.1`).

## Workflows

| Workflow | Trigger | Purpose |
|---|---|---|
| `ci.yaml` | `pull_request`, push to `main` touching `versions/**` | Validates patches, `version.txt`, and `presubmit.yml`; builds artifacts; runs presubmit targets on Linux; on merge, reads `version.txt` and dispatches `release.yaml` after tests pass |
| `check-llvm-release.yaml` | Cron (every 6h), manual (`workflow_dispatch`) | Detects new upstream LLVM releases or seeds a specific version; bootstraps `versions/{version}/` and dispatches `release.yaml` |
| `release.yaml` | `workflow_dispatch` | Builds the repackaged archive and publishes a GitHub release |
| `bcr-publish.yaml` | Release published | Submits the release to the Bazel Central Registry |

## Local development

CI and presubmit tooling use **`bazel run //tools:…`** so Python deps (PyYAML) come from the locked [`tools/requirements.txt`](tools/requirements.txt) (`@pip_deps`). Regenerate the lock from [`tools/requirements.in`](tools/requirements.in) with:

```bash
bazel run //tools:requirements.update
```

```bash
# Build a version locally (output in build/<version>/ under the repo root)
bazel run //tools:build -- --llvm-version 17.0.3

# Build a BCR patched version
bazel run //tools:build -- --llvm-version 17.0.3 --bcr-version 1

# Validate version directories
bazel run //tools:validate_patches -- versions

# Run tests via Bazel
bazel test //tools/...
```

Arguments after `--` are passed to the script; paths are resolved from your shell cwd (`BUILD_WORKING_DIRECTORY`). Use `bazel run //tools:build -- --help` for all build options.

### Presubmit testing (BCR `presubmit.yml`)

Each `versions/{version}/presubmit.yml` follows the same shape as the [Bazel Central Registry](https://github.com/bazelbuild/bazel-central-registry) module presubmit format.

```bash
# Structural validation only (fast; no LLVM Bazel tests)
bazel run //tools:run_presubmit -- --validate --presubmit versions/17.0.3/presubmit.yml

# After building, run all tasks for the current OS family (linux / macos / windows)
bazel run //tools:run_presubmit -- \
  --presubmit versions/17.0.3/presubmit.yml \
  --run-host \
  --source-dir build/17.0.3/llvm-project-17.0.3.bzl

# Run one matrix-expanded task (disambiguate with --platform / --bazel)
bazel run //tools:run_presubmit -- \
  --presubmit versions/17.0.3/presubmit.yml \
  --run-task run_tests \
  --platform debian10 \
  --bazel 7.x \
  --source-dir build/17.0.3/llvm-project-17.0.3.bzl

# Preview the dynamic Buildkite pipeline JSON (no upload)
bazel run //tools:run_presubmit -- --pipeline --dry-run
```

**GitHub Actions:** `ci.yaml` uses Bazelisk, then `bazel run //tools:validate_patches`, `bazel run //tools:run_presubmit -- --validate`, `bazel run //tools:build`, and `bazel run //tools:run_presubmit -- --run-host` on Ubuntu (linux presubmit tasks only). Releases dispatch only after the test job succeeds.

**BuildKite:** `.bazelci/presubmit.yml` runs `bazel run //tools:run_presubmit -- --pipeline`, which detects changed `versions/*/` directories, expands each version's matrix, and uploads steps. Each generated step runs `bazel run //tools:build` then `bazel run //tools:run_presubmit` for one `(task, platform, bazel)` on the matching agent queue (`default`, `macos`, `macos_arm64`, `windows`).
