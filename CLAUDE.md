# build-agent

Reusable GitHub Actions workflow repo for building and releasing **Capacitor** (React + Capacitor) apps to Google Play and TestFlight. Also used for Unity game projects. Workflows are designed to be **called** from individual project repos via `workflow_call`, not triggered directly in this repo.

## Repo structure

```
.github/workflows/capacitor.yml      # Reusable workflow for Capacitor apps
scripts/push-secrets.sh              # Pushes secrets/variables to target GitHub repos via gh CLI
.secrets.env.example                 # Combined template for shared + project-specific values
.secrets.env                         # Shared secrets (Slack, Asana) — gitignored, never commit
<project_name>/
  .secrets.env                       # Project-specific secrets (keystore, certs, app IDs) — gitignored
```

**Secret layering:** root `.secrets.env` is loaded first, then `<project_name>/.secrets.env` on top. Project values override root values for the same key.

## How the workflow is used

Caller repos reference it like:
```yaml
uses: webavenue/build-agent/.github/workflows/capacitor.yml@main
```

### Workflow inputs
| Input | Default | Description |
|---|---|---|
| `action` | `"build + upload"` | `"build + upload"` or `"build only"` |
| `build_android` | `true` | Whether to build the Android app |
| `build_ios` | `true` | Whether to build the iOS app |
| `version_name` | `"1.0.0"` | Semantic version string |
| `release_notes` | `"Bug fixes..."` | Changelog text for stores |
| `version_code_offset` | `100` | Added to `github.run_number` to compute build code |

> **Note:** iOS job only runs on `action: "build + upload"` — there is no build-only option for iOS.

## Secrets and variables

Secrets/variables must be set on the **caller repo**, not this one. Use `push-secrets.sh` to push them.

### Conventions in `.secrets.env`
- `[secrets]` section → pushed as **GitHub Secrets** (masked in logs)
- `[variables]` section → pushed as **GitHub Variables** (visible in logs)
- `_BASE64` or `_B64` suffix → value is a **file path**; the script reads and base64-encodes the file before pushing

### Key secrets/variables required by the workflow
**Android:** `ANDROID_KEYSTORE_BASE64`, `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_PASSWORD`, `GOOGLE_PLAY_JSON_KEY_BASE64`  
**iOS:** `APPLE_API_KEY_BASE64`, `APPLE_DIST_CERT_BASE64`, `APPLE_DIST_CERT_PASSWORD`, `APPLE_PROVISIONING_PROFILE_BASE64`, `APPLE_API_KEY_ID`, `APPLE_API_ISSUER_ID`  
**App config (vars):** `APP_NAME`, `ANDROID_PACKAGE_NAME`, `IOS_BUNDLE_ID`, `IOS_SCHEME`, `GOOGLE_PLAY_TRACK`, `TESTFLIGHT_GROUPS`, `APPLE_TEAM_ID`  
**Notifications:** `SLACK_BOT_TOKEN`, `ASANA_ACCESS_TOKEN`, `SLACK_CHANNEL_ID`, `ASANA_PROJECT_ID`, etc.  
**AI failure analysis (optional):** `ANTHROPIC_API_KEY` — when set, the notify job posts a plain-English diagnosis of build failures as a threaded Slack reply. Skipped silently if unset.

## push-secrets.sh

Merges root + project secrets and pushes to one or more GitHub repos.

```bash
# Shared + project-specific secrets (project overrides root):
./scripts/push-secrets.sh --project my-app webavenue/my-app-repo

# Shared secrets only (no project):
./scripts/push-secrets.sh webavenue/some-repo

# Multiple repos:
./scripts/push-secrets.sh --project my-app webavenue/repo1 webavenue/repo2

# Via env var:
GITHUB_REPOS="webavenue/repo1" ./scripts/push-secrets.sh --project my-app

# Dry-run — shows what would be pushed without calling gh:
DRY_RUN=1 ./scripts/push-secrets.sh --project my-app webavenue/my-app-repo
```

**Prerequisites:** `gh` CLI installed and authenticated (`gh auth login`). At least one of root `.secrets.env` or `<project>/.secrets.env` must exist.

## Workflow internals / gotchas

- **Android:** ubuntu-latest, Java 21, Node 22, Ruby 3.1.6. Uses Fastlane.
- **iOS:** macos-15, Xcode 26, Node 22, Ruby 3.1.6, CocoaPods 1.16.2. Uses Fastlane.
- **Keystore conversion:** Android job converts PKCS12 keystore → JKS before signing. This is required because older BouncyCastle versions in the Android Gradle Plugin can't parse PKCS12 keystores created with JDK 9+.
- **Version code formula:** `version_code_offset + github.run_number`. Set different offsets per project to prevent collisions when multiple projects share the same workflow.
- **Notifications:** After both Android and iOS jobs finish, a notify job creates Asana QA tasks and posts a Slack message with build status, download links, and release notes.
- **Failure diagnosis:** On Android or iOS failure, the notify job pulls the failed-step logs via `gh run view --log-failed`, asks Claude Haiku 4.5 for a 2–4 sentence plain-English diagnosis, and posts it as a threaded reply to the Slack failure message. Requires `ANTHROPIC_API_KEY` on the caller repo; silently skipped if absent. Needs `actions: read` permission on the notify job (already declared).
