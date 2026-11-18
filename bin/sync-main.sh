#!/usr/bin/env bash
# alpha-mesh/bin/sync-main.sh
# mesh sync-main --repo-dir DIR: fast-forwards a main checkout to origin's default branch, then verifies that the
# branch equals origin's and the tree is clean. Fails loudly otherwise: inside the Claude Code sandbox git cannot
# write settings.json, and a fast-forward then writes every other file but leaves the branch and index behind.
# Never resets on its own.

set -euo pipefail

if [ $# -ne 2 ] || [ "$1" != "--repo-dir" ]; then
  echo "usage: mesh sync-main --repo-dir DIR   (the main checkout to fast-forward)" >&2
  exit 2
fi
cd "$2"

fail() { echo "Not synced: $*" >&2; exit 1; }

default="$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null)" \
  || fail "origin/HEAD is not set, so the default branch is unknown. Run: git remote set-head origin --auto"
default="${default#origin/}"
branch="$(git branch --show-current)"
[ "$branch" = "$default" ] || fail "$PWD is on '${branch:-a detached HEAD}', not the default branch '$default'."
dirty="$(git status --porcelain)"
[ -z "$dirty" ] || fail "$PWD is not clean; nothing was changed:"$'\n'"$dirty"

git fetch origin
if ! git merge --ff-only "origin/$default" || [ "$(git rev-parse HEAD)" != "$(git rev-parse "origin/$default")" ] \
   || [ -n "$(git status --porcelain)" ]; then
  echo "" >&2
  echo "Not synced: $default is at $(git rev-parse --short HEAD), origin/$default at $(git rev-parse --short "origin/$default")." >&2
  echo "Files left changed on disk (a half-applied fast-forward if this lists files):" >&2
  git status --porcelain | sed 's/^/  /' >&2
  echo "Was this run inside the Claude Code sandbox? It blocks writes to settings.json and .git/config." >&2
  echo "Repair, outside the sandbox, after checking the files above only come from the incoming commits:" >&2
  echo "  git -C $PWD reset --hard origin/$default" >&2
  exit 1
fi
echo "Synced: $default is at origin/$default ($(git rev-parse --short HEAD)), and the tree is clean."
