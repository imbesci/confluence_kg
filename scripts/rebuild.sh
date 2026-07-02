#!/usr/bin/env bash
# Scheduled refresh: rebuild the knowledge tree and open it as a reviewable PR.
set -euo pipefail
cd "$(dirname "$0")/.."

# 1. Refresh the scrape (plug in your own scraper here)
# python my_confluence_scraper.py --out ./confluence_dump

# 2. Rebuild — idempotent: only changed pages and their ancestor summaries are regenerated
python -m kg build

# 3. Ship the diff for human review
BRANCH="kg-refresh-$(date +%Y%m%d-%H%M)"
git checkout -b "$BRANCH"
git add knowledge/
git commit -m "kg refresh $(date +%F)" || { echo "no changes"; exit 0; }
git push -u origin "$BRANCH"
# gh pr create --fill    # uncomment if you use GitHub CLI
