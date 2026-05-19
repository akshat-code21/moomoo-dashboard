#!/usr/bin/env bash
#
# Install the dashboard auto-launcher into ~/Library/LaunchAgents.
# Streamlit will then start automatically every time you log in.

set -e
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$PROJECT_ROOT/scripts/com.moomoo.dashboard.plist.template"
TARGET="$HOME/Library/LaunchAgents/com.moomoo.dashboard.plist"
LABEL="com.moomoo.dashboard"

# Unload existing
if launchctl list | grep -q "$LABEL"; then
    echo "Unloading old $LABEL..."
    launchctl unload "$TARGET" 2>/dev/null || true
fi

mkdir -p "$(dirname "$TARGET")"
mkdir -p "$PROJECT_ROOT/data"

sed "s|__PROJECT_ROOT__|$PROJECT_ROOT|g" "$TEMPLATE" > "$TARGET"
echo "Wrote $TARGET"

launchctl load "$TARGET"
echo "Loaded $LABEL — dashboard will now auto-start on every login."
echo
echo "Manual control:"
echo "  Start now:   launchctl start $LABEL"
echo "  Stop:        launchctl stop  $LABEL"
echo "  Uninstall:   launchctl unload $TARGET && rm $TARGET"
echo
echo "Dashboard:     http://localhost:8501"
echo "Logs:          tail -f $PROJECT_ROOT/data/dashboard.log"
