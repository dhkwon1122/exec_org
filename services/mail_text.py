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
    """HTML → 글자. 표는 행 단위 'a | b | c', 블록 요소는 줄바꿈.

    병합된 칸(rowspan)은 아래 행에도 같은 값을 채워 넣는다 — 비서실 일정표처럼 날짜 칸이 여러 행에 걸쳐 병합돼
    있으면 HTML 의 아래 행에는 날짜 칸 자체가 없어 열이 밀리고, AI 가 그 행의 날짜를 모르게 되기 때문.
    가로 병합(colspan)은 값을 한 번만 쓰고 나머지 칸은 비운다. 표 안의 표는 바깥 칸의 글자로 넣는다."""
    BLOCK = {'p', 'div', 'br', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.tables, self.skip = [], [], 0

    def _cell(self):
        return self.tables[-1]['cell'] if self.tables else None

    @staticmethod
    def _span(attrs, name) -> int:
        try:
            return max(1, min(50, int(dict(attrs).get(name) or 1)))
        except ValueError:
            return 1

    def handle_starttag(self, tag, attrs):
        if tag in ('style', 'script', 'head'):
            self.skip += 1
        elif tag == 'table':
            self.tables.append({'rows': [], 'row': None, 'cell': None, 'span': (1, 1)})
        elif tag == 'tr' and self.tables:
            self.tables[-1]['row'] = []
        elif tag in ('td', 'th') and self.tables:
            t = self.tables[-1]
            if t['row'] is None:  # <tr> 없이 바로 칸
                t['row'] = []
            t['cell'], t['span'] = [], (self._span(attrs, 'rowspan'), self._span(attrs, 'colspan'))
        elif tag in ('br', 'p', 'div', 'li') and self._cell() is not None:
            self._cell().append('\x1f')  # 칸 안 줄바꿈 — 나중에 ' / ' 로
        elif tag in self.BLOCK:
            self.out.append('\n')

    def handle_endtag(self, tag):
        if tag in ('style', 'script', 'head'):
            self.skip = max(0, self.skip - 1)
        elif tag in ('td', 'th') and self.tables and self.tables[-1]['cell'] is not None:
            t = self.tables[-1]
            text = re.sub(r'[^\S\x1f]+', ' ', ''.join(t['cell'])).strip(' \x1f')
            text = re.sub(r'\s*\x1f[\s\x1f]*', ' / ', text)
            t['row'].append((text, *t['span']))
            t['cell'] = None
        elif tag == 'tr' and self.tables and self.tables[-1]['row'] is not None:
            t = self.tables[-1]
            t['rows'].append(t['row'])
            t['row'] = None
        elif tag == 'table' and self.tables:
            t = self.tables.pop()
            if t['row']:
                t['rows'].append(t['row'])
            lines = [' | '.join(r) for r in expand_spans(t['rows']) if any(r)]
            outer = self._cell()
            if outer is not None:
                outer.append('\x1f'.join(lines))
            else:
                self.out.append('\n' + '\n'.join(lines) + '\n')
        elif tag in self.BLOCK and self._cell() is None:
            self.out.append('\n')

    def handle_data(self, data):
        if self.skip:
            return
        cell = self._cell()
        (cell if cell is not None else self.out).append(data)


def expand_spans(rows: list[list[tuple[str, int, int]]]) -> list[list[str]]:
    """(글자, rowspan, colspan) 칸으로 된 행들 → 병합을 풀어 칸 위치가 맞는 표. 세로 병합은 아래 행에도 같은 값."""
    grid, pending = [], {}  # 열 → [글자, 남은 행 수]
    for row in rows:
        out, col = [], 0

        def fill():
            nonlocal col
            while col in pending:
                text, left = pending[col]
                out.append(text)
                if left <= 1:
                    del pending[col]
                else:
                    pending[col][1] = left - 1
                col += 1

        for text, rs, cs in row:
            fill()
            for k in range(cs):
                out.append(text if k == 0 else '')
                if rs > 1:
                    pending[col + k] = [text if k == 0 else '', rs - 1]
            col += cs
        fill()
        while pending and col <= max(pending):  # 행 끝 뒤쪽의 병합 칸
            if col in pending:
                fill()
            else:
                out.append('')
                col += 1
        grid.append(out)
    return grid


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


def _tabs_to_cells(text: str) -> str:
    """표를 복사해 붙여넣은 글(칸 = 탭) → '칸 | 칸'. 칸 안에 줄바꿈이 있으면 Outlook·엑셀은 그 칸을 큰따옴표로
    감싸 여러 줄에 걸쳐 붙여 넣으므로, 따옴표가 닫힐 때까지 이어 붙여 한 행으로 읽고 칸 안 줄바꿈은 ' / ' 로."""
    import csv
    lines, out, i = text.replace('\r\n', '\n').split('\n'), [], 0
    while i < len(lines):
        ln = lines[i]
        if '\t' not in ln:
            out.append(ln)
            i += 1
            continue
        while ln.count('"') % 2 == 1 and i + 1 < len(lines):  # 열린 따옴표 칸 — 다음 줄까지 한 행
            i += 1
            ln += '\n' + lines[i]
        i += 1
        try:
            cells = next(csv.reader(io.StringIO(ln), dialect='excel-tab'))
        except (csv.Error, StopIteration):
            cells = ln.split('\t')
        out.append(' | '.join(re.sub(r'\s*\n\s*', ' / ', c).strip() for c in cells))
    return '\n'.join(out)


def from_pasted(text: str) -> dict:
    """붙여넣은 글 — HTML 로 붙었으면 표를 살려 글자로, 아니면 그대로(표에서 복사한 탭은 ' | ' 로)."""
    text = text or ''
    if re.search(r'<(table|tr|td|div|p|br)\b', text, re.I):
        text = html_to_text(text)
    else:
        text = _tabs_to_cells(text)
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
    def header(name):
        try:
            return str(msg.get(name) or '').strip()
        except Exception:  # noqa: BLE001
            return ''
    # Date 머리가 없으면(저장 방식에 따라) 본문에 붙은 '보낸 날짜:' 줄로
    return {'name': subject, 'mail_date': _date_of(msg.get('date')) or _header_date(body), 'text': body,
            'sender': header('from'), 'message_id': header('message-id')}


def attachments(data: bytes, max_bytes: int = 30 * 1024 * 1024) -> list[tuple[str, bytes]]:
    """메일 원본의 첨부 파일 → [(파일 이름, 내용)]. 본문에 박힌 그림(서명 이미지 등)과 max_bytes 보다 큰 파일은 뺀다."""
    try:
        msg = email.message_from_bytes(data, policy=email.policy.default)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        try:
            name = part.get_filename()
        except Exception:  # noqa: BLE001 — 깨진 머리글
            name = None
        if not name:
            continue
        disp = (part.get_content_disposition() or '').lower()
        if disp == 'inline' and part.get_content_maintype() == 'image' and part.get('content-id'):
            continue  # 본문 속 그림
        payload = part.get_payload(decode=True) or b''
        if payload and len(payload) <= max_bytes:
            out.append((str(name).strip().replace('\r', '').replace('\n', ''), payload))
    return out


def from_bytes(data: bytes) -> dict:
    """메일 원본(RFC 822, POP3 로 받은 그대로) → {'name','mail_date','text','sender','message_id'}."""
    return _eml(data)


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
