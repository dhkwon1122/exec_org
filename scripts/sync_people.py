"""People in SAIT 인사 정보에서 임원 기본 정보(이름·직위·소속)를 불러온다. 화면의 'People in SAIT에서 불러오기'와 같다.

    docker compose exec app python scripts/sync_people.py

정기 갱신이 필요하면 서버 cron 에 위 명령을 걸어 둔다 (예: 매주 월요일 아침).
임원 범위는 .env 의 EXEC_ORG_POSITIONS (기본: 사장,부사장,상무,고문).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import people_sync  # noqa: E402


def main() -> int:
    try:
        r = people_sync.sync_people()
    except people_sync.SyncError as e:
        print(f'✗ {e}')
        return 1
    print(f"People in SAIT 임원 {r['total']}명: 추가 {len(r['added'])}, 갱신 {len(r['updated'])}, "
          f"연결 {len(r['linked'])}, 변동 없음 {r['unchanged']}")
    for label, key in (('추가', 'added'), ('갱신', 'updated'), ('연결', 'linked'), ('최신 인원에 없음', 'missing')):
        if r[key]:
            print(f'  {label}: ' + ', '.join(r[key]))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
