#!/usr/bin/env bash
#
# One-shot setup for the TimeTree → Skylight sync.
#
# Run this from inside the unzipped `timetree-skylight-sync/` folder, on your
# own machine. It does everything that can be automated:
#   1. creates a PUBLIC GitHub repo and pushes this code
#   2. enables GitHub Pages (Actions source)
#   3. sets the secrets + variables (your password is read silently and sent
#      straight to GitHub's encrypted secret store — never printed or stored)
#   4. runs the calendar-list helper and shows your calendars
#   5. sets your chosen calendar id and triggers the first sync
#
# The only things left for you afterward: subscribe to the printed feed URL in
# Skylight, and (optionally) eyeball the first run.
#
# Requirements:
#   - git
#   - GitHub CLI `gh` (https://cli.github.com/), authenticated with:
#       gh auth login --scopes 'repo,workflow'
#     (the 'workflow' scope is required to push the .github/workflows files)
#   - python3 or uuidgen (for the random feed slug)

set -euo pipefail

REPO_NAME="${REPO_NAME:-timetree-skylight-sync}"

bold(){ printf '\n\033[1m▶ %s\033[0m\n' "$*"; }
warn(){ printf '\033[33m! %s\033[0m\n' "$*"; }
die(){ printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

command -v git >/dev/null || die "git is not installed."
command -v gh  >/dev/null || die "GitHub CLI 'gh' is not installed → https://cli.github.com/"
gh auth status >/dev/null 2>&1 || die "Authenticate first:  gh auth login --scopes 'repo,workflow'"
[ -f sync.py ] || die "Run this from inside the timetree-skylight-sync folder (sync.py not found)."

OWNER="$(gh api user --jq .login)"
REPO="$OWNER/$REPO_NAME"
PAGES_BASE="https://$OWNER.github.io/$REPO_NAME"

bold "Target repository: $REPO"

# --- 1. Create (or reuse) the repo and push -------------------------------- #
if gh repo view "$REPO" >/dev/null 2>&1; then
  bold "Repo already exists — pushing latest files"
  [ -d .git ] || git init -q
  git add -A
  git commit -qm "TimeTree → Skylight sync" || true
  git branch -M main
  git remote add origin "https://github.com/$REPO.git" 2>/dev/null || true
  git push -u origin main
else
  bold "Creating public repo and pushing"
  [ -d .git ] || git init -q
  git add -A
  git commit -qm "TimeTree → Skylight sync"
  git branch -M main
  gh repo create "$REPO" --public --source=. --remote=origin --push
fi

# --- 2. Enable GitHub Pages (Actions source) ------------------------------- #
bold "Enabling GitHub Pages (Actions source)"
gh api -X POST "repos/$REPO/pages" -f build_type=workflow >/dev/null 2>&1 \
  || gh api -X PUT "repos/$REPO/pages" -f build_type=workflow >/dev/null 2>&1 \
  || warn "Couldn't toggle Pages via API. Enable manually: Settings → Pages → Source: GitHub Actions."

# --- 3. Secrets & variables ------------------------------------------------ #
bold "Configuring secrets & variables"

# Unguessable feed slug (keeps the URL unlisted).
if command -v python3 >/dev/null 2>&1; then
  FEED_SLUG="${FEED_SLUG:-$(python3 -c 'import uuid;print(uuid.uuid4().hex)')}"
else
  FEED_SLUG="${FEED_SLUG:-$(uuidgen | tr -d - | tr 'A-Z' 'a-z')}"
fi
printf '%s' "$FEED_SLUG" | gh secret set FEED_SLUG -R "$REPO"

read -rp "TimeTree email: " TT_EMAIL
printf '%s' "$TT_EMAIL" | gh secret set TIMETREE_EMAIL -R "$REPO"

read -rsp "TimeTree password (hidden): " TT_PASS; echo
printf '%s' "$TT_PASS" | gh secret set TIMETREE_PASSWORD -R "$REPO"
unset TT_PASS

read -rp "Calendar display name for Skylight [Family]: " FEED_NAME
FEED_NAME="${FEED_NAME:-Family}"
gh variable set FEED_NAME   -R "$REPO" --body "$FEED_NAME"
gh variable set PAGES_BASE_URL -R "$REPO" --body "$PAGES_BASE"

# --- 4. Discover calendars ------------------------------------------------- #
bold "Finding your calendars (running the list-calendars workflow)…"
gh workflow run list-calendars.yml -R "$REPO" >/dev/null
sleep 8
for _ in $(seq 1 30); do
  ST="$(gh run list -R "$REPO" --workflow=list-calendars.yml --limit 1 --json status --jq '.[0].status' 2>/dev/null || true)"
  [ "$ST" = "completed" ] && break
  sleep 6
done
RID="$(gh run list -R "$REPO" --workflow=list-calendars.yml --limit 1 --json databaseId --jq '.[0].databaseId' 2>/dev/null || true)"
echo
if [ -n "$RID" ] && gh run view "$RID" -R "$REPO" --log 2>/dev/null | grep -E 'id=.*code=.*name='; then
  :
else
  warn "Couldn't auto-read the calendar list. Open: Actions tab → 'List TimeTree calendars' → latest run."
fi

# --- 5. Pick the calendar and run the first sync --------------------------- #
read -rp $'\nEnter the calendar id to export: ' TT_CAL
printf '%s' "$TT_CAL" | gh secret set TIMETREE_CALENDAR_ID -R "$REPO"

bold "Triggering the first sync"
gh workflow run sync.yml -R "$REPO" >/dev/null

FEED_URL="$PAGES_BASE/feed/$FEED_SLUG/calendar.ics"
printf '\n\033[32m✅ All set.\033[0m\n'
cat <<EOF

Your fixed feed URL — subscribe to this ONCE in Skylight
(Sync new calendar → Calendar URL):

    $FEED_URL

Handy commands:
    gh run watch -R "$REPO"          # watch the sync finish
    curl -sL "$FEED_URL" | head      # confirm it serves an .ics
EOF
