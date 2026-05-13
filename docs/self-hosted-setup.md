# Self-Hosted Mac Runner Setup

Step-by-step guide to use a Mac mini as a self-hosted GitHub Actions runner for `capacitor-selfhosted.yml`.

**Setup model:** repo-scoped runners. One runner instance per caller repo, all running side-by-side on the same Mac mini. Each runner picks up jobs only from its registered repo, so caller repos stay isolated. This is the right pattern for personal/free GitHub accounts where org-level runners aren't available (or where org features aren't worth the GitHub Team upgrade).

A second Capacitor caller repo? Repeat the runner-install steps in §3, change the directory name + repo URL. Both runners coexist on the same Mac mini.

---

## 1. Required host tools

Install these on the Mac mini once. Subsequent runners on the same machine reuse them.

| Tool | Required | Verify with | Install |
|---|---|---|---|
| macOS | 15+ | `sw_vers` | — |
| Xcode | 26+ | `xcodebuild -version` | App Store |
| JDK | 21 (Temurin) | `java -version` | `brew install --cask temurin@21` |
| Node | 22+ (24 OK) | `node --version` | `brew install node` |
| Ruby | 3.1.6+ | `ruby --version` | `brew install ruby` (and add to PATH) |
| Bundler | any | `bundle --version` | `gem install bundler` |
| CocoaPods | 1.16.2 | `pod --version` | `gem install cocoapods -v 1.16.2` |
| Homebrew | 4+ | `brew --version` | https://brew.sh |

If `java -version` shows 21 but `$JAVA_HOME` is empty, set it permanently:

```bash
echo 'export JAVA_HOME=$(/usr/libexec/java_home -v 21)' >> ~/.zshrc
source ~/.zshrc
```

## 2. iOS signing prerequisites

The `capacitor-selfhosted.yml` iOS job uses **automatic signing** with `xcodebuild -allowProvisioningUpdates` + the App Store Connect API key. Profiles are fetched at build time; no `.mobileprovision` is decoded from secrets.

### 2a. Apple Distribution certificate in the login keychain

Verify it's there:

```bash
security find-identity -v -p codesigning
```

You should see at least one `Apple Distribution: <Company Name> (<TEAM_ID>)` entry. If you develop locally on this Mac, it's likely already installed. Otherwise export the `.p12` from another Mac (Keychain Access → My Certificates → right-click → Export), copy it over, and double-click to import.

### 2b. Apple ID signed into Xcode (recommended)

Open Xcode → Settings → Accounts → add your Apple ID. With the App Store Connect API key, this isn't strictly required for CI, but having it logged in helps when Xcode needs to refresh profiles outside of CI.

## 3. Install a runner for one caller repo

Repeat this section for each caller repo that wants to use the self-hosted workflow.

The convention on this Mac mini is one directory per repo, all under `~/actions-runners/`:

```
/Users/ava/actions-runners/
├── Golf/              # webavenue/GolfTycoon (Unity)
├── DotCollector2/     # webavenue/Dot-Collector-Idle-2 (Capacitor)
└── <next-repo>/       # ...
```

### Step 3a. Get the registration token

In the browser, go to the caller repo's runner page:

```
https://github.com/<owner>/<repo>/settings/actions/runners/new
```

- Runner image: **macOS**
- Architecture: **ARM64**

GitHub shows a set of commands — **copy just the token** from the `./config.sh --token <TOKEN>` line. Tokens are single-use and expire in 1 hour, so generate fresh if you don't use it immediately.

Also note the **runner version** in the URL of the download command (e.g. `v2.328.0`).

### Step 3b. Download + extract the runner on the Mac mini

```bash
# Replace <repo-shortname> with a folder name for this repo (e.g. DotCollector2)
mkdir -p /Users/ava/actions-runners/<repo-shortname>
cd /Users/ava/actions-runners/<repo-shortname>

# Use the version GitHub showed you
RUNNER_VERSION="2.328.0"
curl -o actions-runner.tar.gz -L \
  https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-osx-arm64-${RUNNER_VERSION}.tar.gz
tar xzf actions-runner.tar.gz
rm actions-runner.tar.gz
```

### Step 3c. Configure — pin to this caller repo

Replace `<TOKEN>` with the registration token from 3a, and `<owner>/<repo>` with the exact repo URL.

```bash
./config.sh \
  --url https://github.com/<owner>/<repo> \
  --token <TOKEN> \
  --name "mac-mini-<repo-shortname>" \
  --labels "self-hosted,macOS,ARM64" \
  --work "_work" \
  --unattended
```

**Common 404 errors here** mean the URL doesn't match the repo the token was generated for. Both must be the same repo. Double-check with `git remote -v` on a local checkout if unsure.

### Step 3d. Install as a launchd service

So the runner auto-starts after Mac mini reboots:

```bash
./svc.sh install
./svc.sh start
./svc.sh status
```

`status` should show "started" and a process ID.

### Step 3e. Verify it's online

On the Mac mini:
```bash
tail -n 10 /Users/ava/actions-runners/<repo-shortname>/_diag/Runner_*.log
```
Should end with "Listening for Jobs".

On GitHub: the runner page should show the runner with a green dot, status "Idle".

## 4. Caller repo changes

### 4a. Switch the workflow `uses:` line

In the caller repo's `.github/workflows/build.yml`, change:

```yaml
uses: webavenue/build-agent/.github/workflows/capacitor.yml@main
```

to:

```yaml
uses: webavenue/build-agent/.github/workflows/capacitor-selfhosted.yml@main
```

Secrets and `vars.*` references stay the same — the new workflow uses the same names. `APPLE_DIST_CERT_BASE64`, `APPLE_DIST_CERT_PASSWORD`, and `APPLE_PROVISIONING_PROFILE_BASE64` become unused (safe to leave in place, or delete later).

### 4b. Add `ship_auto` lane to the Fastfile

The self-hosted iOS job calls `bundle exec fastlane ios ship_auto`. Add this lane next to the existing `ship` lane in `fastlane/Fastfile`:

```ruby
desc "Build a signed iOS IPA using automatic signing (for self-hosted Mac runner)"
private_lane :build_auto do
  set_ios_version
  setup_api_key
  ensure_applovin_quality_service
  workspace = File.expand_path(File.join(ENV['CAPACITOR_PROJECT_PATH'], 'ios/App/App.xcworkspace'))
  project   = File.expand_path(File.join(ENV['CAPACITOR_PROJECT_PATH'], 'ios/App/App.xcodeproj'))
  gym(
    scheme:           ENV["IOS_SCHEME"] || "App",
    workspace:        File.exist?(workspace) ? workspace : nil,
    project:          File.exist?(workspace) ? nil : project,
    configuration:    "Release",
    output_directory: "./build_output",
    clean:            true,
    xcargs:           "-allowProvisioningUpdates",
    export_options: {
      method:       "app-store",
      signingStyle: "automatic",
      teamID:       ENV["APPLE_TEAM_ID"],
    },
  )
end

desc "Build + Upload to TestFlight using automatic signing"
lane :ship_auto do
  build_auto
  upload
  notify_success("iOS")
end
```

The Xcode project's `CODE_SIGN_STYLE` should stay `Automatic` (its default after Capacitor scaffolding). The new lane does NOT patch the project to manual signing.

## 5. First-build smoke test

1. Trigger the caller repo's workflow as usual (Actions tab → Build Agent → Run workflow).
2. Watch the run page on GitHub. The Android and iOS jobs should pick up the `[self-hosted, macOS, ARM64]` label and run on the Mac mini.
3. If something gets stuck, tail the runner log on the Mac mini:
   ```bash
   tail -f /Users/ava/actions-runners/<repo-shortname>/_diag/Runner_*.log
   ```

## 6. Switching back to cloud builds

Switch the caller repo's `uses:` line back to `capacitor.yml@main`. No other changes needed — the existing `ship` lane is untouched, secrets are still all in GitHub.

---

## Troubleshooting

### "No runner matching all labels: self-hosted, macOS, ARM64"
The runner is offline or configured with different labels. On the Mac mini:
```bash
cd /Users/ava/actions-runners/<repo-shortname> && ./svc.sh status
```

### `config.sh` returns 404 Not Found
The URL and the registration token don't match the same repo. Verify the URL with `git remote -v` on a local checkout, regenerate the token from that exact repo's runner-new page, and re-run.

### Stale workspace
Self-hosted runners preserve build artifacts between runs (a feature — warm caches). If something gets into a bad state:
```bash
cd /Users/ava/actions-runners/<repo-shortname>/_work && rm -rf *
```
Next build will be a clean checkout from scratch.

### iOS signing fails with "No signing certificate found"
The dist cert isn't in the login keychain, or the keychain is locked. Re-verify:
```bash
security find-identity -v -p codesigning
```
If the keychain is locked after a reboot, unlock it manually once.

### iOS signing fails with "Failed to update profile"
The App Store Connect API key doesn't have the right permissions. In App Store Connect → Users and Access → Keys, the key must have **App Manager** or **Admin** role.

### Two builds run at the same time and OOM
With multiple repo-scoped runners on one Mac mini, simultaneous builds across repos run in parallel. With 16GB RAM, Xcode + Gradle in parallel can swap or OOM. Options:
- Stagger build triggers manually
- Set `concurrency` in the caller workflow to a shared group across repos (advanced)
- Add RAM

### Need to update the runner version
GitHub auto-updates self-hosted runners by default. If auto-update is disabled or fails, manually:
```bash
cd /Users/ava/actions-runners/<repo-shortname>
sudo ./svc.sh stop
# Download new version, extract over existing files
sudo ./svc.sh start
```
