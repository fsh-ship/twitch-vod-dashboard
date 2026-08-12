#!/usr/bin/env bash
set -euo pipefail

VOD_DIR="${VOD_DASHBOARD_MEDIA_ROOT:-${HOME}/Documents/Twitch VODs}"
CONTAINER="${VOD_DASHBOARD_CONTAINER:-}"
MIN_FREE_GB="${VOD_DASHBOARD_MIN_FREE_GB:-40}"

if [[ ! -d "$VOD_DIR" ]]; then
    echo "Media root does not exist or is not a directory: $VOD_DIR" >&2
    exit 2
fi

if [[ ! "$MIN_FREE_GB" =~ ^[0-9]+$ ]]; then
    echo "VOD_DASHBOARD_MIN_FREE_GB must be a non-negative integer." >&2
    exit 2
fi

AVAILABLE_KB="$(df -Pk "$VOD_DIR" | awk 'NR==2 {print $4}')"
MIN_FREE_KB=$((MIN_FREE_GB * 1024 * 1024))

if (( AVAILABLE_KB < MIN_FREE_KB )); then
    AVAILABLE_GB=$((AVAILABLE_KB / 1024 / 1024))
    MESSAGE="Only ${AVAILABLE_GB} GB remain free under ${VOD_DIR}."

    if command -v logger >/dev/null 2>&1; then
        logger -t vod-disk-guard "$MESSAGE"
    else
        echo "$MESSAGE" >&2
    fi

    if [[ -n "$CONTAINER" ]]; then
        if [[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || true)" == "true" ]]; then
            docker stop "$CONTAINER"
        fi
    fi

    exit 1
fi

exit 0
