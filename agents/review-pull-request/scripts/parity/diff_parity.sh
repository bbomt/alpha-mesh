#!/usr/bin/env bash
# PMAT-314 (d): compare `gh pr diff` with the compare API baseRefOid...headRefOid, per PR.
# Needed once for the PMAT-314 refactor (full reviews moved from `gh pr diff` to the compare API);
# safe to delete later. Usage: diff_parity.sh owner/repo:PR ...   (exit 1 if any PR differs)
set -u
out="${TMPDIR:-/tmp}/parity"; mkdir -p "$out"
fail=0
for spec in "$@"; do
  repo="${spec%%:*}"; pr="${spec##*:}"; tag="${repo//\//_}_$pr"
  # A failed gh call leaves an empty file, and two empty files would compare as SAME.
  if ! shas=$(gh pr view "$pr" --repo "$repo" --json baseRefOid,headRefOid --jq '"\(.baseRefOid) \(.headRefOid)"') \
      || ! read -r base head <<< "$shas" || [[ -z "$base" || -z "$head" ]] \
      || ! gh pr diff "$pr" --repo "$repo" > "$out/$tag.pr.diff" \
      || ! gh api -H "Accept: application/vnd.github.v3.diff" "repos/$repo/compare/$base...$head" > "$out/$tag.cmp.diff" \
      || [[ ! -s "$out/$tag.pr.diff" ]]; then
    fail=1
    echo "ERROR $spec: a gh call failed or returned an empty diff"
    continue
  fi
  if cmp -s "$out/$tag.pr.diff" "$out/$tag.cmp.diff"; then
    echo "SAME  $spec base=${base:0:7} head=${head:0:7} bytes=$(wc -c < "$out/$tag.pr.diff")"
  else
    fail=1
    echo "DIFF  $spec base=${base:0:7} head=${head:0:7} pr=$(wc -c < "$out/$tag.pr.diff") cmp=$(wc -c < "$out/$tag.cmp.diff")"
  fi
done
exit $fail
