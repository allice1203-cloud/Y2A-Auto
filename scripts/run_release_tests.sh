#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$PROJECT_ROOT/docker-compose.hk.yml"

if [[ ! -f "$COMPOSE_FILE" ]]; then
    echo "缺少香港 VPS compose 文件：$COMPOSE_FILE" >&2
    exit 1
fi

DEFAULT_IMAGE="$(awk '$1 == "image:" {print $2; exit}' "$COMPOSE_FILE")"
TEST_IMAGE="${1:-$DEFAULT_IMAGE}"
if [[ -z "$TEST_IMAGE" ]]; then
    echo "无法从 compose 文件识别测试镜像" >&2
    exit 1
fi

HOST_UID="$(id -u)"
HOST_GID="$(id -g)"

TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/video-transfer-release-tests.XXXXXX")"
cleanup() {
    if [[ -n "${TEST_ROOT:-}" && -d "$TEST_ROOT" ]]; then
        find "$TEST_ROOT" -depth -delete
    fi
}
trap cleanup EXIT INT TERM

# 只复制代码和测试；生产凭证、数据库、Cookie、媒体和日志不会进入测试容器。
rsync -a \
    --exclude='.git/' \
    --exclude='.git 2/' \
    --exclude='.pytest_cache/' \
    --exclude='config/' \
    --exclude='db/' \
    --exclude='cookies/' \
    --exclude='downloads/' \
    --exclude='logs/' \
    --exclude='temp/' \
    --exclude='backups/' \
    --exclude='modules 2/' \
    --exclude='static 2/' \
    "$PROJECT_ROOT/" "$TEST_ROOT/"

mkdir -p "$TEST_ROOT/config" "$TEST_ROOT/db" "$TEST_ROOT/logs" "$TEST_ROOT/temp"

docker run --rm \
    --user root \
    --entrypoint sh \
    --volume "$TEST_ROOT:/workspace" \
    --workdir /workspace \
    --env PIP_DISABLE_PIP_VERSION_CHECK=1 \
    --env PIP_ROOT_USER_ACTION=ignore \
    --env TEST_HOST_UID="$HOST_UID" \
    --env TEST_HOST_GID="$HOST_GID" \
    "$TEST_IMAGE" \
    -c 'status=0; pip install --no-cache-dir --quiet pytest==8.3.5 || status=$?; if [ "$status" -eq 0 ]; then python -m pytest -q -o cache_dir=/tmp/pytest_cache tests || status=$?; fi; chown -R "$TEST_HOST_UID:$TEST_HOST_GID" /workspace; exit "$status"'
