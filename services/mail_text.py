"""메일(붙여넣은 글·.eml·.msg·.html·.txt)에서 제목·보낸 날짜·본문 글자를 뽑는다.

비서실 일정 메일은 표가 많아서 HTML 표는 행마다 '칸 | 칸 | 칸' 한 줄로 바꿔 AI 가 열을 알아보기 쉽게 한다.
"""
import base64
import email
import email.policy
import io
import re
from datetime import date
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser


class _HTMLText(HTMLParser):
    """HTML → 글자. 표는 행 단위 'a | b | c', 블록 요소는 줄바꿈."""
    BLOCK = {'p', 'div', 'br', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'table', 'tr', 'ul', 'ol'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.row, self.cell, self.in_cell, self.skip = [], None, [], False, 0

    def handle_starttag(self, tag, attrs):
        if tag in ('style', 'script', 'head'):
            self.skip += 1
        elif tag == 'tr':
            self.row = []
        elif tag in ('td', 'th'):
            self.in_cell, self.cell = True, []
        elif tag == 'br' and self.in_cell:
            self.cell.append(' / ')
        elif tag in self.BLOCK and not self.in_cell:
            self.out.append('\n')

    def handle_endtag(self, tag):
        if tag in ('style', 'script', 'head'):
            self.skip = max(0, self.skip - 1)
        elif tag in ('td', 'th'):
            text = re.sub(r'\s+', ' ', ''.join(self.cell)).strip(' /')
            if self.row is not None:
                self.row.append(text)
            self.in_cell = False
        elif tag == 'tr':
            if self.row and any(self.row):
                self.out.append('\n' + ' | '.join(self.row) + '\n')
            self.row = None
        elif tag in self.BLOCK and not self.in_cell:
            self.out.append('\n')

    def handle_data(self, data):
        if self.skip:
            return
        (self.cell if self.in_cell else self.out).append(data)


def html_to_text(html: str) -> str:
    p = _HTMLText()
    p.feed(html or '')
    p.close()
    text = ''.join(p.out).replace('\xa0', ' ')
    lines = [re.sub(r'[ \t]+', ' ', ln).strip() for ln in text.splitlines()]
    out, blank = [], 0
    for ln in lines:
        blank = blank + 1 if not ln else 0
        if blank <= 1:
            out.append(ln)
    return '\n'.join(out).strip()


def _date_of(v) -> str:
    try:
        return parsedate_to_datetime(v).date().isoformat() if v else ''
    except (TypeError, ValueError, IndexError):
        return ''


def _header_date(text: str) -> str:
    """붙여넣은 메일 머리의 '보낸 날짜: 2026년 10월 20일 월요일 오후 5:10' / 'Sent: …' / 'Date: …' 줄."""
    m = re.search(r'(?:보낸\s*날짜|날짜|Sent|Date)\s*:\s*(.+)', text or '')
    if not m:
        return ''
    v = m.group(1)
    k = re.search(r'(\d{4})\s*[년.\-/]\s*(\d{1,2})\s*[월.\-/]\s*(\d{1,2})', v)
    if k:
        try:
            return date(int(k.group(1)), int(k.group(2)), int(k.group(3))).isoformat()
        except ValueError:
            return ''
    return _date_of(v)


def _subject_of(text: str) -> str:
    m = re.search(r'(?:제목|Subject)\s*:\s*(.+)', text or '')
    return m.group(1).strip()[:300] if m else ''


def from_pasted(text: str) -> dict:
    """붙여넣은 글 — HTML 로 붙었으면 표를 살려 글자로, 아니면 그대로."""
    text = text or ''
    if re.search(r'<(table|tr|td|div|p|br)\b', text, re.I):
        text = html_to_text(text)
    return {'name': _subject_of(text) or '붙여넣은 일정', 'mail_date': _header_date(text), 'text': text.strip()}


# 국내 메일이 흔히 다는 EUC-KR 계열 이름 — 실제로는 확장 한글(똠·햏 등)까지 들어 있어 cp949 로 읽어야 깨지지 않는다
_KOREAN_CHARSETS = {'ks_c_5601-1987', 'ks_c_5601', 'ksc5601', 'euc-kr', 'euc_kr', 'euckr', 'ks_c_5601-1989', 'x-windows-949'}


def _part_text(part) -> str:
    raw = part.get_payload(decode=True) or b''
    charset = (part.get_content_charset() or '').lower()
    for enc in (('cp949',) if charset in _KOREAN_CHARSETS else (charset,) if charset else ()) + ('utf-8', 'cp949'):
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode('utf-8', errors='replace')


def _eml(data: bytes) -> dict:
    msg = email.message_from_bytes(data, policy=email.policy.default)
    html_part = msg.get_body(preferencelist=('html',))
    plain_part = msg.get_body(preferencelist=('plain',))
    if html_part is not None:
        body = html_to_text(_part_text(html_part))
    elif plain_part is not None:
        body = _part_text(plain_part)
    else:
        body = ''
    body = body.replace('\r\n', '\n').strip()
    try:
        subject = str(msg.get('subject') or '')
    except Exception:  # noqa: BLE001 — 깨진 머리글
        subject = ''
    # Date 머리가 없으면(저장 방식에 따라) 본문에 붙은 '보낸 날짜:' 줄로
    return {'name': subject, 'mail_date': _date_of(msg.get('date')) or _header_date(body), 'text': body}


def _msg(data: bytes) -> dict:
    """Outlook .msg (OLE) — 제목·본문(HTML 우선)·보낸 날짜(전송 헤더의 Date)."""
    import olefile
    ole = olefile.OleFileIO(io.BytesIO(data))

    def stream(code: str) -> bytes | None:
        for suffix in ('001F', '001E', '0102'):
            name = f'__substg1.0_{code}{suffix}'
            if ole.exists(name):
                return ole.openstream(name).read()
        return None

    def as_text(raw: bytes | None, code: str) -> str:
        if raw is None:
            return ''
        if ole.exists(f'__substg1.0_{code}001F'):
            return raw.decode('utf-16-le', errors='replace')
        for enc in ('utf-8', 'cp949'):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode('latin-1', errors='replace')

    try:
        subject = as_text(stream('0037'), '0037')
        html = stream('1013')
        html_text = ''
        if html is not None:
            raw = html.decode('utf-16-le', errors='replace') if ole.exists('__substg1.0_1013001F') else None
            if raw is None:
                m = re.search(rb'charset=["\']?([\w-]+)', html[:2000])
                try:
                    raw = html.decode(m.group(1).decode() if m else 'utf-8')
                except (LookupError, UnicodeDecodeError):
                    raw = html.decode('cp949', errors='replace')
            html_text = html_to_text(raw)
        body = html_text or as_text(stream('1000'), '1000')
        headers = as_text(stream('007D'), '007D')
        sent = _msg_sent_date(ole)
    finally:
        ole.close()
    m = re.search(r'^Date:\s*(.+)$', headers, re.M)
    return {'name': subject.strip('\x00 '), 'mail_date': (_date_of(m.group(1).strip()) if m else '') or sent,
            'text': body.replace('\x00', '').replace('\r\n', '\n').strip()}


def _msg_sent_date(ole) -> str:
    """속성 스트림의 보낸 시각(PR_CLIENT_SUBMIT_TIME 0x0039, 없으면 받은 시각 0x0E06) — FILETIME."""
    import struct
    from datetime import datetime, timedelta
    if not ole.exists('__properties_version1.0'):
        return ''
    raw = ole.openstream('__properties_version1.0').read()
    found = {}
    for off in range(32, len(raw) - 15, 16):  # 최상위 메시지는 머리 32바이트 뒤로 16바이트씩
        ptype, pid = struct.unpack_from('<HH', raw, off)
        if ptype == 0x0040 and pid in (0x0039, 0x0E06):
            found[pid] = struct.unpack_from('<Q', raw, off + 8)[0]
    ft = found.get(0x0039) or found.get(0x0E06)
    if not ft:
        return ''
    try:
        return (datetime(1601, 1, 1) + timedelta(microseconds=ft // 10)).date().isoformat()
    except OverflowError:
        return ''


def from_upload(contents: str, filename: str) -> dict:
    """dcc.Upload 한 파일 → {'name','mail_date','text'}. 읽지 못하면 ValueError."""
    try:
        data = base64.b64decode(contents.split(',', 1)[1])
    except (IndexError, ValueError) as e:
        raise ValueError(f'{filename}: 파일을 읽을 수 없습니다.') from e
    name = (filename or '').lower()
    if name.endswith('.eml'):
        out = _eml(data)
    elif name.endswith('.msg'):
        try:
            out = _msg(data)
        except Exception as e:  # noqa: BLE001 — 깨진 파일·다른 형식
            raise ValueError(f'{filename}: Outlook 메일(.msg)을 열 수 없습니다 ({type(e).__name__}).') from e
    elif name.endswith(('.html', '.htm', '.txt')):
        for enc in ('utf-8-sig', 'cp949'):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError(f'{filename}: 글자 인코딩을 알 수 없습니다.')
        out = from_pasted(text) if name.endswith('.txt') else {
            'name': '', 'mail_date': _header_date(html_to_text(text)), 'text': html_to_text(text)}
    else:
        raise ValueError(f'{filename}: .msg .eml .html .txt 파일만 올릴 수 있습니다.')
    out['name'] = out['name'] or filename
    if not out['text']:
        raise ValueError(f'{filename}: 본문이 비어 있습니다.')
    return out
