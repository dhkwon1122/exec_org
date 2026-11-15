"""사용자·화면별 초안. 토큰으로 다른 탭의 초안을 덮어쓰지 않는다."""
import json
import uuid

from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError

from services import db
from services.db import UserDraft


def _key(user_id, context):
    if not user_id or not context or len(context) > 200:
        raise ValueError('초안을 저장할 사용자와 화면을 확인하세요.')
    return user_id, context


def get(user_id, context):
    with db.SessionLocal() as s:
        row = s.get(UserDraft, _key(user_id, context))
        return {'payload': json.loads(row.payload), 'token': row.token,
                'at': row.updated_at.strftime('%m/%d %H:%M:%S')} if row else None


def save(user_id, context, payload, expected_token=None):
    key = _key(user_id, context)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if len(raw) > 150000:
        raise ValueError('초안이 너무 깁니다. 본문을 나누어 저장하세요.')
    token = uuid.uuid4().hex
    try:
        with db.transaction() as s:
            if expected_token:
                result = s.execute(update(UserDraft).where(UserDraft.user_id == user_id,
                    UserDraft.context == context, UserDraft.token == expected_token).values(payload=raw, token=token))
                if not result.rowcount:
                    raise ValueError('다른 창에서 초안이 변경됐습니다. 불러오거나 저장된 초안을 삭제한 뒤 계속하세요.')
            else:
                s.add(UserDraft(user_id=key[0], context=key[1], payload=raw, token=token))
    except IntegrityError as error:
        raise ValueError('다른 창에 저장된 초안이 있습니다. 먼저 불러오기를 선택하세요.') from error
    return get(user_id, context)


def clear(user_id, context, expected_token):
    _key(user_id, context)
    if not expected_token:
        return
    with db.transaction() as s:
        result = s.execute(delete(UserDraft).where(UserDraft.user_id == user_id,
            UserDraft.context == context, UserDraft.token == expected_token))
        if not result.rowcount:
            raise ValueError('다른 창에서 초안이 변경됐습니다. 먼저 불러오기를 선택하세요.')
