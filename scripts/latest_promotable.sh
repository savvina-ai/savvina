#!/bin/sh
# Exit 0 when TAG should become `latest`: its commit is reachable from
# origin/main and it is the newest vX.Y.Z tag *that is itself on origin/main*.
# Re-running an old tag's publish, or tagging a backport branch, therefore
# never moves `latest` backwards. A tag hand-pushed on an unmerged branch is
# ignored rather than blocking real releases forever. Exit 2 means the check
# itself could not be performed (e.g. origin/main is missing) -- this is
# distinct from a legitimate refusal (exit 1) so it doesn't get silently
# treated as "not promotable" by the caller.
set -eu

tag="${1:?usage: latest_promotable.sh vX.Y.Z}"

# ^{commit} because semantic-release creates annotated tags, whose own object
# id is not the commit's.
commit="$(git rev-parse "${tag}^{commit}")"

# Resolve origin/main explicitly before using it as an ancestor-check argument.
# `git merge-base --is-ancestor` exits 128 with the same shape of failure
# whether the ref is missing or the tag genuinely isn't an ancestor, so without
# this check a missing origin/main (e.g. from a shallower `fetch-depth`) would
# be misreported as "$tag is not on main" -- a routine-looking refusal that
# actually means the check never ran. The caller (the `promote` job in
# docker-publish.yml) fails on exit 2, so this message is what the failed run
# shows; keep it loud and distinct from the two legitimate refusals.
if ! main_commit="$(git rev-parse --verify -q origin/main^{commit})"; then
    echo "ERROR: origin/main does not resolve; cannot determine promotability of $tag (does the checkout need fetch-depth: 0?)"
    exit 2
fi

if ! git merge-base --is-ancestor "$commit" "$main_commit"; then
    echo "$tag is not on main; latest unchanged"
    exit 1
fi

# --merged origin/main restricts candidates to tags reachable from main, so a
# tag hand-pushed on a feature branch can't outrank every real release and
# freeze `latest` until someone notices and deletes it.
# -c versionsort.suffix=- sorts a `-rc.N`-style suffix *below* its release
# (e.g. v2.1.0 above v2.1.0-rc.1), matching semantic-release's own precedence;
# without it, a hand-cut prerelease tag would outrank the stable release that
# follows it. Passed as `-c` (not `git config`) so it only affects this one
# invocation.
newest="$(git -c versionsort.suffix=- tag --merged origin/main --list 'v*.*.*' --sort=-v:refname | head -n 1)"
if [ "$newest" != "$tag" ]; then
    echo "$tag is older than $newest; latest unchanged"
    exit 1
fi

echo "$tag is the newest release on main; promoting to latest"
