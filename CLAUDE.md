# build-agent

Reusable GitHub Actions workflow repo for building and releasing **Capacitor** (React + Capacitor) apps to Google Play and TestFlight. Also used for Unity game projects, plus a shared **automated PR reviewer** for both stacks. Workflows are designed to be **called** from individual project repos via `workflow_call`, not triggered directly in this repo.

## Repo structure

```
.github/workflows/capacitor.yml             # Reusable workflow for Capacitor apps (GitHub-hosted runners)
.github/workflows/capacitor-selfhosted.yml  # Same workflow but for self-hosted Mac mini (Android + iOS sequential, automatic iOS signing)
.github/workflows/unity.yml                 # Reusable workflow for Unity projects (cloud)
.github/workflows/unity-selfhosted.yml      # Unity workflow on the self-hosted Mac mini
.github/workflows/claude-review.yml         # Reusable automated PR reviewer (Claude Code Action), tuned per project_type
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

## Automated PR review (claude-review.yml)

Reusable workflow that runs `anthropics/claude-code-action@v1` on a PR with a checklist tuned to the project stack. Designed to be the first reviewer on AI-generated game code — it catches the failure modes humans skim past (invented APIs, stale-closure bugs, missing Capacitor permissions, Unity null-check traps).

Supports two trigger modes — caller picks one. The reusable workflow handles both `pull_request` and `issue_comment` events; the gate resolves PR context (head ref, draft state, file count) on either path.

> **Required caller `permissions:` block.** Reusable-workflow calls cap permissions at what the caller grants — per-job permissions in the called workflow have no effect if the caller hasn't granted them. Repos with a restricted default `GITHUB_TOKEN` policy (which is most of ours) will see GHA cancel the run with `is only allowed 'issues: none, pull-requests: none'`. Always include the block shown below in the caller.

### Caller usage — automatic on every PR push

```yaml
# In <game-repo>/.github/workflows/pr-review.yml
name: PR Review
on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]

permissions:
  contents: read
  pull-requests: write
  issues: write
  id-token: write
  actions: read

jobs:
  review:
    uses: webavenue/build-agent/.github/workflows/claude-review.yml@main
    with:
      project_type: capacitor   # or "unity"
    secrets: inherit
```

### Caller usage — manual via `/review` comment

```yaml
# In <game-repo>/.github/workflows/pr-review.yml
name: PR Review
on:
  issue_comment:
    types: [created]

permissions:
  contents: read
  pull-requests: write
  issues: write
  id-token: write
  actions: read

jobs:
  review:
    if: >
      github.event.issue.pull_request != null &&
      startsWith(github.event.comment.body, '/review')
    uses: webavenue/build-agent/.github/workflows/claude-review.yml@main
    with:
      project_type: unity
      model: claude-haiku-4-5   # default; reviewer can override per-call
    secrets: inherit
```

The `if:` filters out comments on issues (vs PRs) and comments that don't start with `/review` (case-sensitive prefix). Comment grammar — parsed by the reusable workflow:

```
/review [opus|sonnet|haiku] [gdd:<url>] [free-form focus hint...]
```

Examples:
```
/review                                      → caller's default model
/review opus                                 → claude-opus-4-7  (deepest, ~25x haiku)
/review sonnet                               → claude-sonnet-4-6
/review haiku                                → claude-haiku-4-5
/review focus on the spawner                 → default model + focus hint
/review opus focus on the spawner            → model override + focus hint
/review gdd:https://docs.google.com/document/d/<id>/edit
/review opus gdd:https://docs.google.com/document/d/<id>/edit focus on combat
```

Tokens are positional and case-insensitive for the model keyword. `gdd:<url>` must be a single whitespace-bounded token; Google Docs URLs have no spaces so this works. URLs wrapped in `<>` by GitHub's autolinker are also accepted. Anything after the optional `gdd:` token becomes a "Reviewer asked: …" line appended to the prompt so Claude knows where to focus. A reviewer using the word "opusified" in their notes won't false-trigger the Opus model — matching is whole-token only.

### Tool permissions inside Claude (not GHA permissions)

The action's default permission mode prompts for each tool use — and since CI has no human to approve, every tool call is denied. The reusable workflow already passes the necessary allowlist via `--allowed-tools` in `claude_args`. If you're customizing the action elsewhere, the minimum set for PR reviews is:

```
Read,Glob,Grep,LS,Bash,mcp__github_inline_comment__create_inline_comment,mcp__github_comment__update_claude_comment
```

The two `mcp__github_*` tools are how the action surfaces comments to the PR — without them allowlisted, Claude can analyze the code but has no way to post findings.

### Inputs
| Input | Default | Description |
|---|---|---|
| `project_type` | _required_ | `"capacitor"` or `"unity"` — selects the review checklist |
| `model` | `claude-haiku-4-5` | Default model. Reviewers can override per-comment with `/review opus\|sonnet\|haiku ...`. |
| `max_changed_files` | `80` | Skip review when the PR touches more files than this (cost guard). `0` disables. |
| `skip_drafts` | `true` | Don't review draft PRs. |
| `extra_instructions` | `""` | Extra prompt text appended to the checklist (e.g. "Pay extra attention to the new payments flow."). |

### What the checklists cover

- **Capacitor:** React/Vite footguns (stale useEffect deps, conditional hooks, missing keys), Capacitor lifecycle/permission issues, mobile WebView quirks (touch vs mouse, safe areas, Android back button), game-loop leaks (rAF, audio, WebGL), and AI hallucinations (invented plugins/APIs).
- **Unity:** Unity `Object` null pitfalls (`?.` lies about destroyed objects), coroutine/lifecycle leaks, hot-path allocations (LINQ in Update, boxing, GetComponent), event subscription leaks, mobile input/perf settings, and AI hallucinations (deprecated/nonexistent APIs).

Both checklists explicitly tell Claude **not** to comment on style, naming, or "nice to haves" — only `[BUG]`, `[SECURITY]`, `[PERF]`, `[CRASH-RISK]`, or `[LOGIC]` issues earn inline comments. Everything else goes into a single sticky summary comment.

### GDD validation (optional, per-comment)

Reviewers can attach a Game Design Document URL to a single review by adding `gdd:<url>` to the comment:

```
/review gdd:https://docs.google.com/document/d/<DOC-ID>/edit
/review opus gdd:https://docs.google.com/document/d/<DOC-ID>/edit focus on the spawner
```

When present, the workflow:
1. Extracts the doc ID (accepts standard share URLs, URLs wrapped in `<>` by GitHub's autolinker, and bare doc IDs).
2. Fetches the GDD as markdown via `https://docs.google.com/document/d/<ID>/export?format=markdown` — no auth, relies on **"Anyone with the link can view"** sharing.
3. Prepends the GDD to the review prompt under a clearly labeled section and adds a `[GDD-DRIFT]` severity tag to the inline-comment allowlist.
4. Soft-fails on any fetch error (404, 403, malformed URL) — the review continues without GDD context and the failure reason is reported in the sticky summary.

**`[GDD-DRIFT]` is advisory, not blocking.** The prompt explicitly tells Claude that GDDs often lag implementation: if a deviation is found, Claude asks whether the GDD or the code should change — it doesn't claim the PR is wrong.

**Doc size.** GDDs are truncated at 200KB (≈50 pages, ≈25K tokens). For a giant master GDD, point at a feature-specific sub-doc instead — the per-PR URL design assumes the reviewer picks the relevant doc.

**Cost added per review** (rough, for a 5-page feature GDD ≈ 2K tokens):
- Haiku: +$0.002
- Sonnet: +$0.006
- Opus: +$0.03

### Requirements on the caller repo

- `ANTHROPIC_API_KEY` secret (same one used by build-failure diagnosis — push via `push-secrets.sh`).
- No additional permissions config — the reusable workflow declares `pull-requests: write` and `issues: write` itself.

### Cost notes

- Cost per typical 5–20 file PR — Haiku 4.5 ~$0.02–$0.10, Sonnet 4.6 ~$0.05–$0.30, Opus 4.7 ~$0.50–$1.50. Default is Haiku because it's good enough to catch the common AI-generated game-code mistakes; escalate to Opus per-PR with `/review opus` when the change is gnarly. The `max_changed_files` guard short-circuits unusually large PRs (vendored asset commits, generated code) before any tokens are spent.
- Bot authors (dependabot, renovate) are skipped automatically.

## Self-hosted Mac mini — non-obvious gotchas

These cost real time the first time we set up — capture them so future setups skip the trial-and-error. Full step-by-step is in [docs/self-hosted-setup.md](docs/self-hosted-setup.md).

- **Android SDK is not bundled.** Cloud runners get it via `actions/setup-java`. On self-hosted you install `android-commandlinetools` via Homebrew, run `sdkmanager --licenses`, install `platforms;android-XX` + `build-tools;XX.0.0` + `platform-tools` matching the project's `variables.gradle`, and put `ANDROID_HOME` in the runner's `.env` file. Without it, Gradle fails immediately with "SDK location not found".
- **`npm config get ignore-scripts` must be `false` on the runner.** If it's `true` (a common "npm hardening" default), npm *silently* skips all lifecycle scripts — `prebuild`/`postbuild` and `postinstall` during `npm ci`. Capacitor projects often fetch uncommitted assets via these hooks (e.g. an `ensure:tiles` prebuild), so a `true` here ships builds with missing assets and no error in the log — it cost us a Critical "world map missing" prod-blocker. The Capacitor workflows now pass `--ignore-scripts=false` on `npm ci`/`npm run build` as a guard, but also run `npm config set ignore-scripts false` on the host. See docs/self-hosted-setup.md §1.
- **Disable the Gradle daemon globally.** `echo "org.gradle.daemon=false" >> ~/.gradle/gradle.properties`. On self-hosted macOS the daemon's IPC with the gradlew wrapper hangs (visible as build "stuck" at `mapReleaseSourceSetPaths` with 0% CPU). One-shot JVM mode is ~5–10s slower per run but reliable.
- **iOS uses manual signing with a sigh-fetched profile, not pure automatic.** Pure `CODE_SIGN_STYLE=Automatic` + `-allowProvisioningUpdates` doesn't work for App Store archives — Xcode always tries to fetch a Development profile and conflicts with the Distribution cert. The `ship_auto` lane in the caller repo's Fastfile uses `sigh` (via App Store Connect API key) to create/refresh a distribution profile at build time, then patches just the App target to manual signing with that profile name. Pods/SPM packages keep their automatic-signing defaults.
- **API key needs App Manager or Admin role.** Sigh creates provisioning profiles, which requires elevated permissions. A Developer-role key worked on cloud (profile was hand-supplied) but fails on self-hosted. Mint a new key in App Store Connect → Users and Access → Integrations → Keys if your existing key is too narrow.
- **`set-key-partition-list` + per-build `unlock-keychain` are both needed.** Codesign under launchd hits `errSecInternalComponent` on the first 1–2 framework signings without these. Setup-time: run `security set-key-partition-list -S apple-tool:,apple:,codesign: ...` once. Per-build: the workflow runs `security unlock-keychain` using the `MAC_LOGIN_PASSWORD` secret. If the secret is missing, the unlock step is a soft no-op and you'll likely hit the codesign error.
- **Personal/free GitHub accounts use repo-scoped runners.** No org-level runner pool. Install one runner instance per caller repo under `~/actions-runners/<repo-shortname>/`. They coexist on the same Mac mini and don't pick up each other's jobs.
- **Some maven mirrors are slow from non-US locations and break dep resolution.** Capacitor projects with AppLovin/MAX mediation include `https://artifactory.bidmachine.io/bidmachine` in `android/build.gradle`. BidMachine mirrors Pangle, Chartboost, Mintegral, etc., but responds in 15–60s from outside the US and times out Gradle. On cloud (ubuntu-latest in US) you don't notice; on a self-hosted Mac mini elsewhere it hangs `mapReleaseSourceSetPaths`. **Fix in the caller project's `android/build.gradle`:** put BidMachine LAST in the `allprojects.repositories` block so vendor-specific repos (chartboost.jfrog.io, bytedance.com, mintegral.com, etc.) take precedence; add Smaato's own S3 repo (`https://s3.amazonaws.com/smaato-sdk-releases/`) since it's not on Maven Central; scope AppLovin's repo with `content { includeGroupByRegex "com\\.applovin\\..*" }` so it doesn't 403 on non-AppLovin packages. Also bump Gradle HTTP timeouts on the Mac mini (in `~/.gradle/gradle.properties`: `systemProp.org.gradle.internal.http.connectionTimeout=60000` and `systemProp.org.gradle.internal.http.socketTimeout=180000`) so the few artifacts that DO need BidMachine (e.g. `com.explorestack.*`) have time to land. See docs/self-hosted-setup.md troubleshooting for the full fix.
