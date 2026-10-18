"""People in SAIT 인사 정보와 이 앱 임원 목록을 비교해 바꿀 것을 보여준다. 화면의 'People in SAIT에서 불러오기'와 같다.

    docker compose exec app python scripts/sync_people.py            # 제안만 출력 (DB 변경 없음)
    docker compose exec app python scripts/sync_people.py --apply    # 제안을 모두 반영 (지난번에 유지한 덮어쓰기는 제외)
                                                                     # + 연결된 임원의 경력 이력 누적

골라서 반영하려면 화면을 쓴다. 정기 점검은 서버 cron 에 첫 번째 명령을 걸어 두고 출력을 확인한다.
임원 범위는 .env 의 EXEC_ORG_POSITIONS (기본: 사장,부사장,상무,고문,Master,Fellow).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import people_sync  # noqa: E402

KINDS = {'add': '추가', 'past': '과거 임원', 'link': '연결', 'set': '변경'}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='People in SAIT 임원 정보 비교·반영')
    ap.add_argument('--apply', action='store_true', help='제안을 모두 반영')
    args = ap.parse_args(argv)
    try:
        plan = people_sync.plan_sync()
    except people_sync.SyncError as e:
        print(f'✗ {e}')
        return 1
    print(f"People in SAIT 현재 임원 {plan['total']}명·과거 임원 {plan['past_total']}명: 제안 {len(plan['items'])}건, "
          f"변동 없음 {plan['unchanged']}명, 새 경력 이력 {plan['career_new']}건")
    for it in plan['items']:
        print(f"  [{KINDS[it['kind']]}] {it['label']}")
    if plan['missing']:
        print('  최신 인원에 없음: ' + ', '.join(plan['missing']))
    if not args.apply:
        if plan['items'] or plan['career_new']:
            print('반영하려면 화면의 "People in SAIT에서 불러오기" 또는 --apply')
        return 0
    r = people_sync.apply_sync(plan, people_sync.default_keys(plan))
    print(f"반영: 추가 {len(r['added'])}, 과거 임원 {len(r['past_added'])}, 연결 {len(r['linked'])}, "
          f"갱신 {len(r['updated'])}, 유지 {len(r['kept'])}, 경력 이력 {r['career_added']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
