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

Each LLVM version has a directory under `releases/`:

```
releases/
  20.1.0/
    presubmit.yml        # required: BCR presubmit test config
    source.sha256         # auto-generated: upstream tarball integrity
    patches/
      001_fix_clang.patch  # git-formatted patches, applied in order
      002_fix_mlir.patch
```

### Adding a new LLVM version

Run the **Check LLVM Release** workflow from the Actions tab with the `llvm_tag` input (e.g. `llvmorg-20.1.0`). It bootstraps `releases/{version}/presubmit.yml` from the template and dispatches the release build automatically. The same workflow runs on a cron schedule to detect new upstream releases.

To do it manually:

1. Create `releases/{version}/presubmit.yml`. Copy `.bcr/presubmit.yml` as a starting point and adjust test targets, C++ standard flags, and platform support for the specific LLVM version.
2. Run the **Release** workflow with the `llvm_tag` input.

### Adding or updating patches

1. Add or modify git-formatted patch files in `releases/{version}/patches/` named `NNN_description.patch` where `NNN` is a zero-padded three-digit sequence number starting at `001`. You can add multiple patches in a single PR.
2. Include or update `releases/{version}/presubmit.yml` for the target version.
3. Open a pull request. CI will validate patch naming, build a preview artifact with all patches applied, and upload it for review.
4. On merge to `main`, a workflow automatically computes the next `.bcr.N` version and cuts a release containing all current patches.

### Patch rules

- Files must match `NNN_description.patch` (e.g. `001_fix_build.patch`).
- Numbers must start at `001` and be strictly sequential with no gaps.
- All patches are applied together with `patch -p1` from the source root in numeric order.
- Every version directory with content must include a `presubmit.yml`.

## Workflows

| Workflow | Trigger | Purpose |
|---|---|---|
| `check-llvm-release.yml` | Cron (every 6h), manual (`workflow_dispatch`) | Detects new upstream LLVM releases or seeds a specific version; bootstraps `releases/{version}/` and dispatches `release.yml` |
| `release.yml` | `workflow_dispatch`, `pull_request` | Builds the repackaged archive; on dispatch, publishes a release; on PR, uploads a preview artifact |
| `release-patched.yml` | Push to `main` touching `releases/**` | Computes next `.bcr.N` and dispatches `release.yml` |
| `bcr-publish.yml` | Release published | Submits the release to the Bazel Central Registry |

## Local development

```bash
# Run tests via Bazel
bazel test //scripts/...
```
