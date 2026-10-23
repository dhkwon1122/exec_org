"""LLM 호출 — OpenAI 호환 /v1/chat/completions 엔드포인트(사내 vLLM 등).

Researcher-board 와 같은 .env 를 그대로 쓸 수 있도록 LLM_* 가 없으면 LLM2_* 를 읽는다.

환경변수
  LLM_API_URL   (또는 LLM2_API_URL)  예: http://host:8000/v1/chat/completions  — 비우면 AI 기능 꺼짐
  LLM_MODEL     (또는 LLM2_MODEL)    예: thinkingcap
  LLM_API_KEY   선택. 있으면 Authorization: Bearer 로 보냄
  LLM_TIMEOUT   초, 기본 300
  LLM_MAX_TOKENS 기본 4000 (추론형 모델은 사고 과정에도 토큰을 쓰므로 넉넉히)
"""
import contextvars
import json
import os
import re
from contextlib import contextmanager
from datetime import date

import requests

MAX_INPUT_CHARS = 15000  # 원문이 아주 길면 앞부분만 보낸다(원문 자체는 DB 에 전부 보존)


class LLMError(RuntimeError):
    """LLM 미설정/호출 실패/응답 파싱 실패. 메시지는 화면에 그대로 보여준다."""


def _env(name: str, default: str = '') -> str:
    return (os.environ.get(f'LLM_{name}') or os.environ.get(f'LLM2_{name}') or default).strip()


def is_configured() -> bool:
    return bool(_env('API_URL'))


# ── 다시 분석 (피드백) ────────────────────────────────────────────────
# 결과가 틀려 다시 돌릴 때: with retry(피드백, 이전 결과): 안의 chat() 은 원래 질문 끝에 이전 결과와 피드백을 붙이고
# 온도를 올려 같은 답을 되풀이하지 않게 한다. 부르는 쪽 함수(일정·부재·업무·요약…)는 손대지 않아도 된다.

_retry = contextvars.ContextVar('llm_retry', default=None)
RETRY_TEMPERATURE = 0.6
MAX_PREVIOUS_CHARS = 3000

RETRY_NOTE = """

[다시 분석 요청]
같은 원문을 앞서 분석했는데 결과가 맞지 않아 다시 요청한다.
이전 결과:
<<<
{previous}
>>>
사용자 피드백:
<<<
{note}
>>>
피드백을 반영해 원문을 처음부터 다시 읽고 분석하라. 이전 결과에서 맞는 부분은 살리되 틀린 부분은 반드시 고친다.
피드백이 원문과 어긋나면 원문을 따른다. 답 형식은 위에서 정한 그대로."""


@contextmanager
def retry(note: str = '', previous: str = ''):
    token = _retry.set(((note or '').strip(), (previous or '').strip()))
    try:
        yield
    finally:
        _retry.reset(token)


def _with_retry(user: str, temperature: float) -> tuple[str, float]:
    r = _retry.get()
    if r is None:
        return user, temperature
    note, previous = r
    if len(previous) > MAX_PREVIOUS_CHARS:
        previous = previous[:MAX_PREVIOUS_CHARS] + '\n…(이하 생략)'
    return (user + RETRY_NOTE.format(previous=previous or '(기록 없음)',
                                     note=note or '(없음 — 이전 결과를 되풀이하지 말고 원문을 다시 꼼꼼히 읽을 것)'),
            max(temperature, RETRY_TEMPERATURE))


def chat(system: str, user: str, temperature: float = 0.2) -> str:
    user, temperature = _with_retry(user, temperature)
    url = _env('API_URL')
    if not url:
        raise LLMError('LLM 이 설정되지 않았습니다 (.env 의 LLM_API_URL).')
    headers = {'Content-Type': 'application/json'}
    key = os.environ.get('LLM_API_KEY', '').strip()
    if key:
        headers['Authorization'] = f'Bearer {key}'
    payload = {
        'model': _env('MODEL'),
        'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
        'temperature': temperature,
        'max_tokens': int(_env('MAX_TOKENS', '4000')),
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=float(_env('TIMEOUT', '300')))
    except requests.exceptions.ConnectionError as e:
        raise LLMError(f'LLM 서버에 연결할 수 없습니다 ({url}).') from e
    except requests.exceptions.Timeout as e:
        raise LLMError('LLM 응답 시간 초과.') from e
    if resp.status_code != 200:
        raise LLMError(f'LLM HTTP {resp.status_code}: {resp.text[:300]}')
    try:
        message = resp.json()['choices'][0].get('message', {})
    except (ValueError, KeyError, IndexError) as e:
        raise LLMError(f'LLM 응답 형식 오류: {resp.text[:300]}') from e
    # 추론형 모델은 content 를 비우고 reasoning_content 에만 답을 담기도 한다
    content = message.get('content') or message.get('reasoning_content') or ''
    # <think>…</think> 사고 과정은 버린다
    content = re.sub(r'<think>[\s\S]*?</think>', '', content).strip()
    if not content:
        raise LLMError('LLM 응답이 비어 있습니다 (LLM_MAX_TOKENS 를 늘려 보세요).')
    return content


def _extract_json(text: str) -> dict:
    text = re.sub(r'```(?:json)?', '', text)
    m = re.search(r'\{[\s\S]*\}', text)
    if not m:
        raise LLMError(f'LLM 응답에서 JSON 을 찾지 못했습니다: {text[:200]}')
    try:
        return json.loads(m.group(0))
    except ValueError as e:
        raise LLMError(f'LLM 응답 JSON 파싱 실패: {m.group(0)[:200]}') from e


ANALYZE_SYSTEM = """너는 업무 기록을 정리하는 비서다. 사용자가 받아 적은 원문(회의 메모, 상사·동료 피드백, 구술 받아쓰기, 메신저 대화 등)을 읽고 나중에 검색하기 좋게 정리한다.
- 원문에 없는 사실을 지어내지 않는다.
- 받아쓰기 오타·구어체는 의미를 살려 정리한다.
- 반드시 JSON 객체 하나만 출력한다. 다른 설명은 쓰지 않는다."""

ANALYZE_USER = """기록 기준일: {today} ({weekday})
업무: {task}
관련 임원: {people}
기록 유형: {entry_type}

원문:
<<<
{text}
>>>

다음 형식의 JSON 으로 답하라.
{{
  "title": "이 기록의 주제를 한 줄로 (30자 내외)",
  "summary": "핵심 내용 2~4문장 요약. 결정 사항·요청 사항·후속 조치(누가/언제까지)가 있으면 반드시 포함",
  "keywords": ["검색용 키워드 5~10개"],
  "todos": [{{"title": "해야 할 일 (동사로 끝나는 한 문장)", "due": "YYYY-MM-DD 또는 빈 문자열", "assignee": "담당자 이름 (여러 명이면 쉼표로) 또는 빈 문자열"}}]
}}
keywords 에는 (1) 사람·조직·제품·과제·문서 같은 고유명사, (2) 핵심 주제어, (3) 원문에 그 단어가 없더라도 나중에 검색할 때 쓸 법한 동의어·상위 개념을 섞어 넣는다. 각 키워드는 짧은 명사구로.
기록 유형이 '동향'이면 summary 는 "누가 / 무엇을(관심사·발언·움직임) / 우리에게 주는 시사점" 순으로 쓰고, 원문에 출처나 확인 정도(직접 들음·전언·추정)가 있으면 반드시 밝힌다. keywords 에는 관련 임원의 이름과 직위를 넣는다.
todos 에는 원문에 나온 요청·지시·약속·후속 조치만 넣는다(없으면 빈 배열). "다음주까지", "금요일까지"처럼 상대적인 기한은 기록 기준일을 기준으로 실제 날짜로 바꾸고, 기한이 없으면 due 는 빈 문자열."""


def analyze_record(text: str, task_title: str = '', entry_type: str = '', people: list[str] | None = None,
                   reference_date: date | str | None = None) -> dict:
    """원문 → {'title', 'summary', 'keywords': [..], 'todos': [{'title', 'due', 'assignee'}]}"""
    text = (text or '').strip()
    if len(text) > MAX_INPUT_CHARS:
        text = text[:MAX_INPUT_CHARS] + '\n…(이하 생략)'
    today = date.fromisoformat(reference_date[:10]) if isinstance(reference_date, str) else reference_date or date.today()
    data = _extract_json(chat(ANALYZE_SYSTEM, ANALYZE_USER.format(
        today=today.isoformat(), weekday='월화수목금토일'[today.weekday()] + '요일',
        task=task_title or '-', people=', '.join(people or []) or '-', entry_type=entry_type or '-', text=text)))
    kws = data.get('keywords') or []
    if isinstance(kws, str):
        kws = re.split(r'[,\n]', kws)
    seen, keywords = set(), []
    for k in kws:
        k = str(k).strip().lstrip('#').strip()[:40]
        if k and k.lower() not in seen:
            seen.add(k.lower())
            keywords.append(k)
    return {
        'title': str(data.get('title') or '').strip()[:200],
        'summary': str(data.get('summary') or '').strip(),
        'keywords': keywords[:12],
        'todos': _clean_todos(data.get('todos')),
    }


def _clean_todos(raw) -> list[dict]:
    out = []
    for t in raw if isinstance(raw, list) else []:
        if isinstance(t, str):
            t = {'title': t}
        if not isinstance(t, dict) or not str(t.get('title') or '').strip():
            continue
        due = str(t.get('due') or '').strip()
        out.append({'title': str(t['title']).strip()[:300],
                    'due': due if re.match(r'^\d{4}-\d{2}-\d{2}$', due) else '',
                    'assignee': str(t.get('assignee') or '').strip()[:100]})
    return out[:10]
