"""로컬 사용자 계정 만들기 (USER_DATABASE_URL 이 없는 단독 실행/개발 모드 전용).

    python scripts/create_user.py <아이디> <이름> [--admin]

비밀번호는 입력 프롬프트로 받는다. --admin 이면 이 시스템 관리자 권한까지 준다.
공유 사용자 DB(People in SAIT) 모드에서는 계정을 만들 수 없다 — 그쪽에서 만들고
scripts/grant_access.py 로 접근 권한만 준다.
"""
import argparse
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import auth, users  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument('user_id')
    p.add_argument('name')
    p.add_argument('--admin', action='store_true', help='관리자 권한 부여 (없으면 일반 사용자 권한)')
    a = p.parse_args()
    pw = getpass.getpass('비밀번호(8자 이상): ')
    if pw != getpass.getpass('비밀번호 확인: '):
        sys.exit('비밀번호가 서로 다릅니다.')
    users.create_local_user(a.user_id, pw, a.name)
    auth.grant(a.user_id, 'admin' if a.admin else 'member', granted_by='scripts/create_user.py')
    print(f'{a.user_id} 계정을 만들고 {"관리자" if a.admin else "사용자"} 권한을 부여했습니다.')


if __name__ == '__main__':
    main()
