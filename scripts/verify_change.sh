#!/usr/bin/env bash
# Policy gates for one lab change. The publish job runs this before anything
# is pushed. Every check is derived from the commit itself: the job that made
# the commit ran a model with a shell, so nothing it reports is taken on trust.
#
# usage: verify_change.sh BASE NEW
#   Run inside a clone of the target repository that contains both commits.
#
# environment:
#   EXPECT_NAME, EXPECT_EMAIL  the only identity allowed on the new commit
#   ALLOW_WORKFLOW_EDITS       "false" rejects changes under .github/workflows/
#   MAX_CHANGED_LINES          cap on added+deleted lines, lockfiles excluded
#   MAX_FILE_BYTES             cap on any added or modified file (default 2 MiB)
#
# On success prints `changed_lines=N` on stdout. On the first violation prints
# a GitHub ::error:: annotation and exits 1.

set -euo pipefail

BASE="${1:?usage: verify_change.sh BASE NEW}"
NEW="${2:?usage: verify_change.sh BASE NEW}"
: "${EXPECT_NAME:?EXPECT_NAME is required}"
: "${EXPECT_EMAIL:?EXPECT_EMAIL is required}"
: "${MAX_CHANGED_LINES:?MAX_CHANGED_LINES is required}"
ALLOW_WORKFLOW_EDITS="${ALLOW_WORKFLOW_EDITS:-unknown}"
MAX_FILE_BYTES="${MAX_FILE_BYTES:-2097152}"

fail() {
  echo "::error::$*"
  exit 1
}

# 0. Shape: exactly one new, non-merge commit, directly on top of BASE.
git cat-file -e "$BASE^{commit}" 2>/dev/null || fail "base $BASE is not a commit in this repository"
git cat-file -e "$NEW^{commit}" 2>/dev/null || fail "change $NEW is not a commit in this repository"
read -r -a PARENTS <<<"$(git rev-list --parents -n 1 "$NEW")"
[ "${#PARENTS[@]}" -eq 2 ] || fail "the change must be a single non-merge commit"
[ "${PARENTS[1]}" = "$(git rev-parse "$BASE^{commit}")" ] \
  || fail "the change must be exactly one commit directly on top of $BASE"

# 1. Identity. GitHub lists commit authors as contributors, so the author must
#    be the owner; the committer is checked too so nothing else rides along.
IFS=$'\t' read -r AUTHOR_NAME AUTHOR_EMAIL COMMITTER_NAME COMMITTER_EMAIL \
  < <(git log -1 --format='%an%x09%ae%x09%cn%x09%ce' "$NEW")
if [ "$AUTHOR_NAME" != "$EXPECT_NAME" ] || [ "$AUTHOR_EMAIL" != "$EXPECT_EMAIL" ]; then
  fail "the commit is authored by '$AUTHOR_NAME <$AUTHOR_EMAIL>', expected '$EXPECT_NAME <$EXPECT_EMAIL>'"
fi
if [ "$COMMITTER_NAME" != "$EXPECT_NAME" ] || [ "$COMMITTER_EMAIL" != "$EXPECT_EMAIL" ]; then
  fail "the commit is committed by '$COMMITTER_NAME <$COMMITTER_EMAIL>', expected '$EXPECT_NAME <$EXPECT_EMAIL>'"
fi

# 2. No co-author trailers. GitHub credits co-authors, and a squash merge
#    would carry the trailer onto the default branch. No closing keywords
#    either: on the default branch "fixes #3" in a message closes issue 3.
MESSAGE="$(git log -1 --format=%B "$NEW")"
if grep -qiE '^[[:space:]]*co-authored-by:' <<<"$MESSAGE"; then
  fail "the commit message carries a Co-authored-by trailer"
fi
if grep -qiE '(^|[^[:alnum:]_])(close[sd]?|fix(e[sd])?|resolve[sd]?)[[:space:]]*:?[[:space:]]*([[:alnum:]_.-]+/[[:alnum:]_.-]+)?#[0-9]+' <<<"$MESSAGE"; then
  fail "the commit message contains a closing keyword (like 'fixes #3') that would close an issue on merge"
fi

# 3. No submodules (gitlinks have mode 160000).
if git diff --raw --no-renames "$BASE" "$NEW" | awk '$2 == "160000" { found = 1 } END { exit !found }'; then
  fail "the change adds a git submodule"
fi

# 4. Nothing generated or credential-shaped is added or modified. Deleting
#    such files is allowed: that is how committed artefacts get cleaned up.
JUNK='(^|/)(node_modules|__pycache__|\.venv|venv|\.tox|\.mypy_cache|\.pytest_cache|\.ruff_cache|dist|build|target|\.next|coverage|htmlcov)/|(^|/)\.coverage$|(^|/)\.env$|\.(pyc|log|db|sqlite|sqlite3|db-wal|db-shm|pem|key|p12|pfx|keystore|jks)$|(^|/)id_(rsa|ecdsa|ed25519)$'
if BAD="$(git diff --name-only --no-renames --diff-filter=AMT "$BASE" "$NEW" | grep -E "$JUNK")"; then
  fail "the change adds generated or credential-shaped files: $(echo "$BAD" | tr '\n' ' ')"
fi

# 5. No credential shapes in added lines.
SECRETS='gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AIza[0-9A-Za-z_-]{35}|"type": *"service_account"|(AKIA|ASIA)[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|xox[abprs]-[A-Za-z0-9-]{10,}|sk-ant-[A-Za-z0-9_-]{20,}|glpat-[A-Za-z0-9_-]{20}'
if git diff --no-renames -U0 "$BASE" "$NEW" | grep -E '^\+' | grep -v '^+++ ' | grep -Eq "$SECRETS"; then
  fail "an added line looks like a credential"
fi

# 6. Workflow files can only be pushed with the token's workflow scope. Say so
#    plainly here rather than letting the push fail with a cryptic message.
if [ "$ALLOW_WORKFLOW_EDITS" = "false" ] \
  && git diff --name-only --no-renames "$BASE" "$NEW" | grep -q '^\.github/workflows/'; then
  fail "the change edits .github/workflows/, but GH_PROFILE_TOKEN lacks the workflow scope"
fi

# 7. Reviewable size, lockfiles excluded. Binary files count as zero.
CHANGED="$(git diff --numstat --no-renames "$BASE" "$NEW" -- . \
  ':(exclude,glob)**/*.lock' ':(exclude,glob)**/package-lock.json' ':(exclude,glob)**/pnpm-lock.yaml' \
  | awk '$1 != "-" { n += $1 + $2 } END { print n + 0 }')"
if [ "$CHANGED" -gt "$MAX_CHANGED_LINES" ]; then
  fail "the change touches $CHANGED lines; the cap is $MAX_CHANGED_LINES (lockfiles excluded)"
fi

# 8. No large files, since binary files count as zero lines above.
BIG=""
while IFS=$'\t' read -r META FILE; do
  SIZE="$(git cat-file -s "$(awk '{ print $4 }' <<<"$META")")"
  if [ "$SIZE" -gt "$MAX_FILE_BYTES" ]; then BIG="$BIG $FILE ($SIZE bytes)"; fi
done < <(git diff --raw --no-abbrev --no-renames --diff-filter=AMT "$BASE" "$NEW")
if [ -n "$BIG" ]; then
  fail "the change adds files larger than $MAX_FILE_BYTES bytes:$BIG"
fi

echo "changed_lines=$CHANGED"
