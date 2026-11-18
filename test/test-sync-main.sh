#!/usr/bin/env bash
# alpha-mesh/test/test-sync-main.sh
# Runs `mesh sync-main` against temp clones of a temp upstream and checks it either fast-forwards to a clean,
# verified default branch or fails loudly with the branch unchanged. A write-protected directory stands in for
# the sandbox's mask on settings.json (git unlinks and recreates files, so a read-only file alone does not block it).

set -euo pipefail

MESH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'chmod -R u+w "$WORK"; rm -rf "$WORK"' EXIT
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t

git init -q -b master "$WORK/up"
mkdir "$WORK/up/cfg"
echo one > "$WORK/up/cfg/settings.json"; echo one > "$WORK/up/other"
git -C "$WORK/up" add -A; git -C "$WORK/up" commit -qm one

clone() {  # clone <name>: a clone whose master and origin/master are at commit "one"; upstream is at "two"
  git clone -q "$WORK/up" "$WORK/$1"
  git -C "$WORK/$1" reset -q --hard "$(git -C "$WORK/up" rev-list --max-parents=0 HEAD)"
  git -C "$WORK/$1" update-ref refs/remotes/origin/master HEAD
}
echo two > "$WORK/up/cfg/settings.json"; echo two > "$WORK/up/other"; echo two > "$WORK/up/new"
git -C "$WORK/up" add -A; git -C "$WORK/up" commit -qm two
UP_HEAD="$(git -C "$WORK/up" rev-parse HEAD)"

fails=0
check() {  # check <name> <condition...>
  local name="$1"; shift
  if "$@"; then echo "  PASS: $name"; else echo "  FAIL: $name"; echo "$out" | sed 's/^/    /'; fails=$((fails + 1)); fi
}
sync() {  # sync <args...>: runs mesh sync-main, sets $out and $code
  set +e; out="$("$MESH_DIR/bin/mesh" sync-main "$@" 2>&1)"; code=$?; set -e
}

echo "=== sync-main tests ==="

clone ok; sync --repo-dir "$WORK/ok"
check "(b) a clean clone fast-forwards and verifies" \
  test "$code" -eq 0 -a "$(git -C "$WORK/ok" rev-parse HEAD)" = "$UP_HEAD" -a -z "$(git -C "$WORK/ok" status --porcelain)"

clone prot; before="$(git -C "$WORK/prot" rev-parse HEAD)"; chmod 555 "$WORK/prot/cfg"; sync --repo-dir "$WORK/prot"
check "(a) a protected file fails loudly and names the file" \
  bash -c '[ "$1" -ne 0 ] && grep -q "cfg/settings.json" <<<"$2"' _ "$code" "$out"
check "(a) a protected file leaves the branch unchanged" test "$(git -C "$WORK/prot" rev-parse HEAD)" = "$before"
chmod 755 "$WORK/prot/cfg"

clone dirty; before="$(git -C "$WORK/dirty" rev-parse HEAD)"; echo local > "$WORK/dirty/other"; sync --repo-dir "$WORK/dirty"
check "(c) a dirty checkout is refused and unchanged" \
  bash -c '[ "$1" -ne 0 ] && grep -qi "not clean" <<<"$2"' _ "$code" "$out"
check "(c) a dirty checkout is not fetched" \
  test "$(git -C "$WORK/dirty" rev-parse HEAD)" = "$before" -a "$(git -C "$WORK/dirty" rev-parse origin/master)" = "$before"

clone branch; git -C "$WORK/branch" checkout -q -b feature; sync --repo-dir "$WORK/branch"
check "a checkout not on its default branch is refused" \
  bash -c '[ "$1" -ne 0 ] && grep -q "feature" <<<"$2"' _ "$code" "$out"

sync
check "--repo-dir is required" bash -c '[ "$1" -eq 2 ] && grep -q -- "--repo-dir" <<<"$2"' _ "$code" "$out"

[ "$fails" -eq 0 ] || exit 1
