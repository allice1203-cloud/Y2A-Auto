#!/bin/zsh
set -eu

script_dir=${0:A:h}
repo_root=${script_dir:h}
target_host=${VIDEO_TRANSFER_SYNC_HOST:-winmini}
target_path=${VIDEO_TRANSFER_SYNC_PATH:-/home/allice/.video-transfer-channel-cookies/}

for cookie_name in bilibili_source_cookies.txt douyin_cookies.txt; do
  source_file="$repo_root/cookies/$cookie_name"
  if [[ -s "$source_file" ]]; then
    /usr/bin/rsync -a "$source_file" "$target_host:$target_path"
  fi
done
