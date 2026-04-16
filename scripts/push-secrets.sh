#!/usr/bin/env bash
# push-secrets.sh — Push GitHub secrets and variables to one or more repos.
#
# USAGE:
#   # Shared secrets only (root .secrets.env):
#   ./scripts/push-secrets.sh WebAvenueIG/repo1
#
#   # Shared + project-specific (project values override root):
#   ./scripts/push-secrets.sh --project my-app WebAvenueIG/repo1
#
#   # Multiple repos:
#   ./scripts/push-secrets.sh --project my-app WebAvenueIG/repo1 WebAvenueIG/repo2
#
#   # Via env var:
#   GITHUB_REPOS="WebAvenueIG/repo1" ./scripts/push-secrets.sh --project my-app
#
#   # DRY RUN (prints what would be pushed without calling gh):
#   DRY_RUN=1 ./scripts/push-secrets.sh --project my-app WebAvenueIG/repo1
#
# CONVENTIONS in .secrets.env:
#   - [secrets] section              → pushed as GitHub Secrets    (masked in logs)
#   - [variables] section            → pushed as GitHub Variables  (visible in logs)
#   - Keys suffixed with _BASE64 or _B64 → value is a FILE PATH; base64-encoded before pushing
#   - Lines starting with #          → comments, ignored
#   - Blank lines                    → ignored

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[info]${NC}  $*"; }
success() { echo -e "${GREEN}[ok]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[warn]${NC}  $*"; }
error()   { echo -e "${RED}[error]${NC} $*" >&2; }

# ── Parse arguments ───────────────────────────────────────────────────────────
PROJECT=""
REPOS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)
      if [[ -z "${2:-}" ]]; then
        error "--project requires a project name argument"
        exit 1
      fi
      PROJECT="$2"
      shift 2
      ;;
    *)
      REPOS+=("$1")
      shift
      ;;
  esac
done

# ── Preflight checks ──────────────────────────────────────────────────────────
if ! command -v gh &>/dev/null; then
  error "GitHub CLI (gh) not found. Install it: https://cli.github.com"
  exit 1
fi

if ! gh auth status &>/dev/null; then
  error "Not authenticated with gh. Run: gh auth login"
  exit 1
fi

ROOT_ENV="$REPO_ROOT/.secrets.env"
PROJECT_ENV=""
[[ -n "$PROJECT" ]] && PROJECT_ENV="$REPO_ROOT/$PROJECT/.secrets.env"

if [[ ! -f "$ROOT_ENV" ]] && { [[ -z "$PROJECT_ENV" ]] || [[ ! -f "$PROJECT_ENV" ]]; }; then
  error "No .secrets.env found."
  error "Expected: $ROOT_ENV"
  [[ -n "$PROJECT_ENV" ]] && error "    and/or: $PROJECT_ENV"
  error "Copy .secrets.env.example → .secrets.env and fill in your values."
  exit 1
fi

if [[ -n "$PROJECT_ENV" ]] && [[ ! -f "$PROJECT_ENV" ]]; then
  error "Project secrets file not found: $PROJECT_ENV"
  exit 1
fi

# ── Resolve target repos ──────────────────────────────────────────────────────
if [[ ${#REPOS[@]} -eq 0 ]]; then
  if [[ -n "${GITHUB_REPOS:-}" ]]; then
    read -ra REPOS <<< "$GITHUB_REPOS"
  else
    error "No repos specified."
    error "Usage: $0 [--project <name>] org/repo1 [org/repo2 ...]"
    error "   or: GITHUB_REPOS='org/repo1' $0 [--project <name>]"
    exit 1
  fi
fi

DRY_RUN="${DRY_RUN:-0}"
[[ "$DRY_RUN" == "1" ]] && warn "DRY RUN mode — no changes will be made."

# ── Parse env files into a temp file ─────────────────────────────────────────
# Each line in the temp file: <section> TAB <key> TAB <value>
# Root is parsed first, project second — so project entries override root via
# awk deduplication (last occurrence of each key wins).
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

parse_env_file() {
  local file="$1"
  [[ ! -f "$file" ]] && return
  local section="secrets"

  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ -z "${line//[[:space:]]/}" ]] && continue

    # Section headers: [secrets] or [variables]
    if [[ "$line" =~ ^\[([a-z]+)\]$ ]]; then
      section="${BASH_REMATCH[1]}"
      if [[ "$section" != "secrets" && "$section" != "variables" ]]; then
        warn "Unknown section [$section] in $file — skipping"
        section="secrets"
      fi
      continue
    fi

    if [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
      local key="${BASH_REMATCH[1]}"
      local value="${BASH_REMATCH[2]}"

      # Strip surrounding quotes if present
      if [[ "$value" =~ ^\"(.*)\"$ ]] || [[ "$value" =~ ^\'(.*)\'$ ]]; then
        value="${BASH_REMATCH[1]}"
      fi

      # _BASE64 or _B64 suffix → value is a file path; base64-encode its contents
      if [[ "$key" == *_BASE64 || "$key" == *_B64 ]]; then
        local fp="$value"
        [[ "$fp" != /* ]] && fp="$REPO_ROOT/$fp"
        if [[ ! -f "$fp" ]]; then
          warn "File not found for $key: $fp — skipping"
          continue
        fi
        value="$(base64 < "$fp" | tr -d '\n')"
      fi

      # Write tab-separated: section TAB key TAB value
      printf '%s\t%s\t%s\n' "$section" "$key" "$value" >> "$TMP"
    fi
  done < "$file"
}

# Load root first, then project (project entries override root via awk below)
parse_env_file "$ROOT_ENV"
[[ -n "$PROJECT_ENV" ]] && parse_env_file "$PROJECT_ENV"

# Deduplicate: keep last occurrence of each (section+key) pair, preserving order
DEDUPED=$(awk -F'\t' '
{
  composite = $1 "\t" $2
  data[composite] = $0
  if (!(composite in seen)) { order[++n] = composite; seen[composite] = 1 }
}
END {
  for (i = 1; i <= n; i++) print data[order[i]]
}' "$TMP")

secret_count=$(echo "$DEDUPED" | grep -c "^secrets" || true)
var_count=$(echo "$DEDUPED" | grep -c "^variables" || true)

if [[ -n "$PROJECT" ]]; then
  info "Loaded root + project '$PROJECT' — ${secret_count} secret(s), ${var_count} variable(s)"
else
  info "Loaded root — ${secret_count} secret(s), ${var_count} variable(s)"
fi
echo ""

# ── Push to each repo ─────────────────────────────────────────────────────────
for repo in "${REPOS[@]}"; do
  echo -e "${BLUE}━━━ $repo ━━━${NC}"

  while IFS=$(printf '\t') read -r section key value; do
    if [[ "$section" == "secrets" ]]; then
      if [[ "$DRY_RUN" == "1" ]]; then
        echo "  [dry-run] secret   $key"
      else
        echo -n "$value" | gh secret set "$key" --repo "$repo"
        success "secret   $key"
      fi
    else
      if [[ "$DRY_RUN" == "1" ]]; then
        echo "  [dry-run] variable $key = $value"
      else
        gh variable set "$key" --repo "$repo" --body "$value"
        success "variable $key"
      fi
    fi
  done <<< "$DEDUPED"

  echo ""
done

success "Done."
