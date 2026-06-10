# Self-Hosted Mac Runner Setup

Complete runbook to provision a Mac mini as a self-hosted GitHub Actions runner for `capacitor-selfhosted.yml`.

**Setup model:** repo-scoped runners. One runner instance per caller repo, all running side-by-side on the same Mac mini. Each runner picks up jobs only from its registered repo. This is the right pattern for personal/free GitHub accounts where org-level runners aren't available (or where org features aren't worth the GitHub Team upgrade).

A second Capacitor caller repo? Repeat the runner-install steps in §6, change the directory name + repo URL. Both runners coexist on the same Mac mini.

---

## 1. Host tools

Install once per Mac mini. All subsequent runners share these.

| Tool | Required | Verify with | Install |
|---|---|---|---|
| macOS | 15+ | `sw_vers` | — |
| Xcode | 26+ | `xcodebuild -version` | App Store |
| Xcode Command Line Tools | latest | `xcode-select -p` | `xcode-select --install` |
| JDK | 21 (Temurin) | `java -version` | `brew install --cask temurin@21` |
| Node | 22+ (24 OK) | `node --version` | `brew install node` |
| Ruby | 3.1.6+ | `ruby --version` | `brew install ruby` (and add to PATH) |
| Bundler | any | `bundle --version` | `gem install bundler` |
| CocoaPods | 1.16.2 | `pod --version` | `gem install cocoapods -v 1.16.2` |
| Homebrew | 4+ | `brew --version` | https://brew.sh |
| Android command-line tools | latest | `which sdkmanager` | `brew install --cask android-commandlinetools` |

After installing Ruby via Homebrew, ensure the Homebrew Ruby is on PATH ahead of system Ruby:

```bash
echo 'export PATH="/opt/homebrew/opt/ruby/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

If `java -version` shows 21 but `$JAVA_HOME` is empty, set it permanently:

```bash
echo 'export JAVA_HOME=$(/usr/libexec/java_home -v 21)' >> ~/.zshrc
source ~/.zshrc
```

**Ensure npm lifecycle scripts are enabled.** If `npm config get ignore-scripts` returns `true`, npm silently skips *all* lifecycle scripts — `prebuild`/`postbuild`, `postinstall` during `npm ci`, everything. Capacitor projects often rely on these hooks to fetch assets not committed to git (e.g. an `ensure:tiles` prebuild step), so a `true` here produces builds that are missing assets with no error in the log. Force it off:

```bash
npm config set ignore-scripts false   # must be false; verify with: npm config get ignore-scripts
```

The Capacitor workflows also pass `--ignore-scripts=false` on `npm ci` and `npm run build` as a belt-and-suspenders guard, but set the host config too so any manual `npm` runs on the box behave the same.

## 2. Android SDK

The cloud workflow uses `actions/setup-java`, which auto-installs the Android SDK. On self-hosted, you install it manually once.

The Capacitor project's `android/variables.gradle` declares `compileSdkVersion`, `targetSdkVersion`, `minSdkVersion` — install matching SDK + build-tools.

```bash
# Point env vars at the homebrew install location
export ANDROID_HOME=/opt/homebrew/share/android-commandlinetools
export ANDROID_SDK_ROOT=$ANDROID_HOME
export PATH=$ANDROID_HOME/cmdline-tools/latest/bin:$ANDROID_HOME/platform-tools:$PATH

# Accept all SDK licenses (auto-yes)
yes | sdkmanager --licenses

# Install platform + build-tools matching your project (35 here — change to match)
sdkmanager "platforms;android-35" "build-tools;35.0.0" "platform-tools"

# Verify
sdkmanager --list_installed
```

The `ANDROID_HOME` env var needs to be set inside the runner process, not just your shell. We'll do that in §6.

## 3. Gradle global config

A few one-time tweaks in `~/.gradle/gradle.properties` make self-hosted builds reliable:

```bash
mkdir -p ~/.gradle
cat >> ~/.gradle/gradle.properties <<'EOF'
# Disable the Gradle daemon — daemon IPC hangs under launchd on macOS, manifesting
# as a "stuck" early task (mapReleaseSourceSetPaths etc.) with 0% CPU forever.
# One-shot JVM mode adds ~5-10s per build but never hangs.
org.gradle.daemon=false

# Bump HTTP timeouts. Some maven mirrors used by mediation SDKs (most notoriously
# https://artifactory.bidmachine.io) are slow from non-US locations — 15-60s per
# HEAD/GET request, occasional total stalls. Default 10s connect / 30s read trips
# Gradle into thinking the artifact is unreachable. 60s/180s gives slow-but-eventually-
# successful requests room to land.
systemProp.org.gradle.internal.http.connectionTimeout=60000
systemProp.org.gradle.internal.http.socketTimeout=180000
EOF
```

The HTTP timeout bump alone isn't enough if the caller project lists BidMachine BEFORE its vendor-specific repos — Gradle still queries the slow mirror first for every artifact. See §10 troubleshooting for the project-side `android/build.gradle` reorder.

## 4. iOS signing prerequisites

The iOS job in `capacitor-selfhosted.yml` uses **manual signing with a sigh-fetched Distribution profile**. Pure automatic signing doesn't work for App Store archives in headless builds — Xcode insists on fetching a Development profile and refuses to combine it with the Distribution cert. The Fastfile's `ship_auto` lane works around this by calling fastlane's `sigh` to create/refresh an App Store distribution profile via the App Store Connect API key, then signing manually with that.

What you need on the host:

### 4a. Apple Distribution cert in the login keychain

```bash
security find-identity -v -p codesigning
```

Should list at least one `Apple Distribution: <Company Name> (<TEAM_ID>)` entry. If you develop locally on this Mac, it's already there. Otherwise export the `.p12` from another Mac (Keychain Access → My Certificates → right-click → Export), copy it over, and double-click to import.

### 4b. Grant codesign access to private keys (partition list)

Under launchd, codesign can't access private keys by default unless the partition list explicitly grants it. Without this, codesign fails with `errSecInternalComponent` on the first 1–2 framework signings.

Run this once on the Mac mini (replace `YOUR_LOGIN_PASSWORD` with the `ava` user's actual login password):

```bash
security set-key-partition-list \
  -S apple-tool:,apple:,codesign: \
  -s \
  -k "YOUR_LOGIN_PASSWORD" \
  ~/Library/Keychains/login.keychain-db
```

This tags every private key in the login keychain so `codesign` can use them without an interactive prompt. The workflow ALSO does `security unlock-keychain` per-build (see §7), which belt-and-braces ensures the first codesign call doesn't fail.

### 4c. Apple ID signed into Xcode (recommended)

Xcode → Settings → Accounts → add your Apple ID. Not strictly required for CI (the App Store Connect API key handles everything), but helpful for troubleshooting from the GUI.

## 5. App Store Connect API key requirements

Your `APPLE_API_KEY_BASE64` / `APPLE_API_KEY_ID` / `APPLE_API_ISSUER_ID` secrets must come from a key with **App Manager** or **Admin** role in App Store Connect → Users and Access → Integrations → Keys. The `ship_auto` lane uses sigh to create/refresh distribution profiles via this API key, which requires those permissions.

If your existing key only has Developer role, mint a new one — the cloud workflow worked with Developer because the profile was hand-supplied via secret; the self-hosted workflow has sigh create it on the fly.

## 6. Install a runner for one caller repo

Repeat this section for each caller repo.

Convention: one directory per repo under `~/actions-runners/`:

```
/Users/ava/actions-runners/
├── Golf/              # webavenue/GolfTycoon (Unity)
├── DotCollector2/     # webavenue/Dot-Collector-Idle-2 (Capacitor)
└── <next-repo>/       # ...
```

### 6a. Get the registration token

Browser: `https://github.com/<owner>/<repo>/settings/actions/runners/new`

- Runner image: **macOS**
- Architecture: **ARM64**

Copy the token from the `./config.sh --token <TOKEN>` line GitHub shows. Tokens are single-use and expire in 1 hour. Also note the runner version in the download URL (e.g. `v2.328.0`).

### 6b. Download + extract on the Mac mini

```bash
mkdir -p /Users/ava/actions-runners/<repo-shortname>
cd /Users/ava/actions-runners/<repo-shortname>

RUNNER_VERSION="2.328.0"   # ← use the version GitHub showed
curl -o actions-runner.tar.gz -L \
  https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-osx-arm64-${RUNNER_VERSION}.tar.gz
tar xzf actions-runner.tar.gz
rm actions-runner.tar.gz
```

### 6c. Configure — pin to this caller repo

Replace `<TOKEN>` and the repo URL.

```bash
./config.sh \
  --url https://github.com/<owner>/<repo> \
  --token <TOKEN> \
  --name "mac-mini-<repo-shortname>" \
  --labels "self-hosted,macOS,ARM64" \
  --work "_work" \
  --unattended
```

A **404 here** means the URL doesn't match the repo the token was generated for. Verify with `git remote -v` on a local checkout.

### 6d. Set per-runner env vars (Android SDK path + UTF-8 locale)

The runner reads a `.env` file at startup. Tell it where the Android SDK lives,
and force a UTF-8 locale:

```bash
cat >> /Users/ava/actions-runners/<repo-shortname>/.env <<'EOF'
ANDROID_HOME=/opt/homebrew/share/android-commandlinetools
ANDROID_SDK_ROOT=/opt/homebrew/share/android-commandlinetools
LANG=en_US.UTF-8
LC_ALL=en_US.UTF-8
EOF
```

**Why the locale matters.** Under launchd the runner inherits no `LANG`, so
Ruby's `Encoding.default_external` falls back to US-ASCII. Ruby 4.x made this
fatal where older Rubies tolerated it: any tool that reads a UTF-8 file then
parses or normalizes it breaks. Two we hit:
- **fastlane** — `import_from_git` of the shared Fastfile dies with
  `invalid multibyte character 0xE2` (the Fastfile has `→ ✓ ✅ … —`).
- **CocoaPods** — `pod install` crashes in `String#unicode_normalize`
  (`WARNING: CocoaPods requires your terminal to be using UTF-8 encoding`).

The Capacitor workflow also sets `LANG`/`LC_ALL` at the build-job level as a
belt-and-suspenders guard, but setting it here protects *every* workflow and
any manual command run on the box. Restart the service after editing `.env`
(`./svc.sh stop && ./svc.sh start`) — `.env` is read once at startup.

### 6e. Install as a launchd service

```bash
./svc.sh install
./svc.sh start
./svc.sh status
```

`status` should show "started" and a PID.

### 6f. Verify online

On the Mac mini:
```bash
tail -n 10 /Users/ava/actions-runners/<repo-shortname>/_diag/Runner_*.log
```
Should end with "Listening for Jobs".

On GitHub: runner page shows a green dot, status "Idle".

## 7. Caller repo changes

### 7a. Workflow `uses:` line

In the caller repo's `.github/workflows/build.yml`:

```yaml
uses: webavenue/build-agent/.github/workflows/capacitor-selfhosted.yml@main
```

### 7b. Add `MAC_LOGIN_PASSWORD` secret

In the caller repo settings → Secrets and variables → Actions → New repository secret:

- Name: `MAC_LOGIN_PASSWORD`
- Value: the `ava` user's macOS login password on the Mac mini

The iOS job uses this to run `security unlock-keychain` before xcodebuild, which prevents `errSecInternalComponent` on the first codesign calls. Without it, the workflow will print a warning and continue — and likely fail at codesign on the first 1–2 frameworks.

### 7c. Obsolete secrets (safe to remove)

These secrets are no longer used by the workflow — the dist cert lives in the host login keychain and sigh fetches the provisioning profile via the API key:

- `APPLE_DIST_CERT_BASE64`
- `APPLE_DIST_CERT_PASSWORD`
- `APPLE_PROVISIONING_PROFILE_BASE64`
- `IOS_PROVISIONING_PROFILE_NAME`

Delete them to declutter or leave in place — they're ignored either way.

### 7d. Use the shared Fastfile via `import_from_git`

All Fastlane lanes (`ship`, `ship_auto`, `build_apk`, `fetch_next_version_code`, etc.) live in this repo at `fastlane/capacitor/Fastfile`. Caller repos pull them in:

```ruby
# In <caller-repo>/fastlane/Fastfile
require 'dotenv'
Dotenv.load('../.env')

default_platform(:ios)

import_from_git(
  url:    'https://github.com/webavenue/build-agent',
  branch: 'main',                                # or `tag: 'capacitor-v1'`
  path:   'fastlane/capacitor/Fastfile',
)

# Project-specific overrides (if any) go below the import.
```

Per-repo files that stay alongside: `Gemfile`, `Gemfile.lock`, `fastlane/Appfile`, `fastlane/Pluginfile`. Edit the shared lanes in this repo, not in callers.

## 8. First-build smoke test

1. Trigger the caller repo's workflow from the Actions tab.
2. Watch the run. Android (if enabled) starts first on `mac-mini-<repo>`, iOS runs after. Both labels are `[self-hosted, macOS, ARM64]`.
3. Tail the runner log on the Mac mini if needed:
   ```bash
   tail -f /Users/ava/actions-runners/<repo-shortname>/_diag/Runner_*.log
   ```

Once Android + iOS both ship green, the runner is ready.

---

## Troubleshooting

### "No runner matching all labels: self-hosted, macOS, ARM64"
The runner is offline or has different labels. On the Mac mini:
```bash
cd /Users/ava/actions-runners/<repo-shortname> && ./svc.sh status
```

### `config.sh` returns 404 Not Found
The URL and registration token don't match the same repo. Verify with `git remote -v`, regenerate the token from the right repo's runner-new page, re-run.

### Gradle build hangs at `mapReleaseSourceSetPaths` (or any early task) with 0% CPU
Daemon disconnect. Confirm `org.gradle.daemon=false` is in `~/.gradle/gradle.properties`. After setting it, kill any stuck Gradle processes:
```bash
ps -ef | grep -i gradle | grep -v grep
kill -9 <pids>
```
Then re-trigger the workflow.

### Gradle dependency resolution times out / fails on Pangle, Chartboost, etc.

Symptom: `Could not resolve com.pangle.global:pag-sdk:7.9.1.3` (or chartboost-sdk, mintegral-sdk, smaato-sdk…) with `Read timed out` or `Could not GET 'https://artifactory.bidmachine.io/...'`.

Capacitor projects with AppLovin/MAX mediation include BidMachine's artifactory in `android/build.gradle`:
```gradle
maven { url 'https://artifactory.bidmachine.io/bidmachine' }
```
BidMachine mirrors most mediation SDKs but is slow from non-US locations (15–60s per request). If it's listed BEFORE the vendor-specific repos (chartboost.jfrog.io, bytedance.com, mintegral.com, etc.), Gradle tries BidMachine first for every artifact and stalls.

**Fix in the caller project's `android/build.gradle`** (one-time edit, commit to the project's repo):

1. **Move BidMachine LAST** in the `allprojects.repositories` block, so vendor repos take precedence. BidMachine then only gets queried for things no vendor repo has.
2. **Add Smaato's own S3 repo** — `com.smaato.*` lives at `https://s3.amazonaws.com/smaato-sdk-releases/`, not on Maven Central. Without it, Gradle falls through to AppLovin's repo and gets 403.
3. **Scope AppLovin's repo** to its own group — `https://artifacts.applovin.com/android` returns 403 for non-AppLovin packages, halting resolution. Wrap with `content { includeGroupByRegex "com\\.applovin\\..*" }` so Gradle only consults it for `com.applovin.*`.

Final `allprojects.repositories` block ordering looks like:
```gradle
allprojects {
    repositories {
        google()
        mavenCentral()
        maven { url 'https://maven.ogury.co' }
        maven { url 'https://repo.pubmatic.com/artifactory/public-repos' }
        maven { url 'https://artifact.bytedance.com/repository/pangle' }
        maven { url 'https://cboost.jfrog.io/artifactory/chartboost-ads/' }
        maven { url 'https://dl-maven-android.mintegral.com/repository/mbridge_android_sdk_oversea' }
        maven { url 'https://verve.jfrog.io/artifactory/verve-gradle-release' }
        maven { url 'https://s3.amazonaws.com/smaato-sdk-releases/' }       // NEW
        maven {
            url 'https://artifacts.applovin.com/android'
            content { includeGroupByRegex "com\\.applovin\\..*" }            // SCOPED
        }
        maven { url 'https://jitpack.io' }
        maven { url 'https://artifactory.bidmachine.io/bidmachine' }         // MOVED LAST
    }
}
```

This fix shipped to `webavenue/flight-manager` on commit `4841c9c` — clone that as a reference. Combined with the HTTP timeout bump in §3, dependency resolution completes reliably on the Mac mini for projects with heavy mediation.

The few packages that legitimately need BidMachine (e.g. `com.explorestack.*`, `io.bidmachine.*`) will still be slow on first download — but those resolve once, get cached in `~/.gradle/caches/`, and stay fast on subsequent builds.

### Android build fails with "SDK location not found" / "ANDROID_HOME"
The runner doesn't have `ANDROID_HOME` in its environment. Check `/Users/ava/actions-runners/<repo>/.env` has the line, and restart the runner service:
```bash
cd /Users/ava/actions-runners/<repo>
./svc.sh stop
./svc.sh start
```

### Build succeeds but the app is missing assets (e.g. world map / tiles) at runtime
npm lifecycle scripts are being skipped. Run `npm config get ignore-scripts` on the Mac mini — if it returns `true`, npm silently skips `prebuild`/`postbuild` (and `postinstall` during `npm ci`), so any hook that fetches uncommitted assets never runs and the build ships incomplete with no error. Fix:
```bash
npm config set ignore-scripts false   # verify: npm config get ignore-scripts → false
```
The Capacitor workflows already pass `--ignore-scripts=false` on `npm ci`/`npm run build`, so this only bites manual `npm` runs or older workflow revisions — but keep the host config off regardless. See §1.

### `invalid multibyte character 0xE2` (fastlane) or CocoaPods "requires UTF-8 encoding"
The runner has no UTF-8 locale. Under launchd `LANG` is unset, so Ruby's `Encoding.default_external` is US-ASCII — fatal on Ruby 4.x. fastlane's `import_from_git` then dies parsing the shared Fastfile's multibyte chars (`0xE2`), and `pod install` crashes in `String#unicode_normalize`. Fix on the host (`.env`, §6d):
```bash
printf 'LANG=en_US.UTF-8\nLC_ALL=en_US.UTF-8\n' >> /Users/ava/actions-runners/<repo>/.env
./svc.sh stop && ./svc.sh start   # .env is read once at startup
```
Confirm it took with `locale` (should report `en_US.UTF-8`, not `C`/`POSIX`). The build jobs in `capacitor-selfhosted.yml` also set these at job level, so an up-to-date workflow survives a locale-less host — but fixing the host protects every workflow and manual command on the box.

### iOS codesign fails with `errSecInternalComponent` on the first 1–2 frameworks
Either the partition list wasn't set or the keychain unlock step is skipping. Verify:
1. `set-key-partition-list` was run on `login.keychain-db` (§4b).
2. `MAC_LOGIN_PASSWORD` secret is set on the caller repo (§7b). The workflow's "Unlock login keychain" step should print `✓ Login keychain unlocked` — if it says "skipping", the secret is missing.

### iOS signing fails with "No signing certificate found"
The dist cert isn't in the login keychain, or the keychain is locked. Re-verify:
```bash
security find-identity -v -p codesigning
```

### iOS sigh fails with permission error
The App Store Connect API key doesn't have `App Manager` or `Admin` role. See §5.

### Stale workspace
Self-hosted runners preserve build artifacts between runs (warm caches — a feature). If something gets into a bad state:
```bash
cd /Users/ava/actions-runners/<repo>/_work && rm -rf *
```
Next build does a clean checkout.

### Two builds run simultaneously and OOM
With multiple repo-scoped runners on one 16GB Mac mini, simultaneous builds across repos run in parallel. Xcode + Gradle in parallel can swap. Options:
- Stagger build triggers manually
- Set `concurrency` in the caller workflow to a shared group name across repos
- Add RAM (or use a Mac mini with more)

### Need to update the runner version
GitHub auto-updates self-hosted runners by default. If auto-update is disabled or fails, manually:
```bash
cd /Users/ava/actions-runners/<repo>
sudo ./svc.sh stop
# Download new tarball, extract over existing files
sudo ./svc.sh start
```

## Agent runner (@Neo rollout/health agent)

A dedicated runner instance registered to **webavenue/build-agent** with the extra label `agent`. It exists so `agent.yml` (dispatched by the Slack worker on @Neo mentions) never queues behind 30–60 min build jobs — agent jobs are lightweight (no Xcode/Gradle).

**Current install:** `neo-agent` in `~/actions-runners/neo-agent` on Neo's MacBook (which already has codex/gcloud/bq/ruby logged in and configured). The recipe below works on any Mac host.

One-time host prerequisites (as the runner user):

```bash
brew install google-cloud-sdk          # gcloud + bq (Play token mint, Crashlytics BigQuery)
npm i -g @openai/codex                 # Codex CLI
/opt/homebrew/opt/ruby/bin/gem install jwt   # ES256 for App Store Connect API

codex login          # browser — ChatGPT account (auth persists in ~/.codex/auth.json)
gcloud auth login    # browser — k@infinitygames.io (used by bq for Crashlytics export)
```

Runner install (registration token from GitHub → build-agent repo → Settings → Actions → Runners → "New self-hosted runner"):

```bash
mkdir -p ~/actions-runners/neo-agent && cd ~/actions-runners/neo-agent
curl -o runner.tar.gz -L https://github.com/actions/runner/releases/download/v2.334.0/actions-runner-osx-arm64-2.334.0.tar.gz
tar xzf runner.tar.gz && rm runner.tar.gz
./config.sh --url https://github.com/webavenue/build-agent \
  --token <REGISTRATION_TOKEN> \
  --name neo-agent \
  --labels agent \
  --unattended
# Homebrew + Homebrew-ruby on PATH for agent jobs (gcloud, bq, codex, ruby/jwt):
echo 'PATH=/opt/homebrew/opt/ruby/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin' > .path
./svc.sh install && ./svc.sh start
```

Gotchas:
- `agent.yml` targets `runs-on: [self-hosted, macOS, ARM64, agent]` — only this instance has the `agent` label, and build workflows don't request it, so the two runner pools never steal each other's jobs.
- The two browser logins are the only interactive steps; both persist and self-refresh.
- Write credentials (Play publisher key, ASC .p8) are NOT on this host — `agent.yml` injects them per-job from repo secrets, and only when the requester passed the rollout allowlist.
