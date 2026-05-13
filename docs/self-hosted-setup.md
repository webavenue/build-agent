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

## 3. Disable the Gradle daemon

The Gradle daemon causes hangs on self-hosted macOS — symptoms are: build starts fine, then `mapReleaseSourceSetPaths` or similar early task gets "stuck" at 0% CPU forever. The daemon's IPC with the wrapper fails in the launchd-spawned context.

Fix it globally on the Mac mini:

```bash
mkdir -p ~/.gradle
echo "org.gradle.daemon=false" >> ~/.gradle/gradle.properties
```

Every Gradle invocation now uses a one-shot JVM. Builds are ~5–10 seconds slower per run (negligible), no more hangs.

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

### 6d. Set per-runner env vars (Android SDK path)

The runner reads a `.env` file at startup. Tell it where the Android SDK lives:

```bash
cat >> /Users/ava/actions-runners/<repo-shortname>/.env <<'EOF'
ANDROID_HOME=/opt/homebrew/share/android-commandlinetools
ANDROID_SDK_ROOT=/opt/homebrew/share/android-commandlinetools
EOF
```

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

### 7a. Switch the workflow `uses:` line

In the caller repo's `.github/workflows/build.yml`:

```yaml
# Was:
uses: webavenue/build-agent/.github/workflows/capacitor.yml@main
# To:
uses: webavenue/build-agent/.github/workflows/capacitor-selfhosted.yml@main
```

Other inputs and `secrets: inherit` stay the same.

### 7b. Add `MAC_LOGIN_PASSWORD` secret

In the caller repo settings → Secrets and variables → Actions → New repository secret:

- Name: `MAC_LOGIN_PASSWORD`
- Value: the `ava` user's macOS login password on the Mac mini

The iOS job uses this to run `security unlock-keychain` before xcodebuild, which prevents `errSecInternalComponent` on the first codesign calls. Without it, the workflow will print a warning and continue — and likely fail at codesign on the first 1–2 frameworks.

### 7c. Unused secrets (safe to leave or remove)

These cloud-only secrets aren't used by the self-hosted workflow:

- `APPLE_DIST_CERT_BASE64` — cert lives in the host login keychain
- `APPLE_DIST_CERT_PASSWORD` — same
- `APPLE_PROVISIONING_PROFILE_BASE64` — profile is fetched by sigh via the API key

Leave them in place if you might switch back to the cloud workflow, or delete them to declutter.

### 7d. Add `ship_auto` lane to the Fastfile

The self-hosted iOS job calls `bundle exec fastlane ios ship_auto`. Add this next to the existing `ship` lane in `fastlane/Fastfile`:

```ruby
desc "Fetch/refresh Distribution profile via sigh, then patch App target to manual signing with it"
private_lane :set_ios_signing_sigh do
  # sigh uses the API key (set up by setup_api_key) to create or refresh
  # an App Store distribution profile, downloaded + installed locally.
  # No .mobileprovision secret needed — Apple manages it, sigh fetches it.
  get_provisioning_profile(
    app_identifier: ENV["IOS_BUNDLE_ID"],
    development:    false,    # We want a distribution profile
    force:          false,    # Reuse existing if still valid
  )

  xcodeproj = File.expand_path(File.join(ENV["CAPACITOR_PROJECT_PATH"], "ios/App/App.xcodeproj"))
  update_code_signing_settings(
    use_automatic_signing: false,   # Must be manual — automatic always picks dev profile for archive
    path:                  xcodeproj,
    targets:               ["App"], # Only the App target — leaves Pods/SPM untouched
    team_id:               ENV["APPLE_TEAM_ID"],
    code_sign_identity:    "Apple Distribution",
    profile_name:          lane_context[SharedValues::SIGH_NAME],
    bundle_identifier:     ENV["IOS_BUNDLE_ID"],
  )
end

desc "Build a signed iOS IPA on the self-hosted Mac runner — profile fetched via sigh, cert from host keychain"
private_lane :build_auto do
  set_ios_version
  setup_api_key
  set_ios_signing_sigh
  ensure_applovin_quality_service     # Or your project's equivalent if you have one
  workspace = File.expand_path(File.join(ENV['CAPACITOR_PROJECT_PATH'], 'ios/App/App.xcworkspace'))
  project   = File.expand_path(File.join(ENV['CAPACITOR_PROJECT_PATH'], 'ios/App/App.xcodeproj'))
  gym(
    scheme:           ENV["IOS_SCHEME"] || "App",
    workspace:        File.exist?(workspace) ? workspace : nil,
    project:          File.exist?(workspace) ? nil : project,
    configuration:    "Release",
    output_directory: "./build_output",
    clean:            true,
    export_options: {
      method:               "app-store",
      signingStyle:         "manual",
      signingCertificate:   "Apple Distribution",
      teamID:               ENV["APPLE_TEAM_ID"],
      provisioningProfiles: {
        ENV["IOS_BUNDLE_ID"] => lane_context[SharedValues::SIGH_NAME],
      },
    },
  )
end

desc "Build + Upload to TestFlight (self-hosted Mac runner)"
lane :ship_auto do
  build_auto
  upload
  notify_success("iOS")
end
```

The Xcode project's `CODE_SIGN_STYLE` can stay `Automatic` (the default after Capacitor scaffolding) — `set_ios_signing_sigh` patches the App target to manual at build time and leaves Pods/SPM packages on their defaults.

## 8. First-build smoke test

1. Trigger the caller repo's workflow from the Actions tab.
2. Watch the run. Android (if enabled) starts first on `mac-mini-<repo>`, iOS runs after. Both labels are `[self-hosted, macOS, ARM64]`.
3. Tail the runner log on the Mac mini if needed:
   ```bash
   tail -f /Users/ava/actions-runners/<repo-shortname>/_diag/Runner_*.log
   ```

Once Android + iOS both ship green, the runner is ready.

## 9. Switching back to cloud builds

Change the caller repo's `uses:` line back to `capacitor.yml@main`. No other changes — the original `ship` lane is untouched, secrets are still in place.

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

### Android build fails with "SDK location not found" / "ANDROID_HOME"
The runner doesn't have `ANDROID_HOME` in its environment. Check `/Users/ava/actions-runners/<repo>/.env` has the line, and restart the runner service:
```bash
cd /Users/ava/actions-runners/<repo>
./svc.sh stop
./svc.sh start
```

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
