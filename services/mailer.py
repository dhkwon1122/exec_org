"""사내 메일 발송 REST API 연동 — People in SAIT(Researcher-board) pipeline/mailer.py 와 같은 API·같은 .env 설정.

POST {MAIL_API_URL}/mails/send?userId={MAIL_USER_ID} (Bearer 토큰, System-ID 머리). 첨부가 있으면 멀티파트로
'mail'(JSON 문자열) + 'attachments'(파일마다 하나) 필드. MAIL_API_URL/MAIL_TOKEN/MAIL_SYSTEM_ID/MAIL_FROM 필수,
MAIL_USER_ID(기본 people.sait), MAIL_TIMEOUT(초, 기본 30), MAIL_VERIFY_TLS / MAIL_CA_BUNDLE.
사내망 전용 엔드포인트라 프록시를 거치지 않는다.
"""
import json
import os
from urllib.parse import quote

import requests

_DEFAULT_USER_ID = 'people.sait'


class MailError(RuntimeError):
    """메일 API 미설정 또는 발송 실패."""


def _env(name: str, default: str = '') -> str:
    return os.environ.get(name, default).strip()


def is_configured() -> bool:
    return all(_env(k) for k in ('MAIL_API_URL', 'MAIL_TOKEN', 'MAIL_SYSTEM_ID', 'MAIL_FROM'))


def sender() -> str:
    return _env('MAIL_FROM')


def send_html_email(to: list[str], subject: str, html_body: str,
                    attachments: list[tuple[str, bytes]] | None = None, cc: list[str] | None = None) -> None:
    """to(받는 사람 주소 목록)에게 HTML 본문으로 보낸다. 설정이 없거나 실패하면 MailError."""
    if not is_configured():
        raise MailError('MAIL_API_URL/MAIL_TOKEN/MAIL_SYSTEM_ID/MAIL_FROM 이 설정되지 않았습니다 — '
                        '.env 에 사내 메일 API 정보를 넣으세요(.env.example 의 메일 보내기 참고).')
    recipients = [a.strip() for a in to if a.strip()]
    if not recipients:
        raise MailError('받는 사람이 없습니다.')
    ccs = [a.strip() for a in cc or [] if a.strip() and a.strip() not in recipients]
    token, system_id = _env('MAIL_TOKEN'), _env('MAIL_SYSTEM_ID')
    headers = {'Authorization': f'Bearer {token}', 'System-ID': system_id,
               'Content-Type': 'application/json;charset=utf-8'}
    payload = {
        'subject': subject,
        'contents': html_body,
        'contentType': 'html',
        'docSecuType': 'PERSONAL',
        'sender': {'emailAddress': sender()},
        'recipients': [{'emailAddress': a, 'recipientType': 'TO'} for a in recipients]
                      + [{'emailAddress': a, 'recipientType': 'CC'} for a in ccs],
    }
    # 이 API 는 params= 쿼리를 받지 않아 ?userId= 를 URL 에 직접 붙인다 (People in SAIT 와 같음)
    user_id = _env('MAIL_USER_ID') or _DEFAULT_USER_ID
    url = f"{_env('MAIL_API_URL').rstrip('/')}/mails/send?userId={quote(user_id, safe='')}"
    timeout = float(_env('MAIL_TIMEOUT', '30') or '30')
    ca_bundle = _env('MAIL_CA_BUNDLE')
    verify = ca_bundle or _env('MAIL_VERIFY_TLS', 'true').lower() in ('1', 'true', 'yes', 'on')
    proxies = {'http': None, 'https': None}
    try:
        if attachments:  # 멀티파트 — Content-Type(boundary)은 requests 가 정한다
            resp = requests.post(url, headers={'Authorization': headers['Authorization'], 'System-ID': system_id},
                                 data=[('mail', (None, json.dumps(payload)))],
                                 files=[('attachments', (name, content)) for name, content in attachments],
                                 proxies=proxies, verify=verify, timeout=timeout)
        else:
            resp = requests.post(url, headers=headers, data=json.dumps(payload), proxies=proxies, verify=verify,
                                 timeout=timeout)
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        raise MailError(f'메일 발송 실패: {exc}') from exc
