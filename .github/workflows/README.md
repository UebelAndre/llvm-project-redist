# Workflows

## CI (`ci.yaml`)

Runs on every PR and push to `main`. Validates patches, runs pre-commit checks, and builds release archives for any changed version directories. On pushes to `main`, dispatches the release workflow for affected versions.

**If it fails:** Fix the issue and push again, or re-run the failed job. If the dispatch step fails after a successful build, manually trigger the release workflow from the Actions tab with the appropriate `llvm_version`.

## Check LLVM Release (`check-llvm-release.yaml`)

Runs twice daily on a schedule. Scans upstream llvm-project releases and opens a PR for any new version not yet tracked.

**If it fails:** Run the workflow manually from the Actions tab, optionally with a specific `llvm_version`. If the PR fails to open, the workflow is idempotent and will retry on the next scheduled run.

## Release (`release.yaml`)

Builds release archives, generates provenance attestations, and creates a GitHub release. Normally dispatched automatically by CI.

**If it fails:** Delete the failed release and its tag, fix the issue, then re-trigger the workflow from the Actions tab with the `llvm_version`. Release artifacts are never overwritten — a partial release must be deleted before retrying.

## BCR Publish (`bcr-publish.yaml`)

Opens a pull request to the Bazel Central Registry. Called automatically at the end of the release workflow.

**If it fails:** Fix the issue and manually trigger the workflow from the Actions tab with the release `tag_name` (e.g. `llvmorg-17.0.3.bcr.5`). The branch is force-pushed, so re-runs will update an existing PR rather than creating a duplicate.
