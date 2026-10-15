"""사용자 DB 에 있는 계정에 이 시스템 접근 권한을 준다 (화면의 '접근 권한' 메뉴와 같은 일).

    python scripts/grant_access.py <아이디> [admin|member]
    python scripts/grant_access.py --list
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import auth  # noqa: E402


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == '--list':
        for m in auth.list_members():
            print(f"{m['user_id']:<20} {m['name']:<12} {auth.ROLES[m['role']]:<6} {m['granted_by']}")
        return
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    role = sys.argv[2] if len(sys.argv) > 2 else 'member'
    auth.grant(sys.argv[1], role, granted_by='scripts/grant_access.py')
    print(f'{sys.argv[1]} 에게 {auth.ROLES[role]} 권한을 부여했습니다.')


if __name__ == '__main__':
    main()
