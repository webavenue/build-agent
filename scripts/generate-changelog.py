#!/usr/bin/env python3
"""
generate-changelog.py — Build QA changelog from git history + Claude Haiku 4.5.

Reads commit range (PREV build tag → HEAD), enumerates merged PRs and direct
commits, fetches PR metadata via `gh`, asks Claude to synthesize a structured
changelog, and writes outputs to GITHUB_OUTPUT for the calling workflow to
consume in Asana / Slack steps.

INPUTS (env):
  COMMIT_RANGE_FROM     git ref for "previous build" (e.g. build/1.22.19791).
                        Empty string → use last 50 commits as fallback.
  COMMIT_RANGE_TO       git ref for "current build" (usually HEAD).
  GITHUB_REPOSITORY     owner/repo for gh CLI calls.
  ANTHROPIC_API_KEY     Claude API key.
  PROJECT_NAME          Used in the prompt for context.
  FALLBACK_RELEASE_NOTES  Used as final output when no commits in range OR
                          when LLM generation fails. Empty string disables
                          fallback (script will exit 1 in that case).
  HIGH_RISK_PATHS       Newline-separated repo paths that auto-flag risk=high.
                        Defaults to a sensible mobile-game set.
  IGNORE_PATHS          Newline-separated repo paths to filter out of PR file
                        lists before sending to the LLM.
  EXTRA_QA_HINT         Free-form note appended to the prompt.

OUTPUTS (GITHUB_OUTPUT):
  release_notes_md      Multi-line markdown for Asana task notes.
  release_notes_short   Compact ≤500-char version for Play Store / Slack tail.
  range_empty           "true" when no commits in range (override was used).
  used_fallback         "true" when fallback notes were used (empty range or
                        LLM failure).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
# Caller picks the model via ANTHROPIC_MODEL env. Falls back to Haiku 4.5,
# the cheapest current option — fine for most changelog runs. Use Sonnet
# when commit volume is high or PR bodies are sparse; Opus for the rare
# release where you want the most polished QA notes.
DEFAULT_MODEL = "claude-haiku-4-5"
ANTHROPIC_MAX_TOKENS = 4096

# Truncation caps to keep the prompt bounded regardless of PR size.
PR_BODY_CHAR_CAP = 2000
PR_FILES_CAP = 30
PR_COMMITS_CAP = 50

# Default high-risk paths — sensible for any mobile game project. Override
# per-project via HIGH_RISK_PATHS env.
DEFAULT_HIGH_RISK_PATHS = [
    "Ads",
    "IAP",
    "Purchase",
    "Save",
    "Analytics",
    "Energy",
    "Economy",
]


# ─────────────────────────────────────────────────────────────────────────────
# Data shapes
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class PullRequest:
    number: int
    title: str
    body: str
    author: str
    commits: list[str]  # subjects only
    files: list[str]  # paths only

    def to_prompt_block(self) -> str:
        body = self.body or "(no description)"
        if len(body) > PR_BODY_CHAR_CAP:
            body = body[:PR_BODY_CHAR_CAP] + " …[truncated]"
        commit_lines = "\n".join(f"  - {c}" for c in self.commits[:PR_COMMITS_CAP])
        if len(self.commits) > PR_COMMITS_CAP:
            commit_lines += f"\n  - …({len(self.commits) - PR_COMMITS_CAP} more commits)"
        file_lines = "\n".join(f"  - {f}" for f in self.files[:PR_FILES_CAP])
        if len(self.files) > PR_FILES_CAP:
            file_lines += f"\n  - …({len(self.files) - PR_FILES_CAP} more files)"
        return (
            f"PR #{self.number} — {self.title}\n"
            f"Author: {self.author}\n"
            f"Description:\n{textwrap.indent(body, '  ')}\n"
            f"Commits ({len(self.commits)}):\n{commit_lines or '  (none)'}\n"
            f"Files ({len(self.files)}):\n{file_lines or '  (none)'}"
        )


@dataclass
class DirectCommit:
    sha: str
    subject: str
    author: str

    def to_prompt_block(self) -> str:
        return f"- {self.sha[:7]} | {self.author} | {self.subject}"


@dataclass
class ChangelogConfig:
    project_name: str
    high_risk_paths: list[str]
    ignore_paths: list[str]
    extra_qa_hint: str


# ─────────────────────────────────────────────────────────────────────────────
# Git / gh
# ─────────────────────────────────────────────────────────────────────────────


def run(cmd: list[str], check: bool = True) -> str:
    """Run a subprocess, capture stdout, raise on non-zero exit when check=True."""
    res = subprocess.run(cmd, capture_output=True, text=True)
    if check and res.returncode != 0:
        sys.stderr.write(
            f"Command failed ({res.returncode}): {' '.join(cmd)}\n"
            f"stderr: {res.stderr}\n"
        )
        raise SystemExit(res.returncode)
    return res.stdout


def resolve_range(env_from: str, env_to: str) -> tuple[str, str, bool]:
    """Resolve PREV..CURR. Returns (from_ref, to_ref, used_fallback_range)."""
    to_ref = env_to or "HEAD"
    if env_from:
        # Make sure the ref actually exists locally.
        res = subprocess.run(
            ["git", "rev-parse", "--verify", env_from],
            capture_output=True,
            text=True,
        )
        if res.returncode == 0:
            return env_from, to_ref, False
        sys.stderr.write(
            f"WARN: COMMIT_RANGE_FROM='{env_from}' not found locally, "
            f"falling back to last 50 commits.\n"
        )
    # Fallback: 50 commits back from HEAD. Better than scanning the entire repo.
    return f"{to_ref}~50", to_ref, True


def merge_commits_in_range(from_ref: str, to_ref: str) -> list[tuple[str, str]]:
    """Return list of (sha, subject) for merge commits on the first-parent path."""
    out = run(
        [
            "git",
            "log",
            "--merges",
            "--first-parent",
            f"{from_ref}..{to_ref}",
            "--pretty=format:%H%x09%s",
        ]
    )
    return [tuple(line.split("\t", 1)) for line in out.splitlines() if line.strip()]


def first_parent_non_merge_commits(from_ref: str, to_ref: str) -> list[tuple[str, str, str]]:
    """Non-merge commits on first-parent path: (sha, subject, author_name)."""
    out = run(
        [
            "git",
            "log",
            "--first-parent",
            "--no-merges",
            f"{from_ref}..{to_ref}",
            "--pretty=format:%H%x09%s%x09%an",
        ]
    )
    rows = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 2)
        if len(parts) == 3:
            rows.append(tuple(parts))
    return rows


# Match "Merge pull request #123 from foo/bar" → 123
MERGE_PR_RE = re.compile(r"^Merge pull request #(\d+)\b")
# Match "Subject text (#123)" at end of squash-merge commit → 123
SQUASH_PR_RE = re.compile(r"\(#(\d+)\)\s*$")


def parse_pr_number_from_subject(subject: str) -> int | None:
    m = MERGE_PR_RE.match(subject)
    if m:
        return int(m.group(1))
    m = SQUASH_PR_RE.search(subject)
    if m:
        return int(m.group(1))
    return None


def fetch_pr(repo: str, number: int) -> PullRequest | None:
    """Pull PR metadata via gh CLI. Returns None if the PR can't be fetched."""
    res = subprocess.run(
        [
            "gh",
            "pr",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "number,title,body,author,commits,files",
        ],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        sys.stderr.write(
            f"WARN: gh pr view #{number} failed: {res.stderr.strip()}\n"
        )
        return None
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError:
        sys.stderr.write(f"WARN: gh pr view #{number} returned non-JSON\n")
        return None
    return PullRequest(
        number=data["number"],
        title=data.get("title", "") or "",
        body=data.get("body", "") or "",
        author=(data.get("author") or {}).get("login", "unknown"),
        commits=[
            (c.get("messageHeadline") or "").strip()
            for c in (data.get("commits") or [])
            if c.get("messageHeadline")
        ],
        files=[
            (f.get("path") or "").strip()
            for f in (data.get("files") or [])
            if f.get("path")
        ],
    )


# ─────────────────────────────────────────────────────────────────────────────
# Filtering
# ─────────────────────────────────────────────────────────────────────────────


def filter_files(files: list[str], ignore_paths: list[str]) -> list[str]:
    if not ignore_paths:
        return files
    return [
        p
        for p in files
        if not any(p == ig or p.startswith(ig.rstrip("/") + "/") for ig in ignore_paths)
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Prompt + Claude API
# ─────────────────────────────────────────────────────────────────────────────


PROMPT_HEADER = """\
You are generating QA release notes for the {project_name} mobile game build.

OUTPUT REQUIREMENT: Return ONLY a single valid JSON object. No markdown fences, no commentary before or after. The JSON must match this schema EXACTLY:

{{
  "features": [Entry, ...],
  "fixes":    [Entry, ...],
  "other":    [Entry, ...],
  "qa_focus": ["short cross-cutting note for QA", ...]
}}

Entry := {{
  "title":    "Short headline, max 60 chars",
  "summary":  "1-2 sentence description of what changed and why QA should care",
  "risk":     "low" | "medium" | "high",
  "pr":       <int PR number or null>
}}

RULES:
- For each PR, derive ONE entry from PR title + body. Do NOT enumerate every commit — the goal is "what changed", not a log dump.
- A single PR can have 100 commits but should typically appear as ONE entry in the changelog.
- If a commit message inside a PR clearly describes work UNRELATED to the PR's main topic (e.g. "fix tutorial bug" inside a Progressbar PR), split it out as its own entry under "fixes" or "other".
- If a commit subject starts with [Tag] in square brackets, treat that tag as an explicit feature/area override for that commit.
- "risk": "high" for any change touching these areas (path substrings): {high_risk_paths}. Also "high" for save format changes, network/auth, payment flows. "medium" for gameplay rule changes, economy tuning, new screens. "low" otherwise.
- "summary": include enough detail that a tester reading just this line understands WHAT changed and what surfaces it touches. Per-entry test steps are intentionally not requested — concrete test guidance goes in "qa_focus" only.
- Categorisation: "features" for new functionality, "fixes" for bug repairs, "other" for refactors/build-config/dependency bumps. Skip pure-noise entries (whitespace, comment-only).
- "qa_focus": 3-6 cross-cutting bullets for things QA should verify across the whole build (e.g. "Save/load roundtrip after each gameplay session", "Test on iOS 15 baseline device"). This is the ONLY place test steps appear — make them count.
- Title rules: imperative or noun-phrase, max 60 chars, no PR-number suffix.
"""


def build_prompt(
    config: ChangelogConfig,
    prs: list[PullRequest],
    direct: list[DirectCommit],
) -> str:
    parts = [
        PROMPT_HEADER.format(
            project_name=config.project_name,
            high_risk_paths=", ".join(config.high_risk_paths) or "(none configured)",
        )
    ]
    if config.extra_qa_hint:
        parts.append(f"\nEXTRA QA HINT FOR THIS PROJECT:\n{config.extra_qa_hint}\n")
    parts.append("\n=== INPUT: PRs merged since last build ===\n")
    if prs:
        for pr in prs:
            parts.append(pr.to_prompt_block())
            parts.append("")
    else:
        parts.append("(none)\n")
    parts.append("=== INPUT: Direct commits (no PR) ===\n")
    if direct:
        parts.extend(c.to_prompt_block() for c in direct)
    else:
        parts.append("(none)")
    parts.append(
        "\n=== END INPUT ===\n\nReturn the JSON object now. No fences, no prose."
    )
    return "\n".join(parts)


def call_claude(api_key: str, prompt: str, model: str) -> dict[str, Any]:
    """Single Claude API call. Returns parsed JSON object. Raises on failure."""
    payload = {
        "model": model,
        "max_tokens": ANTHROPIC_MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        ANTHROPIC_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Claude API HTTP {e.code}: {err}") from e

    data = json.loads(body)
    # Anthropic response: {"content": [{"type": "text", "text": "..."}, ...]}
    text_chunks = [
        block.get("text", "")
        for block in data.get("content", [])
        if block.get("type") == "text"
    ]
    raw = "".join(text_chunks).strip()

    # Defensive: strip ```json fences if the model ignores the no-fence rule.
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```\s*$", "", raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Claude returned non-JSON output (parse error: {e}).\n"
            f"Raw response (first 1000 chars): {raw[:1000]}"
        ) from e


# ─────────────────────────────────────────────────────────────────────────────
# Rendering
# ─────────────────────────────────────────────────────────────────────────────


RISK_BADGES = {"high": "🔴 HIGH RISK", "medium": "🟡 MED RISK", "low": ""}


def render_markdown(payload: dict[str, Any]) -> str:
    """Render the LLM's JSON payload into Asana-friendly markdown."""
    sections = []

    def render_section(title: str, entries: list[dict[str, Any]]) -> None:
        if not entries:
            return
        out = [f"### {title}"]
        for e in entries:
            risk = (e.get("risk") or "low").lower()
            badge = RISK_BADGES.get(risk, "")
            head = f"- **{e.get('title', '(untitled)')}**"
            if e.get("pr"):
                head += f" (#{e['pr']})"
            if badge:
                head += f"  {badge}"
            out.append(head)
            summary = (e.get("summary") or "").strip()
            if summary:
                out.append(f"  {summary}")
            # Per-entry QA steps used to be rendered here. QA team asked us to
            # drop them — the cross-cutting "QA Focus" section at the bottom
            # is the one place test guidance now appears. The LLM prompt no
            # longer requests qa_steps either, but we'd just ignore them
            # gracefully if a model included them anyway.
        sections.append("\n".join(out))

    render_section("Features", payload.get("features") or [])
    render_section("Fixes", payload.get("fixes") or [])
    render_section("Other", payload.get("other") or [])

    focus = payload.get("qa_focus") or []
    if focus:
        sections.append("### QA Focus\n" + "\n".join(f"- {f}" for f in focus))

    return "\n\n".join(sections).strip() or "(no notable changes detected)"


def render_short(payload: dict[str, Any], char_cap: int = 480) -> str:
    """Compact single-paragraph version for Play Store + Slack tail (≤500 chars)."""
    bullets: list[str] = []
    for section_key in ("features", "fixes"):
        for e in payload.get(section_key) or []:
            title = (e.get("title") or "").strip()
            if title:
                bullets.append(title)
    if not bullets:
        for e in payload.get("other") or []:
            title = (e.get("title") or "").strip()
            if title:
                bullets.append(title)
    if not bullets:
        return "Bug fixes and performance improvements."

    out = ""
    for b in bullets:
        candidate = (out + "\n• " + b) if out else ("• " + b)
        if len(candidate) > char_cap:
            break
        out = candidate
    return out or "Bug fixes and performance improvements."


# ─────────────────────────────────────────────────────────────────────────────
# SDK Changes — deterministic diff of Packages/manifest.json + packages-lock.json
# between the previous build tag and HEAD. Appended after the LLM-generated
# QA Focus section so QA can see exactly which third-party packages moved.
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_MANIFEST_PATH = "Packages/manifest.json"
DEFAULT_LOCKFILE_PATH = "Packages/packages-lock.json"


@dataclass
class PkgSnapshot:
    """One package's identity at a point in time.

    `ref` is the human-friendly version label (semver tag, git fragment, or
    None when the package is git-tracked with no explicit ref).
    `hash` is the resolved git SHA from packages-lock.json (None for registry
    packages, which don't have one).
    """

    ref: str | None
    hash: str | None

    @property
    def display(self) -> str:
        if self.ref:
            return self.ref
        if self.hash:
            return self.hash[:7]
        return "?"


def git_show(ref: str, path: str) -> str | None:
    """Read the file at a git ref. Returns None when the path or ref is absent."""
    res = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        capture_output=True,
        text=True,
    )
    return res.stdout if res.returncode == 0 else None


def _looks_like_url(s: str) -> bool:
    """Truthy for any git/file/registry URL we might encounter in manifests.

    SSH URLs (`git@github.com:foo/bar.git`) lack `://`, so we explicitly check
    for the other common prefixes too.
    """
    return (
        "://" in s
        or s.startswith("git@")
        or s.startswith("git+")
        or s.startswith("file:")
    )


def _parse_dependencies(content: str | None) -> dict[str, Any]:
    """Pull out the `dependencies` map from a manifest or lockfile blob."""
    if not content:
        return {}
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    deps = data.get("dependencies", {})
    return deps if isinstance(deps, dict) else {}


def snapshot_package(
    pkg_id: str, manifest: dict[str, Any], lockfile: dict[str, Any]
) -> PkgSnapshot | None:
    """Build a (ref, hash) snapshot from manifest + lockfile entries.

    Resolution priority for `ref`:
      1. Git URL fragment from manifest (e.g. `...#1.4.0` → "1.4.0").
      2. Plain version string in manifest (registry packages, e.g. "4.4.2").
      3. Registry version from lockfile (when manifest didn't have it directly).
    `hash` comes from packages-lock.json — only present for git-sourced packages.
    """
    in_m = pkg_id in manifest
    in_l = pkg_id in lockfile
    if not in_m and not in_l:
        return None

    ref: str | None = None
    m_val = manifest.get(pkg_id)
    if isinstance(m_val, str):
        if _looks_like_url(m_val):
            # URL — use the `#fragment` as ref if present, else leave ref None
            # so we fall back to the lockfile hash (branch-tracked package).
            if "#" in m_val:
                fragment = m_val.split("#", 1)[1].split("?", 1)[0].strip()
                ref = fragment or None
        else:
            ref = m_val  # plain version string (registry package)

    l_entry = lockfile.get(pkg_id)
    l_entry = l_entry if isinstance(l_entry, dict) else {}

    # If we didn't get a ref from the manifest, try the lockfile version —
    # but only if it's NOT itself a URL. Registry packages store their semver
    # here as a plain string; git packages store the URL we already handled
    # above (or a duplicate of it), so we ignore those to avoid showing the
    # full git URL as a "version".
    if ref is None:
        l_version = l_entry.get("version")
        if isinstance(l_version, str) and not _looks_like_url(l_version):
            ref = l_version

    h = l_entry.get("hash") if isinstance(l_entry.get("hash"), str) else None

    return PkgSnapshot(ref=ref, hash=h)


def diff_package(
    old: PkgSnapshot | None, new: PkgSnapshot | None
) -> str | None:
    """Return a markdown-formatted change description, or None for no change.

    Detects three flavours of change:
      • added / removed
      • display label changed (e.g. "1.4.0" → "1.5.0")
      • display label identical but resolved SHA moved — happens for git
        packages pinned to a branch like `#main`. Surfaced so QA isn't blind
        to silent version drift.
    """
    if old is None and new is None:
        return None
    if old is None:
        assert new is not None
        return f"added at `{new.display}`"
    if new is None:
        return f"removed (was `{old.display}`)"

    if old.display != new.display:
        return f"`{old.display}` → `{new.display}`"

    # Same display label — but did the underlying SHA move? (Branch-tracked deps.)
    if old.hash and new.hash and old.hash != new.hash:
        return (
            f"`{old.display}` (SHA `{old.hash[:7]}` → `{new.hash[:7]}`)"
        )

    return None


def compute_sdk_section(from_ref: str, to_ref: str) -> str:
    """Return the rendered '### SDK Changes' markdown block, or '' if no changes."""
    manifest_path = os.environ.get("SDK_MANIFEST_PATH", DEFAULT_MANIFEST_PATH)
    lockfile_path = os.environ.get("SDK_LOCKFILE_PATH", DEFAULT_LOCKFILE_PATH)

    old_m = _parse_dependencies(git_show(from_ref, manifest_path))
    new_m = _parse_dependencies(git_show(to_ref, manifest_path))
    old_l = _parse_dependencies(git_show(from_ref, lockfile_path))
    new_l = _parse_dependencies(git_show(to_ref, lockfile_path))

    if not (old_m or new_m or old_l or new_l):
        # No manifest/lockfile at either ref — skip the section silently.
        return ""

    all_pkgs = set(old_m) | set(new_m) | set(old_l) | set(new_l)

    lines: list[str] = []
    for pkg_id in sorted(all_pkgs):
        old_snap = snapshot_package(pkg_id, old_m, old_l)
        new_snap = snapshot_package(pkg_id, new_m, new_l)
        change = diff_package(old_snap, new_snap)
        if change:
            lines.append(f"- **{pkg_id}**: {change}")

    if not lines:
        return ""
    return "### SDK Changes\n" + "\n".join(lines)


def append_section(md: str, section: str) -> str:
    """Append a markdown section to existing notes, joining with a blank line."""
    if not section:
        return md
    if not md:
        return section
    return f"{md}\n\n{section}"


# ─────────────────────────────────────────────────────────────────────────────
# Outputs
# ─────────────────────────────────────────────────────────────────────────────


def write_outputs(**values: str) -> None:
    """Append KEY=value lines to GITHUB_OUTPUT, using heredoc for multiline."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        # Local dev: print to stdout in a parseable form.
        for k, v in values.items():
            print(f"::set-output::{k}={v!r}")
        return
    with open(path, "a", encoding="utf-8") as f:
        for k, v in values.items():
            if "\n" in v:
                # Heredoc form per GitHub Actions docs.
                marker = f"EOF_{k.upper()}_{os.urandom(4).hex()}"
                f.write(f"{k}<<{marker}\n{v}\n{marker}\n")
            else:
                f.write(f"{k}={v}\n")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────


def env_lines(name: str, default: list[str] | None = None) -> list[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return list(default) if default else []
    return [line.strip() for line in raw.splitlines() if line.strip()]


def fallback_output(
    notes: str, range_empty: bool, reason: str, sdk_section: str = ""
) -> None:
    sys.stderr.write(f"Using fallback release notes: {reason}\n")
    fallback = notes.strip() or "Bug fixes and performance improvements."
    # SDK section is deterministic and useful even when the LLM didn't run —
    # append it so QA still sees package movement.
    fallback_md = append_section(fallback, sdk_section)
    write_outputs(
        release_notes_md=fallback_md,
        release_notes_short=fallback[:480],
        range_empty="true" if range_empty else "false",
        used_fallback="true",
    )


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not repo:
        sys.stderr.write("ERROR: GITHUB_REPOSITORY env required\n")
        return 1

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    model = os.environ.get("ANTHROPIC_MODEL", "").strip() or DEFAULT_MODEL
    fallback_notes = os.environ.get("FALLBACK_RELEASE_NOTES", "").strip()
    project_name = os.environ.get("PROJECT_NAME", "this project").strip()
    extra_hint = os.environ.get("EXTRA_QA_HINT", "").strip()
    high_risk = env_lines("HIGH_RISK_PATHS", DEFAULT_HIGH_RISK_PATHS)
    ignore = env_lines("IGNORE_PATHS")

    from_ref, to_ref, used_fallback_range = resolve_range(
        os.environ.get("COMMIT_RANGE_FROM", ""),
        os.environ.get("COMMIT_RANGE_TO", ""),
    )
    sys.stderr.write(f"Commit range: {from_ref}..{to_ref}\n")

    # SDK changes are deterministic from the manifest/lockfile diff — compute
    # once here and append in every output path (LLM success, LLM failure,
    # missing key, empty range). The function returns '' when there's nothing
    # to show, so no special-casing needed downstream.
    sdk_section = compute_sdk_section(from_ref, to_ref)
    if sdk_section:
        sys.stderr.write(
            f"SDK changes detected: {sdk_section.count(chr(10)) - 1} package(s)\n"
        )

    # ── Enumerate PRs and direct commits in the range ───────────────────────
    merge_subjects = merge_commits_in_range(from_ref, to_ref)
    pr_numbers: list[int] = []
    seen_prs: set[int] = set()

    for _sha, subject in merge_subjects:
        n = parse_pr_number_from_subject(subject)
        if n and n not in seen_prs:
            pr_numbers.append(n)
            seen_prs.add(n)

    direct_rows = first_parent_non_merge_commits(from_ref, to_ref)
    direct: list[DirectCommit] = []
    for sha, subject, author in direct_rows:
        n = parse_pr_number_from_subject(subject)
        if n:
            # Squash-merged PR — track via gh, don't treat as a direct commit.
            if n not in seen_prs:
                pr_numbers.append(n)
                seen_prs.add(n)
            continue
        direct.append(DirectCommit(sha=sha, subject=subject, author=author))

    if not pr_numbers and not direct:
        return _emit_fallback_or_fail(
            fallback_notes,
            range_empty=True,
            reason="no commits or PRs in range",
            sdk_section=sdk_section,
        )

    # ── Fetch PR metadata ──────────────────────────────────────────────────
    prs: list[PullRequest] = []
    for n in pr_numbers:
        pr = fetch_pr(repo, n)
        if pr is None:
            continue
        pr.files = filter_files(pr.files, ignore)
        prs.append(pr)

    sys.stderr.write(
        f"Resolved {len(prs)} PR(s) and {len(direct)} direct commit(s)\n"
    )

    # ── Build prompt + call Claude ─────────────────────────────────────────
    config = ChangelogConfig(
        project_name=project_name,
        high_risk_paths=high_risk,
        ignore_paths=ignore,
        extra_qa_hint=extra_hint,
    )
    prompt = build_prompt(config, prs, direct)
    sys.stderr.write(f"Prompt size: {len(prompt)} chars\n")
    sys.stderr.write(f"Model: {model}\n")

    if not api_key:
        sys.stderr.write("ERROR: ANTHROPIC_API_KEY env required for generation\n")
        return _emit_fallback_or_fail(
            fallback_notes,
            range_empty=False,
            reason="missing ANTHROPIC_API_KEY",
            sdk_section=sdk_section,
        )

    try:
        payload = call_claude(api_key, prompt, model)
    except Exception as e:
        sys.stderr.write(f"ERROR: Claude call failed: {e}\n")
        return _emit_fallback_or_fail(
            fallback_notes,
            range_empty=False,
            reason=f"LLM failure: {e}",
            sdk_section=sdk_section,
        )

    md = append_section(render_markdown(payload), sdk_section)
    short = render_short(payload)

    write_outputs(
        release_notes_md=md,
        release_notes_short=short,
        range_empty="false",
        used_fallback="false",
    )
    sys.stderr.write("OK: changelog generated\n")
    return 0


def _emit_fallback_or_fail(
    notes: str, range_empty: bool, reason: str, sdk_section: str = ""
) -> int:
    if notes:
        fallback_output(notes, range_empty, reason, sdk_section)
        return 0
    sys.stderr.write(
        f"ERROR: {reason} and no FALLBACK_RELEASE_NOTES provided — failing.\n"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
