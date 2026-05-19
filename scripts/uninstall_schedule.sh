#!/usr/bin/env bash
#
# Remove the scheduled refresh job.

set -e
TARGET="$HOME/Library/LaunchAgents/com.moomoo.refresh.plist"
LABEL="com.moomoo.refresh"

if launchctl list | grep -q "$LABEL"; then
    launchctl unload "$TARGET" 2>/dev/null || true
    echo "Unloaded $LABEL"
fi

if [ -f "$TARGET" ]; then
    rm "$TARGET"
    echo "Removed $TARGET"
fi

echo "Done."
