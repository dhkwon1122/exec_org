"""메일로 만든 업무 기록·임원 동향의 시각을 '메일을 받은 시각'으로 맞춘다 (예전에는 연결한 시각·메일 날짜 09:00).

    docker compose exec app python scripts/fix_mail_record_times.py          # 바꿀 수만 보기
    docker compose exec app python scripts/fix_mail_record_times.py --apply  # 바꾸기
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import mailbox  # noqa: E402


def main() -> int:
    apply = '--apply' in sys.argv
    r = mailbox.fix_record_times(apply=apply)
    print(f"메일로 만든 기록 {r['checked']:,}건 중 시각이 다른 기록 {r['changed']:,}건"
          + (' — 메일을 받은 시각으로 바꿨습니다.' if apply else ' — --apply 로 실행하면 바꿉니다.'))
    print('  예전에 받은 메일은 정확한 시각이 저장돼 있지 않아, 가져온 시각(같은 날일 때) 또는 메일 날짜 09:00 을 씁니다.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
