#!/bin/bash
# Video Download Service for VPS (香港)
# 负责从 YouTube 等平台下载原始视频，暂存后通过 rsync 同步到本地

set -e

# Configuration
DOWNLOAD_DIR="/tmp/video-transfer/downloads"
TEMP_STORAGE="/tmp/video-transfer/temp"
RSYNC_HOST="localhost"  # This will be tunneled via SSH
RSYNC_PORT="8730"       # Custom rsync port over SSH
MAX_CONCURRENT=2
LOG_FILE="/var/log/video-transfer/download.log"

# Ensure directories exist
mkdir -p "$DOWNLOAD_DIR" "$TEMP_STORAGE"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

download_video() {
    local url="$1"
    local video_id=$(basename "$url")
    local output_file="${DOWNLOAD_DIR}/${video_id}.mp4"
    
    log "Starting download: $url -> $output_file"
    
    # Using yt-dlp (install first: apt-get install yt-dlp)
    if command -v yt-dlp &> /dev/null; then
        yt-dlp -f "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best" \
               --output "${DOWNLOAD_DIR}/%(id)s.%(ext)s" \
               --no-warnings \
               "$url" 2>&1 | tee -a "$LOG_FILE"
        
        # Find the downloaded file
        if ls "${DOWNLOAD_DIR}/${video_id}.*" 1> /dev/null 2>&1; then
            log "✅ Download successful: ${video_id}"
            return 0
        else
            log "❌ Download failed: ${video_id}"
            return 1
        fi
    else
        log "⚠️ yt-dlp not installed, skipping..."
        return 1
    fi
}

sync_to_local() {
    log "Syncing completed videos to local machine via SSH/rsync..."
    
    # Use SSH tunnel for rsync
    ssh -L 8730:localhost:8730 -N f vps-hk &
    local ssh_pid=$!
    
    # Wait a moment for tunnel to establish
    sleep 2
    
    # Create backup folder on local machine
    LOCAL_BACKUP_BASE="/Volumes/资料盘 4T/视频搬运"
    LOCAL_TEMP="/Volumes/资料盘 4T/视频搬运/待归档"
    mkdir -p "$LOCAL_TEMP"
    
    # Sync using SSH port forwarding
    rsync -avz --delete -e "ssh -p 8730" \
          localhost:/tmp/video-transfer/completed/ \
          "$LOCAL_TEMP/" 2>&1 | tee -a "$LOG_FILE"
    
    # Kill SSH tunnel
    kill $ssh_pid 2>/dev/null || true
    
    log "✅ Sync completed"
}

cleanup_old_files() {
    log "Cleaning up files older than 24 hours..."
    
    # Delete files in TEMP_STORAGE older than 24h
    find "$TEMP_STORAGE" -type f -mtime +1 -delete 2>/dev/null || true
    find "$DOWNLOAD_DIR" -type f -name "*.partial" -delete 2>/dev/null || true
    
    log "Cleanup completed"
}

# Main execution
if [ $# -eq 0 ]; then
    echo "Usage: $0 <video_url>"
    exit 1
fi

for url in "$@"; do
    download_video "$url"
done

sync_to_local
cleanup_old_files

log "Service execution completed"
