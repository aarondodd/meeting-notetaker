# Release Process

How to ship a new version of Meeting Notetaker. The pipeline is fully
automated: tag push -> Windows runner builds + signs (in future) +
installs Inno Setup wrapper -> GitHub Release with installer attached.

## Prerequisites (one-time, already done)

- Repo is **public** so `windows-latest` runner minutes + release-asset
  storage are unmetered (truly $0).
- `default_workflow_permissions = "write"` on the repo so `GITHUB_TOKEN`
  can create releases and upload assets. Verify:
  ```bash
  gh api repos/aarondodd/meeting-notetaker/actions/permissions/workflow
  ```
- The workflow lives at `.github/workflows/release.yml` and triggers on
  `push` of any `v*` tag.

## Release sequence

Run this top-to-bottom. The whole thing is ~10-20 minutes wall clock,
most of which is the Windows runner building.

### 1. Pre-flight

From the working tree on `main` or the release-prep branch:

```bash
# Tests pass locally?
python -m pytest tests/ -q

# Version in version.py reflects what you're about to ship?
cat meeting_notetaker/version.py

# Any uncommitted changes that should be in the release?
git status
```

If `version.py` ends in `-dev`, drop the suffix in a commit on the
release-prep branch / PR before tagging. The version baked into the
installer comes from the tag itself, but `__version__` is what the
in-app About dialog + self-updater compare against.

### 2. Merge the release PR

If the work is on a PR (e.g. `v0.6.6-dev` for v0.6.6):

```bash
PR=21   # the release PR number
gh pr ready $PR                                # un-draft if it's a draft
gh pr merge $PR --squash --delete-branch
```

Sync local main:

```bash
git checkout main && git fetch origin && git reset --hard origin/main
git log --oneline -3
```

The top commit should be the squash from your PR.

### 3. Tag + push

```bash
VERSION=0.6.6
git tag -a v$VERSION -m "v$VERSION: <one-line summary>"
git push origin v$VERSION
```

The tag push fires `.github/workflows/release.yml` automatically. You
don't need to trigger anything else.

### 4. Watch the build

```bash
RUN_ID=$(gh run list --workflow=release.yml --limit 1 --json databaseId --jq '.[0].databaseId')
gh run watch $RUN_ID --exit-status
```

`gh run watch` blocks until the run completes (success or failure) and
exits non-zero if it failed. Typical run time: 8-15 min on a cold cache,
3-5 min on a warm `pip cache` hit.

### 5. Verify the release

```bash
gh release view v$VERSION --json name,tagName,assets --jq '{name, tag: .tagName, assets: [.assets[] | {name, size}]}'
```

Expected output:
```json
{
  "name": "v0.6.6",
  "tag": "v0.6.6",
  "assets": [
    {"name": "meeting-notetaker-setup-0.6.6.exe", "size": <somewhere ~250-400 MB>}
  ]
}
```

If the asset is there with a sensible size, the release is done.

### 6. Smoke install (optional but recommended for major versions)

Download the installer on a Windows box:

```
https://github.com/aarondodd/meeting-notetaker/releases/download/v$VERSION/meeting-notetaker-setup-$VERSION.exe
```

- Runs without admin (per-user install by default)
- Appears in Start Menu as "Meeting Notetaker"
- Appears in Settings > Apps & features under "Aaron Dodd" publisher
- App launches; Help > About shows v$VERSION
- Uninstall removes the install dir + Start Menu entry; leaves
  `%APPDATA%\MeetingNotetaker\` (sessions) intact

## What gets shipped

- **`meeting-notetaker-setup-X.Y.Z.exe`** -- the Inno Setup installer.
  Wraps the PyInstaller `--onedir` output (`dist/meeting-notetaker/`)
  with LZMA2/ultra compression. Single user-facing download.

That's it. The raw `meeting-notetaker.exe` launcher is NOT attached --
under `--onedir` it can't run without its sibling DLL/data tree, so
shipping it standalone would just confuse users.

## Self-upgrade behavior

Once a user has v$N installed, the in-app weekly check (and Help >
Check for Updates) compares their version against
`releases/latest`. If v$N+1 is newer, Help > Upgrade downloads
`meeting-notetaker-setup-$(N+1).exe` from the release, runs it with
`/SILENT /SUPPRESSMSGBOXES`, and Inno Setup's Restart Manager hooks
close + relaunch the app after the upgrade. Stable `AppId` in
`installer.iss` (`{B1F03D8E-...}`) means this is an in-place upgrade,
not a side-by-side install.

Users running from source or a portable PyInstaller build get a
"upgrade via your own workflow" message instead of an installer launch.

## Common failure modes

(Populate this section as we encounter and fix things. Each entry
should be: symptom -> root cause -> fix -> commit reference.)

### Workflow run fails with "Resource not accessible by integration"

Symptom: `softprops/action-gh-release` step exits with an HTTP 403 from
the GitHub API.

Root cause: `default_workflow_permissions` is set to `"read"` at the
repo level, and the per-workflow `permissions: contents: write` block
isn't winning.

Fix:
```bash
gh api -X PUT repos/aarondodd/meeting-notetaker/actions/permissions/workflow \
  -F default_workflow_permissions=write \
  -F can_approve_pull_request_reviews=false
```

Then re-run the workflow:
```bash
gh run rerun $RUN_ID
```

### Workflow run fails at the build step with "MISSING" in --check-deps

Symptom: PowerShell errors out with non-zero exit + a dependency report
showing one or more packages as MISSING.

Root cause: PyInstaller couldn't statically resolve a new package; the
spec file's `collect_all()` list or `hiddenimports` needs to grow.

Fix: identify the MISSING package from the log, add it to
`meeting_notetaker.spec`'s `collect_all` loop (preferred) or
`hiddenimports` list, AND add a matching `_Check` entry to
`meeting_notetaker/utils/dependency_check.py` so the build-gate catches
it next time. Commit + push to main; the workflow will NOT auto-re-run
on a tag, so:

```bash
git tag -d v$VERSION                 # delete local tag
git push origin :refs/tags/v$VERSION # delete remote tag
gh release delete v$VERSION --yes    # delete the failed release (if it exists)
git tag -a v$VERSION -m "..."        # recreate tag at new HEAD
git push origin v$VERSION
```

### Inno Setup compile fails

Symptom: `ISCC.exe` exits non-zero; usually a `[Files]` source path
issue or a syntax error.

Root cause: typo in `installer.iss`, or the PyInstaller output isn't
where the spec assumed.

Fix: read `Output/` listing in the workflow log; verify `dist/meeting-
notetaker/` exists with `meeting-notetaker.exe` at its root. Adjust
`installer.iss` if the layout shifted.

### Release exists but installer is missing

Symptom: `gh release view` shows the release but `assets: []`.

Root cause: usually `softprops/action-gh-release` ran successfully but
the file glob matched nothing.

Fix: check the workflow log for the "Upload installer" step. The
`files:` directive expects `Output/meeting-notetaker-setup-${VERSION}.exe`
where VERSION is extracted from the tag. Tag format `v0.6.6` -> version
`0.6.6` -> file `meeting-notetaker-setup-0.6.6.exe`. If the version
extraction broke, you'll see it in the "Derive installer version" step.
