#!/usr/bin/env bash
#
# Install the daily refresh launchd job into ~/Library/LaunchAgents/.
# After install:
#   - Runs every day at 08:00 local time
#   - Logs to data/refresh.log + data/refresh.std{out,err}.log
#   - Sends a macOS notification only if reconciliation drift > threshold
#
# Re-run anytime; it tears down the old job before reinstalling.

set -e

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$PROJECT_ROOT/scripts/com.moomoo.refresh.plist.template"
TARGET_DIR="$HOME/Library/LaunchAgents"
TARGET="$TARGET_DIR/com.moomoo.refresh.plist"
LABEL="com.moomoo.refresh"

mkdir -p "$TARGET_DIR"
mkdir -p "$PROJECT_ROOT/data"

# Unload existing job (if any) so we can rewrite the plist
if launchctl list | grep -q "$LABEL"; then
    echo "Unloading existing $LABEL..."
    launchctl unload "$TARGET" 2>/dev/null || true
fi

# Render template -> real plist with project path substituted
sed "s|__PROJECT_ROOT__|$PROJECT_ROOT|g" "$TEMPLATE" > "$TARGET"
echo "Wrote $TARGET"

launchctl load "$TARGET"
echo "Loaded $LABEL"

echo
echo "Status:"
launchctl list | grep "$LABEL" || true

echo
echo "Done. The refresh will run daily at 08:00 local time."
echo "Manual test now:  launchctl start $LABEL"
echo "View logs:        tail -f $PROJECT_ROOT/data/refresh.log"
echo "Uninstall:        bash scripts/uninstall_schedule.sh"
