#!/usr/bin/env bash
# 이 앱 전용 PostgreSQL(exec-db)을 pg_dump 로 백업한다. 업로드 파일(./data)은 따로 백업한다.
#
#   scripts/backup_db.sh [저장 디렉터리]        # 기본 ./backups — cron 에 걸어 두면 좋다
#
# 복원 (빈 DB 에):
#   docker compose exec -T exec-db pg_restore -U exec_org -d exec_org --clean --if-exists < backups/<파일>.dump
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${1:-$ROOT/backups}"
KEEP_DAYS="${KEEP_DAYS:-30}"

mkdir -p "$OUT_DIR"
chmod 700 "$OUT_DIR"   # 민감한 업무 기록
out="$OUT_DIR/exec_org_$(date +%Y%m%d_%H%M%S).dump"

cd "$ROOT"
docker compose exec -T exec-db pg_dump -U exec_org -d exec_org -Fc > "$out.tmp"
mv "$out.tmp" "$out"
chmod 600 "$out"
echo "[backup_db] $out ($(du -h "$out" | cut -f1))"

# 오래된 백업 정리
find "$OUT_DIR" -name 'exec_org_*.dump' -mtime +"$KEEP_DAYS" -delete
