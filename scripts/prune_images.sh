#!/bin/sh
# Remove superseded CI images and bound the BuildKit cache on the production host.
#
# Scope is deliberately narrow: only image tags matching center-messenger:gitverse-*
# and the builder cache. Running containers, volumes, .secrets files and the
# /opt release directories are never touched, so the active deployment and the
# backups kept by the deploy controller stay intact.
set -eu

# Keep the image the running API uses plus this many most recent CI images, so a
# rollback to the previous release still has its image locally.
KEEP_RECENT=2
# BuildKit cache also holds the apt cache mounts, so it is bounded rather than
# wiped: pruning it fully would make the next build download Chromium deps again.
KEEP_CACHE=8GB

# A running build owns the cache mounts and the images it has just produced;
# removing either of them under it makes the build fail for no reason. Nightly
# runs then simply skip and wait for the next one.
if pgrep -f 'docker build' >/dev/null 2>&1; then
    echo "docker build is running, skipping prune"
    exit 0
fi

active=$(docker inspect --format '{{.Config.Image}}' center-messenger-api-1 || true)

recent=$(docker image ls --format '{{.CreatedAt}}\t{{.Repository}}:{{.Tag}}' \
    | grep -F 'center-messenger:gitverse-' \
    | sort -r \
    | cut -f2 \
    | head -n "$KEEP_RECENT")

keep=$(printf '%s\n%s\n' "$active" "$recent" | sed '/^$/d' | sort -u)

for img in $(docker image ls --format '{{.Repository}}:{{.Tag}}' | grep -F 'center-messenger:gitverse-'); do
    if printf '%s\n' "$keep" | grep -qxF "$img"; then
        echo "keep    $img"
    else
        echo "remove  $img"
        docker image rm "$img" >/dev/null 2>&1 || echo "skipped $img (in use)"
    fi
done

docker image prune -f >/dev/null

# Pull request verification images are disposable: the workflow removes its own,
# and a leftover from an interrupted run is collected here.
for img in $(docker image ls --format '{{.Repository}}:{{.Tag}}' | grep -F 'center-messenger:pr-'); do
    echo "remove  $img"
    docker image rm "$img" >/dev/null 2>&1 || echo "skipped $img (in use)"
done

docker builder prune -f --keep-storage "$KEEP_CACHE" >/dev/null
echo "prune finished"
