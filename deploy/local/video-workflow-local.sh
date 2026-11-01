#!/bin/bash
# Video Workflow for Local Machine (M1 Max)
set -e

VPS_SYNC_DIR="/tmp/vps-video-sync"
LOCAL_WORK_DIR="$HOME/Documents/视频编辑/视频搬运"
HDD_BACKUP_BASE="/Volumes/资料盘 4T/视频搬运"
TEMP_STORAGE="$HOME/Documents/视频搬运临时存储"

mkdir -p "$LOCAL_WORK_DIR" "$TEMP_STORAGE" "$VPS_SYNC_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$HOME/.logs/video-workflow.log"
}

check_hdd_connection() {
    if mountpoint -q "$HDD_BACKUP_BASE" 2>/dev/null; then
        echo "hdd_connected"
    else
        echo "hdd_disconnected"
    fi
}

backup_to_hdd() {
    local hdd_status=$(check_hdd_connection)
    local backup_dest=""
    
    if [ "$hdd_status" == "hdd_connected" ]; then
        backup_dest="$HDD_BACKUP_BASE/视频搬运/$(date +%Y%m%d)"
        log "✅ HDD connected - backing up to: $backup_dest"
    else
        backup_dest="$TEMP_STORAGE/archive/$(date +%Y%m%d)"
        log "⚠️ HDD not connected - using temp: $backup_dest"
        mkdir -p "$(dirname "$backup_dest")"
    fi
    
    mkdir -p "$backup_dest"
    
    if ls "$LOCAL_WORK_DIR"/*.mp4 "$LOCAL_WORK_DIR"/*.mov 1> /dev/null 2>&1; then
        mv "$LOCAL_WORK_DIR"/*.mp4 "$LOCAL_WORK_DIR"/*.mov "$backup_dest/" 2>/dev/null || true
        log "✅ Backup completed"
    else
        log "ℹ️ No videos to backup"
    fi
}

schedule_automatic_backup() {
    cat > ~/Library/LaunchAgents/com.sg99.video-auto-backup.plist << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.sg99.video-auto-backup</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>--backup</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>3</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>~/Library/Logs/video-auto-backup.log</string>
</dict>
</plist>
EOF
    launchctl load ~/Library/LaunchAgents/com.sg99.video-auto-backup.plist
    log "✅ Scheduled daily backup at 03:00 AM"
}

case "${1:-}" in
    --backup) backup_to_hdd ;;
    --schedule) schedule_automatic_backup ;;
    *) echo "Usage: $0 {--backup|--schedule}" ;;
esac
