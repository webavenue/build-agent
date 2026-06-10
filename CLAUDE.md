# build-agent

Reusable GitHub Actions workflow repo for building and releasing **Capacitor** (React + Capacitor) apps to Google Play and TestFlight. Also used for Unity game projects, plus a shared **automated PR reviewer** for both stacks. Workflows are designed to be **called** from individual project repos via `workflow_call`, not triggered directly in this repo.

**All builds run on the self-hosted Mac mini.** Cloud-runner variants were removed — we have one Mac mini that handles Android → iOS sequentially.

## Repo structure

```
.github/workflows/capacitor-selfhosted.yml  # Reusable workflow for Capacitor apps (self-hosted Mac mini, Android → iOS sequential, automatic iOS signing)
.github/workflows/unity-selfhosted.yml      # Reusable workflow for Unity projects (self-hosted Mac mini)
.github/workflows/notify.yml                # Reusable changelog + Asana + Slack + build/* tag (called by unity-selfhosted & external-notify)
.github/workflows/external-notify.yml       # Reusable: fetch Play/TestFlight versions, then delegate to notify.yml (externally-built projects)
.github/workflows/claude-review.yml         # Reusable automated PR reviewer (Claude Code Action), tuned per project_type
fastlane/capacitor/Fastfile                 # SHARED Capacitor Fastlane lanes, imported by caller repos via `import_from_git`
.github/workflows/agent.yml                 # @Neo Slack agent — rollout/health Q&A + staged-rollout control (dispatched on THIS repo, Codex headless)
agent-tools/                                # @Neo agent toolkit: AGENTS.md playbook, per-service scripts (bin/), per-game configs (projects/)
slack-bot/                                  # Cloudflare Worker backing the Slack `/build` slash command (workflow_dispatch trigger)
channel-map.json                            # Source-of-truth Slack channel_id → "owner/repo" map; pushed to the bot's CHANNEL_MAP secret
docs/self-hosted-setup.md                   # Runbook for installing the Mac mini runner + Fastfile changes
scripts/push-secrets.sh                     # Pushes secrets/variables to target GitHub repos via gh CLI
scripts/generate-changelog.py               # LLM release-note generation (used by the notify workflows)
scripts/fetch-store-versions.py             # Queries Play / App Store Connect for latest shipped versions (external-notify)
Gemfile / Gemfile.lock                      # Pins the fastlane gem set used across projects
files/                                      # Shared credential files (Apple .p8/.p12/.mobileprovision, Play key) — gitignored
.secrets.env.example                        # Combined template for shared + project-specific values
.secrets.env                                # Shared secrets (Apple API key/cert, Play key, Slack, Asana, MAC_LOGIN_PASSWORD, ANTHROPIC_API_KEY, Unity) — gitignored
<project_name>/                             # One folder per project (idle-truck-fleet, flight-manager, colorwood-associations, …)
  .secrets.env                              # Project-specific secrets (keystore, app IDs, channel/Asana) — gitignored
  files/                                    # Project credential files (keystore.jks, google-services.json) — gitignored
```

**Secret layering:** root `.secrets.env` is loaded first, then `<project_name>/.secrets.env` on top. Project values override root values for the same key.

## How the workflow is used

Caller repos reference it like:
```yaml
uses: webavenue/build-agent/.github/workflows/capacitor-selfhosted.yml@main
```

> **Org name:** the canonical slug is `webavenue/build-agent` (confirmed via `gh repo view --json nameWithOwner`; the org was renamed). The old `WebAvenueIG/...` slug still redirects — fine for git remotes and `uses:` references, but **API calls (e.g. `workflow_dispatch` from the Slack bot) must use the canonical `webavenue/...` form**, since POSTs don't reliably follow renamed-repo redirects.

See [docs/self-hosted-setup.md](docs/self-hosted-setup.md) for runner install + the shared Fastfile mechanism.

## Shared Fastlane lanes (`fastlane/capacitor/Fastfile`)

All Capacitor build lanes (`ship`, `ship_auto`, `build_apk`, `fetch_next_version_code`, etc.) live in this repo — caller repos pull them in via `import_from_git`. Per-repo Fastfile shrinks to a ~15-line stub:

```ruby
# In <game-repo>/fastlane/Fastfile
require 'dotenv'
Dotenv.load('../.env')

default_platform(:ios)

import_from_git(
  url:    'https://github.com/webavenue/build-agent',
  branch: 'main',                                # or `tag: 'capacitor-v1'` for stability
  path:   'fastlane/capacitor/Fastfile',
)
```

**Per-repo files that stay** (NOT centralised):
- `Gemfile` / `Gemfile.lock` — pins fastlane gem version
- `fastlane/Appfile` *(optional)* — only if a project needs a static app_identifier. Current projects skip it: the shared lanes read `IOS_BUNDLE_ID` / `ANDROID_PACKAGE_NAME` from env, so the working callers (flight-manager, idle-truck-fleet) ship just `fastlane/Fastfile`.
- `fastlane/Pluginfile` *(optional)* — only if a project needs extra Fastlane plugins.

**Project-specific overrides** (e.g. extra `before_all` setup) can go in the caller Fastfile after the `import_from_git` call.

**Crashlytics dSYMs (iOS):** `ship_auto` ends with a self-detecting Crashlytics dSYM upload — when the caller has the FirebaseCrashlytics pod (`ios/App/Pods/FirebaseCrashlytics/upload-symbols`) and `ios/App/App/GoogleService-Info.plist`, gym's dSYM zip is pushed via `upload_symbols_to_crashlytics`. No extra secrets (auth comes from the plist). Projects without Crashlytics skip silently; upload errors are warn-only and never fail the build (it runs after the TestFlight upload). This is separate from App Store Connect symbolication, which already works via the default `uploadSymbols` export option.

**Future Unity:** Unity lanes will live in `fastlane/unity/Fastfile` (separate path, separate import) when set up. No interference with Capacitor lanes.

### Workflow inputs
| Input | Default | Description |
|---|---|---|
| `action` | `"build + upload"` | `"build + upload"` or `"build only"` |
| `build_android` | `true` | Whether to build the Android app |
| `build_ios` | `true` | Whether to build the iOS app |
| `do_asana` | `true` | Create Asana QA tasks (skipped for `build only`) |
| `do_slack` | `true` | Send the Slack build notification |
| `version_name` | `"1.0.0"` | Semantic version string |
| `release_notes` | `"Bug fixes..."` | Fallback notes when `auto_changelog` is off or auto-generation finds nothing |
| `auto_changelog` | `true` | Auto-generate release notes from commits since the last build tag via Claude; falls back to `release_notes` |
| `version_code_offset` | `100` | Added to `github.run_number` to compute build code (fallback only — auto-versioning from the stores is the default) |

> **Note:** iOS job only runs on `action: "build + upload"` — there is no build-only option for iOS.

## Secrets and variables

Secrets/variables must be set on the **caller repo**, not this one. Use `push-secrets.sh` to push them.

### Conventions in `.secrets.env`
- `[secrets]` section → pushed as **GitHub Secrets** (masked in logs)
- `[variables]` section → pushed as **GitHub Variables** (visible in logs)
- `_BASE64` or `_B64` suffix → value is a **file path**; the script reads and base64-encodes the file before pushing

### Key secrets/variables required by the workflow
**Android:** `ANDROID_KEYSTORE_BASE64`, `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_PASSWORD`, `GOOGLE_PLAY_JSON_KEY_BASE64`  
**iOS:** `APPLE_API_KEY_BASE64`, `APPLE_API_KEY_ID`, `APPLE_API_ISSUER_ID`, `MAC_LOGIN_PASSWORD` (Mac mini login password — used to unlock the keychain before codesign). The dist cert lives in the host login keychain; `sigh` fetches the provisioning profile via the API key — no cert/profile secrets needed.  
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

- **All builds run on the self-hosted Mac mini.** Both Android + iOS on `[self-hosted, macOS, ARM64]`, sequential — Android then iOS — because 16GB RAM can't reliably run Gradle + Xcode in parallel. Caller repos call `ship_auto` (iOS) and `ship` (Android) — both defined in the shared `fastlane/capacitor/Fastfile`. See [docs/self-hosted-setup.md](docs/self-hosted-setup.md) for runner install.
- **Keystore conversion:** Android job converts PKCS12 keystore → JKS before signing. This is required because older BouncyCastle versions in the Android Gradle Plugin can't parse PKCS12 keystores created with JDK 9+.
- **Version code formula:** `version_code_offset + github.run_number`. Set different offsets per project to prevent collisions when multiple projects share the same workflow.
- **Auto-versioning from stores (both platforms).** The shared Fastfile defines `android.fetch_next_version_code` and `ios.fetch_next_build_number`. When present (which they are by default once the caller imports the shared Fastfile), each platform's job runs its respective lane instead of using the offset formula:
    - **Android:** queries the Play **internal** track for the max version code, +1. Env in: `ANDROID_PACKAGE_NAME`, `GOOGLE_PLAY_JSON_KEY_PATH`, `NEXT_VERSION_CODE_FILE`.
    - **iOS:** queries TestFlight for the max build number across all versions, +1. Env in: `IOS_BUNDLE_ID`, `APPLE_API_KEY_PATH`, `APPLE_API_KEY_ID`, `APPLE_API_ISSUER_ID`, `NEXT_BUILD_NUMBER_FILE`.

    If either lane is absent or fails, that platform's job silently falls back to `version_code_offset + run_number`. Resolved codes are exposed as `needs.android.outputs.version_code` and `needs.ios.outputs.version_code`, consumed by their respective Asana QA tasks, the Slack message (shows both when they diverge — `Version: 1.2.3 (Android 158 · iOS 42)`), and the `build-<N>` release tag (prefers Android, falls back to iOS).
- **Play's "missing native debug symbols" warning is unactionable for Capacitor ad games.** Investigated for Dot Collector (June 2026): `debugSymbolLevel 'FULL'` + pinned `ndkVersion` are set, the NDK is on the runner, and `extractReleaseNativeDebugMetadata` runs — but it extracts 0 files because every `.so` in the app is a pre-stripped vendor binary (Pangle, AppLovin crash reporter, DataStore, zstd-jni). There's no first-party native code, so no symbols exist to bundle. The Android job's "Verify native debug symbols in AAB" step prints this diagnosis per build. Java/ANR stacks stay readable (minify off); vendor-native frames can only be symbolicated by the vendor.
- **Notifications:** After both Android and iOS jobs finish, a notify job creates Asana QA tasks and posts a Slack message with build status, download links, and release notes. Notify runs on ubuntu-latest (no benefit to self-hosting it, and it frees the Mac mini for build jobs).
- **Failure diagnosis:** On Android or iOS failure, the notify job pulls the failed-step logs via `gh run view --log-failed`, asks Claude Haiku 4.5 for a 2–4 sentence plain-English diagnosis, and posts it as a threaded reply to the Slack failure message. Requires `ANTHROPIC_API_KEY` on the caller repo; silently skipped if absent. Needs `actions: read` permission on the notify job (already declared).

## Notify workflows (`notify.yml` / `external-notify.yml`)

Standalone reusable workflows, separate from the inline notify job inside `capacitor-selfhosted.yml`:

- **`notify.yml`** — generates a QA changelog from commits since the last `build/*` tag, creates one Asana QA task per platform built, posts a Slack message, and pushes a new `build/<X.Y.Z>` tag. Called by `unity-selfhosted.yml` and `external-notify.yml`. Caller must grant `contents: write` so the tag push succeeds. Either platform's version input may be `""` to mark it "not built" — the matching Asana task is skipped and the Slack message adapts.
- **`external-notify.yml`** — for projects an **external studio** builds and uploads (e.g. `colorwood-associations`). It first queries Google Play (internal track) + App Store Connect (latest TestFlight build) to learn what the external CI shipped, validates the `X.Y.Z` format (fails on malformed), then delegates to `notify.yml`. We never build these — only the changelog + QA artefacts are generated afterward.

`capacitor-selfhosted.yml` does NOT use these — it has its own inline notify job. They exist for the Unity and external-studio paths.

## Slack bot (`slack-bot/`) — `/build` + `/invite-to-repo`

A single Cloudflare Worker (`build-agent-slack-bot`) backing two slash commands. Both resolve the target repo from the channel the command runs in via the shared `CHANNEL_MAP`, so no repo argument is ever typed.

- **`/build <android|ios|both> <version> [branch] [nogate]`** → GitHub `workflow_dispatch`, so anyone can ship a build from Slack without opening the Actions tab. e.g. `/build android 1.2.3` or `/build both 1.2.3 main`. Version must be `X.Y.Z`; omitting the branch uses `DEFAULT_REF`. Endpoint `POST /slack/build`.
- **`/invite-to-repo <github-username>`** → adds that GitHub user as a **write** collaborator on the channel's repo (`PUT /repos/{owner}/{repo}/collaborators/{username}`, `permission: push`). e.g. `/invite-to-repo octocat`. Takes a GitHub **username**, not an email. **No inviter allowlist** — anyone in a mapped channel can invite (deliberate; revisit if it's abused). Replies `in_channel` on success (201) for an audit trail; ephemeral for already-a-collaborator (204), unknown user (404), or permission errors (403). Endpoint `POST /slack/invite`.
- **Channel → repo mapping** lives in the **`CHANNEL_MAP` Worker secret** (JSON: `{"C0123ABC":"webavenue/flight-manager", …}`), keyed by Slack **channel ID** (the same value as the project's `SLACK_CHANNEL_ID`). `channel-map.json` in the repo root is the human-maintained source of truth — it is **NOT** read at runtime, so editing it alone does nothing; you must push it to the secret.
- **Onboarding a new project:** add the entry to `channel-map.json`, then from `slack-bot/` run `npx wrangler secret put CHANNEL_MAP < ../channel-map.json`. Takes effect immediately — no redeploy.
- **`GITHUB_TOKEN` permissions:** `/build` needs `actions: write`; `/invite-to-repo` also needs collaborator management — classic `repo` scope, or a fine-grained PAT with **Administration: read & write** — on every repo in `CHANNEL_MAP`. A token that's actions-only will surface a 403 in the Slack reply on invite.
- **Worker config (`wrangler.toml`):** `WORKFLOW_FILE=build.yml`, `DEFAULT_REF=develop` — so the dispatched workflow must exist on `develop` (or pass a branch as the 3rd arg). Secrets set via `wrangler secret put`: `SLACK_SIGNING_SECRET`, `GITHUB_TOKEN`, `CHANNEL_MAP`.
- **Deploy / debug:** `npm run deploy` (wrangler deploy), `npm run tail` for live logs. Healthcheck `GET /healthz`. All requests are HMAC-verified against `SLACK_SIGNING_SECRET` with 5-minute replay protection.

## @Neo rollout & app-health agent (`agent.yml` + `agent-tools/`)

@mention the **Neo** Slack app in a mapped game channel to ask rollout/health questions ("how is the game doing?", "any new crashes?", "why did ad revenue drop?") or — for allowlisted users — change a staged rollout ("increase rollout to 50%", "halt the rollout"). Runs **OpenAI Codex headless** (not Claude — uses the ChatGPT-subscription login cached on the runner host), driven by the playbook in `agent-tools/AGENTS.md`. Capacitor games only for now.

### Flow

```
@Neo mention → worker POST /slack/events (HMAC verify, retry dedupe, 3s ack)
            → workflow_dispatch agent.yml on webavenue/build-agent   ← canonical slug required for API calls
            → runner "neo-agent" (label `agent`; Neo's MacBook, ~/actions-runners/neo-agent)
            → codex exec in agent-tools/ (+ Firebase MCP server for Crashlytics)
            → threaded Slack reply (chat.postMessage)
```

### Security model — the LLM is never the authorization boundary

- **Who can mutate:** Slack user IDs in the worker secret `ROLLOUT_ALLOWLIST` (JSON array), re-checked inside agent.yml against the repo **variable** `ROLLOUT_ALLOWED_SLACK_IDS` (comma-separated). Both must agree.
- **Credential gating:** write-capable keys (Play publisher JSON, ASC .p8) are written per-job from secrets and **deleted before Codex starts** when the requester isn't allowlisted — the mutating scripts fail closed no matter what the model tries. Read-only runs still see rollout status + ratings via a workflow **pre-fetch snapshot** embedded in the prompt.
- **99% hard cap:** `play_rollout_update` refuses anything ≥99.5%, and there is **no complete-to-100% tool** for either platform (100% is manual in Play Console; iOS phased release auto-ramps per Apple's 7-day schedule).
- All Slack-controlled text reaches shell via `env:` only — never `${{ }}` inside `run:` blocks — so a malicious question can't shell-inject.

### Data sources (each validated live before being wired in)

| Data | Source | Auth |
|---|---|---|
| Rollout state + control | Play `edits.tracks` / ASC phased release | `GOOGLE_PLAY_JSON_KEY_BASE64` / `APPLE_API_KEY_BASE64` (gated) |
| Crash/ANR rates by versionCode | Play Developer Reporting API | `GOOGLE_PLAY_REPORTING_KEY_BASE64` |
| Crash issues + stack traces (both platforms, 90d) | **Firebase MCP** (`firebase mcp --only crashlytics`) | `firebase login` on the runner host |
| Users / engagement / revenue by appVersion | GA4 Data API | reporting SA is GA **account-level Viewer** |
| Ratings | Play `reviews.list` (per-version, commented only) · iTunes lookup (public) · ASC `customerReviews` | publisher SA / none / `.p8` |
| Ad monetization (eCPM, fill rate, per-network) | AppLovin MAX reporting API | `MAX_REPORTING_API_KEY` |

### Secrets/variables — all on THIS repo, game repos need nothing

Secrets: `GOOGLE_PLAY_JSON_KEY_BASE64`, `GOOGLE_PLAY_REPORTING_KEY_BASE64`, `APPLE_API_KEY_BASE64`, `APPLE_API_KEY_ID`, `APPLE_API_ISSUER_ID`, `SLACK_BOT_TOKEN`, `MAX_REPORTING_API_KEY` — push with `./scripts/push-secrets.sh webavenue/build-agent`. Variable: `ROLLOUT_ALLOWED_SLACK_IDS`. Worker secrets (`wrangler secret put`): `ROLLOUT_ALLOWLIST`, `SLACK_BOT_TOKEN`, `CHANNEL_MAP`.

### Onboarding a game to the agent

1. Create `agent-tools/projects/<name>.env` — `APP_NAME`, `ANDROID_PACKAGE_NAME`, `IOS_BUNDLE_ID`, `FIREBASE_PROJECT`, `FIREBASE_ANDROID_APP_ID` + `FIREBASE_IOS_APP_ID` (from `firebase apps:list`), `GA_PROPERTY_ID`, `CRASHLYTICS_TABLE_PREFIX`.
2. In `channel-map.json`, change the channel's entry from `"owner/repo"` to `{"repo":"owner/repo","project":"<name>"}`, then `cd slack-bot && npx wrangler secret put CHANNEL_MAP < ../channel-map.json`.
3. Nothing in the game repo. No new secrets anywhere.

### Gotchas (these cost real time)

- **Version namespaces differ per service** for the same game (Energy: Play `8.11.5` vs ASC `9.9.3` vs GA `9.99`; Crashlytics mixes old `1.0.x` builds in). Correlate by release date, never string-match — encoded as rule 1 in `agent-tools/AGENTS.md`.
- **Unindented lines inside a YAML `run: |` block** silently terminate the block scalar; GitHub still registers the workflow but `workflow_dispatch` fails with a misleading 422 "Workflow does not have 'workflow_dispatch' trigger". Parse with `ruby -ryaml` before pushing.
- **The firebase CLI has no Crashlytics read commands** (upload-only) — the MCP server is the only CLI-adjacent read path. The BigQuery export (`bin/crashlytics_top`) is an optional fallback for custom SQL.
- **Play `reviews.list`** returns only commented reviews (~last 7 days, skews negative — fine for build-vs-build, never comparable to the store average). **Apple ratings are cumulative** — no per-version ratings exist.
- **Runner host one-time setup:** `codex login` (ChatGPT auth), `gcloud auth login` (for bq), `gem install jwt`, google-cloud-sdk. See the "Agent runner" section in docs/self-hosted-setup.md. bq/gcloud also need codex sandbox write carve-outs — already configured in agent.yml.

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
- **API key needs App Manager or Admin role.** Sigh creates provisioning profiles, which requires elevated permissions. A Developer-role key won't work — mint a new key in App Store Connect → Users and Access → Integrations → Keys if your existing key is too narrow.
- **`set-key-partition-list` + per-build `unlock-keychain` are both needed.** Codesign under launchd hits `errSecInternalComponent` on the first 1–2 framework signings without these. Setup-time: run `security set-key-partition-list -S apple-tool:,apple:,codesign: ...` once. Per-build: the workflow runs `security unlock-keychain` using the `MAC_LOGIN_PASSWORD` secret. If the secret is missing, the unlock step is a soft no-op and you'll likely hit the codesign error.
- **Personal/free GitHub accounts use repo-scoped runners.** No org-level runner pool. Install one runner instance per caller repo under `~/actions-runners/<repo-shortname>/`. They coexist on the same Mac mini and don't pick up each other's jobs.
- **Some maven mirrors are slow from non-US locations and break dep resolution.** Capacitor projects with AppLovin/MAX mediation include `https://artifactory.bidmachine.io/bidmachine` in `android/build.gradle`. BidMachine mirrors Pangle, Chartboost, Mintegral, etc., but responds in 15–60s from outside the US and times out Gradle on the Mac mini, hanging `mapReleaseSourceSetPaths`. **Fix in the caller project's `android/build.gradle`:** put BidMachine LAST in the `allprojects.repositories` block so vendor-specific repos (chartboost.jfrog.io, bytedance.com, mintegral.com, etc.) take precedence; add Smaato's own S3 repo (`https://s3.amazonaws.com/smaato-sdk-releases/`) since it's not on Maven Central; scope AppLovin's repo with `content { includeGroupByRegex "com\\.applovin\\..*" }` so it doesn't 403 on non-AppLovin packages. Also bump Gradle HTTP timeouts on the Mac mini (in `~/.gradle/gradle.properties`: `systemProp.org.gradle.internal.http.connectionTimeout=60000` and `systemProp.org.gradle.internal.http.socketTimeout=180000`) so the few artifacts that DO need BidMachine (e.g. `com.explorestack.*`) have time to land. See docs/self-hosted-setup.md troubleshooting for the full fix.
