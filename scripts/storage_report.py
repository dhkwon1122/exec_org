"""받은 메일·첨부 저장 공간 보고 (+ 같은 첨부를 한 벌로 줄이기).

    docker compose exec app python scripts/storage_report.py          # 보고만 (아무것도 바꾸지 않음)
    docker compose exec app python scripts/storage_report.py --dedup  # 같은 첨부 파일을 한 벌로
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import storage  # noqa: E402


def mb(n: int) -> str:
    return f'{n / 1024 / 1024:,.1f} MB'


def main() -> int:
    r = storage.report()
    print(f"받은 메일 {r['mails']:,}통 · 본문 {r['mail_text']:,}자")
    print('  상태별: ' + ', '.join(f'{k} {v:,}' for k, v in sorted(r['by_status'].items(), key=lambda kv: -kv[1])))
    print(f"업무에 연결된 메일 기록 {r['mail_records']:,}건 · {r['mail_record_text']:,}자 (업무 기록으로 복사된 본문)")
    print(f"첨부 {r['attachments']:,}건 · 파일 {r['attachment_files']:,}개 · {mb(r['attachment_bytes'])}")
    print(f"  같은 내용으로 여러 벌 저장된 첨부: {r['duplicate_files']:,}개 · {mb(r['duplicate_bytes'])}")
    if '--dedup' in sys.argv:
        d = storage.dedup_attachments(apply=True)
        print(f"✓ 같은 첨부 {d['files']:,}개를 한 벌로 줄였습니다 — {mb(d['bytes'])} 확보")
    elif r['duplicate_files']:
        d = storage.dedup_attachments(apply=False)
        print(f"  --dedup 으로 실행하면 파일 {d['files']:,}개 · {mb(d['bytes'])} 를 줄일 수 있습니다 (기록은 그대로).")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
