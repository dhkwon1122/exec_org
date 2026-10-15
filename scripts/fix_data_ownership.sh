#!/usr/bin/env bash
# ./data 를 컨테이너 사용자(uid/gid 10001, Dockerfile 의 app 사용자) 소유로 바꾼다.
# 바인드 마운트(./data:/app/data)는 호스트 소유권을 그대로 쓰므로, 처음 배포할 때 한 번 실행한다.
#
#   sudo scripts/fix_data_ownership.sh [데이터 디렉터리]
set -euo pipefail

DATA_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/data}"
APP_UID=10001
APP_GID=10001

mkdir -p "$DATA_DIR"

if [ "$(id -u)" -ne 0 ]; then
  echo "[fix_data_ownership] root 권한이 필요합니다 — sudo로 실행하세요:" >&2
  echo "  sudo $0 $DATA_DIR" >&2
  exit 1
fi

echo "[fix_data_ownership] 대상: $DATA_DIR (소유권을 ${APP_UID}:${APP_GID}로 변경, 다른 사용자 접근 차단)"
chown -R "${APP_UID}:${APP_GID}" "$DATA_DIR"
# 민감한 업무 기록이므로 소유자 외에는 읽을 수 없게
chmod -R u+rwX,go-rwx "$DATA_DIR"
echo "[fix_data_ownership] 완료. 컨테이너를 재시작할 필요는 없습니다(바인드 마운트라 즉시 반영됨)."
