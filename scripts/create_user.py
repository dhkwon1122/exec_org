"""로컬 사용자 계정 만들기 (USER_DATABASE_URL 이 없는 단독 실행/개발 모드 전용).

    python scripts/create_user.py <아이디> <이름> [--role executive_org]

비밀번호는 입력 프롬프트로 받는다. 기본 역할은 executive_org(임원조직 담당자)로,
이 역할이어야 로그인할 수 있다. 공유 사용자 DB(People in SAIT) 모드에서는 계정을
만들 수 없다 — 그쪽에서 만든다.
"""
import argparse
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import users  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument('user_id')
    p.add_argument('name')
    p.add_argument('--role', default='executive_org')
    a = p.parse_args()
    pw = getpass.getpass('비밀번호(8자 이상): ')
    if pw != getpass.getpass('비밀번호 확인: '):
        sys.exit('비밀번호가 서로 다릅니다.')
    users.create_local_user(a.user_id, pw, a.name, role=a.role)
    print(f'{a.user_id} 계정을 만들었습니다 (역할: {a.role}).')


if __name__ == '__main__':
    main()
