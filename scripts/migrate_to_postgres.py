"""기존 SQLite 업무 기록(data/exec_org.db)을 이 앱 전용 PostgreSQL(exec-db)로 옮긴다.

    docker compose up -d                                          # exec-db 가 떠 있어야 한다
    docker compose exec app python scripts/migrate_to_postgres.py

- 원본: --source (기본 data/exec_org.db), 대상: --target (기본 EXEC_ORG_DATABASE_URL)
- 모든 테이블을 id 그대로 옮기고, PostgreSQL 의 id 시퀀스를 옮긴 값 다음부터 이어지게 맞춘다.
- 대상에 기록이 하나라도 있으면 덮어쓰지 않고 멈춘다 (두 번 실행해도 안전).
- 원본은 지우지 않는다 — 옮긴 뒤 화면에서 확인하고, 필요 없어지면 직접 치운다(백업으로 보관 권장).
- 업로드 파일(data/uploads)은 DB 밖 파일이라 옮길 필요가 없다.
"""
import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, select, text  # noqa: E402

logging.getLogger('services.db').setLevel(logging.ERROR)  # '옮기려면 이 스크립트를 실행' 안내는 여기선 불필요

from services import db  # noqa: E402

BATCH = 500
# 자기 자신(상위 업무)이나 나중에 옮길 테이블(이력)을 가리키는 칸 — 행 순서와 상관없이 옮기려고 마지막에 채운다
DEFERRED = {'tasks': ['parent_id', 'source_history_id']}


class MigrateError(RuntimeError):
    pass


def _counts(engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {t.name: conn.execute(select(func.count()).select_from(t)).scalar()
                for t in db.Base.metadata.sorted_tables}


def migrate(source_url: str, target_url: str) -> dict[str, int]:
    """원본의 모든 행을 대상으로 복사하고 테이블별 행 수를 돌려준다."""
    if source_url == target_url:
        raise MigrateError('원본과 대상이 같은 DB 입니다.')
    # make_engine 은 앱 시작과 같은 자동 마이그레이션을 거치므로, 예전 버전 원본도 지금 테이블 모양이 된다
    src = db.make_engine(source_url)
    dst = db.make_engine(target_url)
    try:
        filled = {k: n for k, n in _counts(dst).items() if n}
        if filled:
            raise MigrateError('대상 DB 에 이미 기록이 있어 멈춥니다: '
                               + ', '.join(f'{k} {n}건' for k, n in filled.items()))
        tables = db.Base.metadata.sorted_tables  # 외래키 순서 (부모 테이블 먼저)
        with src.connect() as s, dst.begin() as d:
            later = []  # (테이블, id, {칸: 값}) — 아직 안 옮긴 행을 가리킬 수 있는 외래키는 다 옮긴 뒤에 채운다
            for t in tables:
                cols = DEFERRED.get(t.name, [])
                result = s.execute(select(t).order_by(*t.primary_key.columns))
                while rows := result.mappings().fetchmany(BATCH):
                    batch = []
                    for r in rows:
                        r = dict(r)
                        if any(r.get(c) is not None for c in cols):
                            later.append((t, r['id'], {c: r[c] for c in cols}))
                        batch.append({**r, **{c: None for c in cols}})
                    d.execute(t.insert(), batch)
            for t, rid, vals in later:
                d.execute(t.update().where(t.c.id == rid).values(**vals))
            if dst.dialect.name == 'postgresql':
                for t in tables:
                    if 'id' in t.c:  # 다음 새 기록의 id 가 옮긴 id 와 겹치지 않게
                        d.execute(text(f"SELECT setval(pg_get_serial_sequence('{t.name}', 'id'), "
                                       f"COALESCE((SELECT MAX(id) FROM {t.name}), 1), "
                                       f"(SELECT MAX(id) FROM {t.name}) IS NOT NULL)"))
        before, after = _counts(src), _counts(dst)
        if before != after:
            raise MigrateError(f'행 수가 다릅니다: 원본 {before} / 대상 {after}')
        return after
    finally:
        src.dispose()
        dst.dispose()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--source', default='sqlite:///' + os.path.join(db.DATA_DIR, 'exec_org.db'))
    ap.add_argument('--target', default=os.environ.get('EXEC_ORG_DATABASE_URL', '').strip())
    args = ap.parse_args(argv)
    if not args.target:
        print('✗ 대상 DB 가 없습니다 — EXEC_ORG_DATABASE_URL 을 설정하거나 --target 으로 지정하세요.')
        return 1
    if args.source.startswith('sqlite:///') and not os.path.exists(args.source[len('sqlite:///'):]):
        print(f'✗ 원본 파일이 없습니다: {args.source}')
        return 1
    try:
        counts = migrate(args.source, args.target)
    except MigrateError as e:
        print(f'✗ {e}')
        return 1
    print('✓ 옮기기 완료 — ' + ', '.join(f'{k} {n}' for k, n in counts.items()))
    print('  원본은 그대로 두었습니다. 화면에서 기록을 확인한 뒤 백업으로 보관하세요.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
