# build-agent

Reusable GitHub Actions workflow repo for building and releasing **Capacitor** (React + Capacitor) apps to Google Play and TestFlight. Also used for Unity game projects. Workflows are designed to be **called** from individual project repos via `workflow_call`, not triggered directly in this repo.

## Repo structure

```
.github/workflows/capacitor.yml             # Reusable workflow for Capacitor apps (GitHub-hosted runners)
.github/workflows/capacitor-selfhosted.yml  # Same workflow but for self-hosted Mac mini (Android + iOS sequential, automatic iOS signing)
docs/self-hosted-setup.md                   # Runbook for installing the Mac mini runner + Fastfile changes
scripts/push-secrets.sh                     # Pushes secrets/variables to target GitHub repos via gh CLI
.secrets.env.example                        # Combined template for shared + project-specific values
.secrets.env                                # Shared secrets (Slack, Asana) — gitignored, never commit
<project_name>/
  .secrets.env                              # Project-specific secrets (keystore, certs, app IDs) — gitignored
```

**Secret layering:** root `.secrets.env` is loaded first, then `<project_name>/.secrets.env` on top. Project values override root values for the same key.

## How the workflow is used

Caller repos reference it like:
```yaml
uses: webavenue/build-agent/.github/workflows/capacitor.yml@main
```

Or, for the self-hosted variant (one Mac mini, runs Android → iOS sequentially, automatic iOS signing):
```yaml
uses: webavenue/build-agent/.github/workflows/capacitor-selfhosted.yml@main
```

The self-hosted variant takes the same inputs and uses the same caller-repo secrets/vars — see [docs/self-hosted-setup.md](docs/self-hosted-setup.md) for runner install + the required `ship_auto` Fastlane lane.

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
**iOS (cloud):** `APPLE_API_KEY_BASE64`, `APPLE_DIST_CERT_BASE64`, `APPLE_DIST_CERT_PASSWORD`, `APPLE_PROVISIONING_PROFILE_BASE64`, `APPLE_API_KEY_ID`, `APPLE_API_ISSUER_ID`  
**iOS (self-hosted, additional):** `MAC_LOGIN_PASSWORD` (Mac mini login password — used to unlock the keychain before codesign). The cert/profile secrets above become unused since the cert lives in the host keychain and `sigh` fetches the profile via the API key.  
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

- **Android (cloud):** ubuntu-latest, Java 21, Node 22, Ruby 3.1.6. Uses Fastlane.
- **iOS (cloud):** macos-15, Xcode 26, Node 22, Ruby 3.1.6, CocoaPods 1.16.2. Uses Fastlane.
- **Self-hosted variant:** both Android + iOS on `[self-hosted, macOS, ARM64]` (one Mac mini), sequential — Android then iOS — because 16GB RAM can't reliably run Gradle + Xcode in parallel. Caller repos must add a `lane :ship_auto` to their Fastfile — see [docs/self-hosted-setup.md](docs/self-hosted-setup.md) for full setup.
- **Keystore conversion:** Android job converts PKCS12 keystore → JKS before signing. This is required because older BouncyCastle versions in the Android Gradle Plugin can't parse PKCS12 keystores created with JDK 9+. Applies to both cloud and self-hosted variants.
- **Version code formula:** `version_code_offset + github.run_number`. Set different offsets per project to prevent collisions when multiple projects share the same workflow.
- **Notifications:** After both Android and iOS jobs finish, a notify job creates Asana QA tasks and posts a Slack message with build status, download links, and release notes. Notify always runs on ubuntu-latest (both variants).
- **Failure diagnosis:** On Android or iOS failure, the notify job pulls the failed-step logs via `gh run view --log-failed`, asks Claude Haiku 4.5 for a 2–4 sentence plain-English diagnosis, and posts it as a threaded reply to the Slack failure message. Requires `ANTHROPIC_API_KEY` on the caller repo; silently skipped if absent. Needs `actions: read` permission on the notify job (already declared).

## Self-hosted Mac mini — non-obvious gotchas

These cost real time the first time we set up — capture them so future setups skip the trial-and-error. Full step-by-step is in [docs/self-hosted-setup.md](docs/self-hosted-setup.md).

- **Android SDK is not bundled.** Cloud runners get it via `actions/setup-java`. On self-hosted you install `android-commandlinetools` via Homebrew, run `sdkmanager --licenses`, install `platforms;android-XX` + `build-tools;XX.0.0` + `platform-tools` matching the project's `variables.gradle`, and put `ANDROID_HOME` in the runner's `.env` file. Without it, Gradle fails immediately with "SDK location not found".
- **Disable the Gradle daemon globally.** `echo "org.gradle.daemon=false" >> ~/.gradle/gradle.properties`. On self-hosted macOS the daemon's IPC with the gradlew wrapper hangs (visible as build "stuck" at `mapReleaseSourceSetPaths` with 0% CPU). One-shot JVM mode is ~5–10s slower per run but reliable.
- **iOS uses manual signing with a sigh-fetched profile, not pure automatic.** Pure `CODE_SIGN_STYLE=Automatic` + `-allowProvisioningUpdates` doesn't work for App Store archives — Xcode always tries to fetch a Development profile and conflicts with the Distribution cert. The `ship_auto` lane in the caller repo's Fastfile uses `sigh` (via App Store Connect API key) to create/refresh a distribution profile at build time, then patches just the App target to manual signing with that profile name. Pods/SPM packages keep their automatic-signing defaults.
- **API key needs App Manager or Admin role.** Sigh creates provisioning profiles, which requires elevated permissions. A Developer-role key worked on cloud (profile was hand-supplied) but fails on self-hosted. Mint a new key in App Store Connect → Users and Access → Integrations → Keys if your existing key is too narrow.
- **`set-key-partition-list` + per-build `unlock-keychain` are both needed.** Codesign under launchd hits `errSecInternalComponent` on the first 1–2 framework signings without these. Setup-time: run `security set-key-partition-list -S apple-tool:,apple:,codesign: ...` once. Per-build: the workflow runs `security unlock-keychain` using the `MAC_LOGIN_PASSWORD` secret. If the secret is missing, the unlock step is a soft no-op and you'll likely hit the codesign error.
- **Personal/free GitHub accounts use repo-scoped runners.** No org-level runner pool. Install one runner instance per caller repo under `~/actions-runners/<repo-shortname>/`. They coexist on the same Mac mini and don't pick up each other's jobs.
- **Some maven mirrors are slow from non-US locations and break dep resolution.** Capacitor projects with AppLovin/MAX mediation include `https://artifactory.bidmachine.io/bidmachine` in `android/build.gradle`. BidMachine mirrors Pangle, Chartboost, Mintegral, etc., but responds in 15–60s from outside the US and times out Gradle. On cloud (ubuntu-latest in US) you don't notice; on a self-hosted Mac mini elsewhere it hangs `mapReleaseSourceSetPaths`. **Fix in the caller project's `android/build.gradle`:** put BidMachine LAST in the `allprojects.repositories` block so vendor-specific repos (chartboost.jfrog.io, bytedance.com, mintegral.com, etc.) take precedence; add Smaato's own S3 repo (`https://s3.amazonaws.com/smaato-sdk-releases/`) since it's not on Maven Central; scope AppLovin's repo with `content { includeGroupByRegex "com\\.applovin\\..*" }` so it doesn't 403 on non-AppLovin packages. Also bump Gradle HTTP timeouts on the Mac mini (in `~/.gradle/gradle.properties`: `systemProp.org.gradle.internal.http.connectionTimeout=60000` and `systemProp.org.gradle.internal.http.socketTimeout=180000`) so the few artifacts that DO need BidMachine (e.g. `com.explorestack.*`) have time to land. See docs/self-hosted-setup.md troubleshooting for the full fix.
