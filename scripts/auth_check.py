"""로그인이 안 될 때 어느 단계에서 막히는지 확인하는 진단 스크립트 (tea-meeting-agenda 의 auth_check 와 같은 역할).

    docker compose exec app python scripts/auth_check.py <아이디> <비밀번호>

앱과 같은 환경(.env 포함)에서 실행해야 의미가 있다. 사용자 DB 연결 → 계정 → 비밀번호 →
임시 비밀번호 → 역할(임원조직 담당자) → 세션 키 순서로 확인해 각 단계의 결과를 출력한다.
로그인 화면은 보안상 사유를 자세히 보여주지 않으므로 실제 원인 추적은 이 스크립트와
`docker compose logs app` 의 [login] 줄로 한다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, select, text  # noqa: E402
from werkzeug.security import check_password_hash  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    user_id, password = argv
    from services import auth, users  # noqa: E402  — 여기서 import 해야 .env 가 먼저 읽힌다

    url = users.user_db_url()
    if not url:
        print('△ DATABASE_URL / USER_DATABASE_URL 이 비어 있음 → 로컬 계정(data/exec_org.db) 모드')
    else:
        src = 'USER_DATABASE_URL' if os.environ.get('USER_DATABASE_URL', '').strip() else 'DATABASE_URL'
        print(f'✓ 사용자 DB: {src} 설정됨 (끝부분: ...{url[-28:]})')
        try:
            with create_engine(url, connect_args={'connect_timeout': 5} if url.startswith('postgresql') else {}
                               ).connect() as conn:
                conn.execute(text('SELECT 1'))
        except Exception as e:  # noqa: BLE001
            print(f'✗ 사용자 DB 연결 실패: {e}')
            print("  → 컨테이너 안에서는 localhost/127.0.0.1 이 컨테이너 자신이다. People in SAIT DB 가 같은 서버의 "
                  "postgres 컨테이너면 docker-compose.people-net.yml 로 네트워크를 붙이고 호스트를 db 로, "
                  "호스트에 직접 떠 있으면 host.docker.internal 로 쓴다. tea-meeting-agenda 의 .env 값과 비교해 볼 것.")
            return 1
        print('✓ 사용자 DB 연결 성공')

    try:
        engine, table = users._get()
    except users.UserDBError as e:
        print(f'✗ {e}')
        return 1
    print('✓ app_users 테이블 확인 (컬럼: ' + ', '.join(c.name for c in table.columns) + ')')

    with engine.connect() as conn:
        row = conn.execute(select(table).where(table.c.user_id == user_id)).mappings().first()
    if row is None:
        print(f"✗ user_id='{user_id}' 계정이 없습니다 (대소문자·오타 확인).")
        return 1
    print(f"✓ 계정 찾음 (이름={row.get('display_name')!r}, 역할={row.get('role')!r})")

    if not check_password_hash(row['password_hash'], password):
        print('✗ 비밀번호가 일치하지 않습니다.')
        return 1
    print('✓ 비밀번호 일치')

    if row.get('must_change_password'):
        print('✗ 임시 비밀번호 상태 → People in SAIT 에서 비밀번호를 먼저 바꿔야 합니다.')
        return 1

    allowed = sorted(auth.allowed_roles())
    if row.get('role') not in allowed:
        print(f"✗ 역할 {row.get('role')!r} 은 접속 허용 역할 {allowed} 에 없습니다.")
        print('  → People in SAIT 에서 역할을 executive_org(임원조직 담당자)로 바꾸거나, '
              '.env 의 EXEC_ORG_ROLES 에 이 역할을 추가하세요.')
        return 1
    print(f'✓ 역할 허용됨 ({allowed})')

    if os.environ.get('SESSION_SECRET') or os.environ.get('FLASK_SECRET_KEY'):
        print('✓ 세션 키: 환경변수(SESSION_SECRET/FLASK_SECRET_KEY)')
    else:
        from services.db import DATA_DIR
        path = os.path.join(DATA_DIR, '.flask_secret_key')
        ok = os.path.exists(path) and os.access(path, os.R_OK)
        print(('✓' if ok else '✗') + f' 세션 키 파일: {path}' + ('' if ok else ' 없음/읽기 불가 → '
              'sudo scripts/fix_data_ownership.sh 실행 또는 .env 에 SESSION_SECRET 지정'))
        if not ok:
            return 1
    print('✓ 로그인 조건을 모두 만족합니다. 그래도 화면에서 안 되면 브라우저 주소·쿠키 문제 — '
          'docker compose logs app 의 [login] 줄을 확인하세요.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
