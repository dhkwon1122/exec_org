"""의미 검색 색인을 한꺼번에 만든다 — 처음 켤 때나 모델을 바꾼 뒤. 앱의 백그라운드 색인과 같은 일을 쉬지 않고 한다.

    docker compose exec app python scripts/build_embeddings.py            # 빠진 것·바뀐 것 모두
    docker compose exec app python scripts/build_embeddings.py --status   # 몇 개 됐는지만

.env 의 EMBED_BASE_URL(·EMBED_API_KEY)이 있어야 한다.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import embed  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--status', action='store_true', help='색인 현황만 출력')
    args = ap.parse_args()
    st = embed.status()
    print(f"색인 {st['indexed']} / {st['total']}")
    if args.status:
        return 0
    if not embed.is_configured():
        print('EMBED_BASE_URL 이 없습니다 (.env).')
        return 1
    print(f'바뀐 글 {embed.scan()}개')
    done, t0 = 0, time.time()
    try:
        while True:
            n = embed.index_pending()
            if not n:
                break
            done += n
            print(f'  {done}개 ({time.time() - t0:.0f}초)', flush=True)
    except embed.EmbedError as e:
        print(f'실패: {e}')
        return 1
    st = embed.status()
    print(f"끝 — 새로 만든 것 {done}개, 색인 {st['indexed']} / {st['total']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
