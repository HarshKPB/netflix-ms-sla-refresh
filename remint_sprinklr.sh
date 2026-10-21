#!/usr/bin/env bash
# One-command Sprinklr session re-mint. The Sprinklr dashboard login is Netflix SSO
# plus 2FA, which cannot be automated, so step 1 is manual (a browser opens, you sign
# in). Everything after that is automatic: push the fresh session into the GitHub
# secret and kick off the refresh so Sprinklr rows come back.
#
#   cd ~/Desktop/MISC/netflix-ms-sla-refresh && ./remint_sprinklr.sh
set -euo pipefail
cd "$(dirname "$0")"

REPO="HarshKPB/netflix-ms-sla-refresh"
SESSION="$HOME/.netflix_sprinklr_session.json"
PY=".venv/bin/python"
[ -x "$PY" ] || PY="python3"

echo "1/3  Opening Sprinklr login. Sign in and clear 2FA in the browser window that opens..."
"$PY" setup_session.py

if ! "$PY" -c "import json,sys; sys.exit(0 if json.load(open('$SESSION')).get('cookies') else 1)" 2>/dev/null; then
  echo "ERROR: $SESSION has no cookies. Login did not complete; re-run and finish signing in." >&2
  exit 1
fi

echo "2/3  Updating the GitHub secret from the fresh session..."
gh secret set SPRINKLR_SESSION_JSON --repo "$REPO" < "$SESSION"

echo "3/3  Re-running the refresh workflow..."
gh workflow run refresh.yml --repo "$REPO"

echo "Done. Sprinklr rows should return in a few minutes."
echo "Watch it:  gh run watch \$(gh run list --repo $REPO --workflow=refresh.yml --limit 1 --json databaseId -q '.[0].databaseId') --repo $REPO"
