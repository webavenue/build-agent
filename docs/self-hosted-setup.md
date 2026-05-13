# Self-Hosted Mac Runner Setup

Step-by-step guide to set up a Mac mini as a self-hosted GitHub Actions runner for `capacitor-selfhosted.yml`.

One Mac mini hosts one runner that builds both Android and iOS sequentially (Android first, then iOS). The runner is shared across all caller repos that opt into the self-hosted workflow.

---

## 1. Required host tools

Install these on the Mac mini before configuring the runner.

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

After installing Ruby via Homebrew, ensure the Homebrew Ruby is on PATH ahead of the system Ruby:

```bash
echo 'export PATH="/opt/homebrew/opt/ruby/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

## 2. iOS signing prerequisites

The `capacitor-selfhosted.yml` iOS job uses **automatic signing** with `xcodebuild -allowProvisioningUpdates` + the App Store Connect API key. Profiles are fetched at build time; no `.mobileprovision` is decoded from secrets.

Two things must be set up on the Mac mini:

### 2a. Apple Distribution certificate in the login keychain

Verify it's there:

```bash
security find-identity -v -p codesigning
```

You should see at least one `Apple Distribution: <Company Name> (<TEAM_ID>)` entry. If you develop locally on this Mac, it's likely already installed. Otherwise, export the `.p12` from another Mac (Keychain Access → My Certificates → right-click → Export), copy it over, and double-click to import.

### 2b. Apple ID signed into Xcode (recommended fallback)

Open Xcode → Settings → Accounts → add your Apple ID. With the App Store Connect API key, this isn't strictly required, but having it logged in helps when Xcode needs to refresh profiles outside of CI.

## 3. Install the GitHub Actions runner

GitHub gives you the exact commands when you add a new runner. For each repo (or, better, at the organization level), go to:

**Settings → Actions → Runners → New self-hosted runner → macOS → ARM64**

Follow the on-screen instructions, which look roughly like:

```bash
mkdir ~/actions-runner && cd ~/actions-runner
curl -o actions-runner-osx-arm64.tar.gz -L https://github.com/actions/runner/releases/download/v2.XXX.X/actions-runner-osx-arm64-2.XXX.X.tar.gz
tar xzf ./actions-runner-osx-arm64.tar.gz
./config.sh --url https://github.com/<org-or-user> --token <token>
```

When `config.sh` asks for **labels**, enter (in addition to the defaults):

```
self-hosted,macOS,ARM64
```

These three labels are required — the workflow targets `runs-on: [self-hosted, macOS, ARM64]`. If you skip them, jobs will queue forever waiting for a matching runner.

### Run the runner as a launchd service (recommended)

So it auto-starts after reboots:

```bash
cd ~/actions-runner
./svc.sh install
./svc.sh start
```

Verify it's online: GitHub → Settings → Actions → Runners → status should be "Idle".

## 4. Caller repo changes

For each Capacitor project repo that wants to use the self-hosted runner:

### 4a. Switch the workflow `uses:` line

In the caller repo's `.github/workflows/build.yml` (or whatever it's named), change:

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

Before relying on the self-hosted runner for shipping builds, do one test run:

1. Trigger the caller repo's workflow as usual.
2. Watch the run page on GitHub. The Android and iOS jobs should pick up the `[self-hosted, macOS, ARM64]` label and run on the Mac mini.
3. Tail the runner log on the Mac mini if something gets stuck:
   ```bash
   tail -f ~/actions-runner/_diag/Runner_*.log
   ```

## 6. Switching back to cloud builds

Switch the caller repo's `uses:` line back to `capacitor.yml@main`. No other changes needed — the existing `ship` lane is untouched, secrets are still all in GitHub.

---

## Troubleshooting

### "No runner matching all labels: self-hosted, macOS, ARM64"
The runner is offline or configured with different labels. On the Mac mini:
```bash
cd ~/actions-runner && ./svc.sh status
```

### Stale workspace
Self-hosted runners preserve build artifacts between runs (a feature — warm caches). If something gets into a bad state:
```bash
cd ~/actions-runner/_work && rm -rf *
```
Next build will be a clean checkout from scratch.

### iOS signing fails with "No signing certificate found"
The dist cert isn't in the login keychain, or the keychain is locked. Re-verify:
```bash
security find-identity -v -p codesigning
```
If the keychain is locked after a reboot, unlock it manually once — or use `security unlock-keychain` with a stored password.

### iOS signing fails with "Failed to update profile"
The App Store Connect API key doesn't have the right permissions. In App Store Connect → Users and Access → Keys, the key must have **App Manager** or **Admin** role.

### Build runs but never completes
The Mac mini may be running out of memory if some other process is competing (Xcode UI open, Chrome eating RAM, etc.). Close GUI apps before triggering builds, or upgrade RAM.

### Need to run two builds at once
Out of scope for this setup. The workflow assumes sequential execution. If volume grows, add a second Mac mini with the same labels — GitHub will queue jobs across both runners.
