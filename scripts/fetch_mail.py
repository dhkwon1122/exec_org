"""받은 메일을 한 번 가져와 처리한다 (POP3). 앱이 MAIL_POLL_MINUTES 마다 알아서 하지만, 연결 확인이나 cron 용.

    docker compose exec app python scripts/fetch_mail.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import mailbox  # noqa: E402


def main() -> int:
    if not mailbox.is_configured():
        print('✗ POP3 가 설정되지 않았습니다 — .env 의 MAIL_POP3_HOST, MAIL_POP3_USER, MAIL_POP3_PASSWORD')
        return 1
    st = mailbox.run_once()
    if not st['ok']:
        print(f"✗ 가져오기 실패: {st['error']}")
        return 1
    print(f"✓ 새 메일 {st['new']}통 처리, 오래돼 건너뜀 {st['skipped']}통")
    for m in mailbox.list_mails(limit=st['new'] or 1)[:st['new']]:
        print(f"  [{mailbox.STATUS.get(m['status'], m['status'])}] {m['subject']} — {m['reason']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
