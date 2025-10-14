# -*- coding: utf-8 -*-
"""
IMAP → SQLite 同期ツール（堅牢化版）
  - FETCH 応答の多様性に対応（tuple/bytes/余分要素）
  - SUBJECT フィルタを環境変数化（SUBJECT_FILTER）
  - raw_content は bytes を安全に decode（fallback あり）
"""
import sys, os, re, imaplib, email, argparse
from email.header import decode_header
from email.utils import parseaddr
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup
# from dotenv import load_dotenv
from sqlalchemy import (
    create_engine, Column, BigInteger, String,
    Text, DateTime, UniqueConstraint
)
from sqlalchemy.orm import declarative_base, sessionmaker

# ---------- stdout 対策 ----------
if sys.platform.startswith('win') and sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

# ---------- .env ----------
# load_dotenv()
IMAP_HOST      = os.getenv('IMAP_HOST', 'imap.gmail.com')
IMAP_PORT      = int(os.getenv('IMAP_PORT', '993'))
IMAP_USER      = os.getenv('IMAP_USER')
IMAP_PASSWORD  = os.getenv('IMAP_PASSWORD')
MAILBOX        = os.getenv('IMAP_MAILBOX', 'INBOX')
DB_URL         = os.getenv('DATABASE_URL', 'sqlite:///emails.db')
SUBJECT_FILTER = os.getenv('SUBJECT_FILTER', 'クリーニング見積もり')

if not IMAP_USER or not IMAP_PASSWORD:
    print('[ERROR] 環境変数に IMAP_USER / IMAP_PASSWORD がありません', file=sys.stderr)
    # 本番での強制終了は避けるが、同期関数内で未設定なら return する

# ---------- DB ----------
Base = declarative_base()

class EmailModel(Base):
    __tablename__ = 'emails'
    uidvalidity   = Column(BigInteger, primary_key=True)
    uid           = Column(BigInteger, primary_key=True)
    message_id    = Column(String(255), unique=True, nullable=False)

    subject       = Column(Text)
    customer_name = Column(Text)        # 顧客名
    from_addr     = Column(Text)
    to_addr       = Column(Text)
    date          = Column(DateTime)
    body          = Column(Text)
    raw_content   = Column(Text)

    status        = Column(String(20), default='新規')
    gpt_response  = Column(Text)
    fetched_at    = Column(DateTime, default=datetime.now(timezone.utc))

    __table_args__ = (UniqueConstraint('message_id', name='_message_id_uc'),)

engine  = create_engine(DB_URL, echo=False, future=True)
Session = sessionmaker(bind=engine)
Base.metadata.create_all(engine)

# ---------- デコード utils ----------
def dec_mime(val: str | None) -> str:
    if not val: return ''
    out = ''
    for part, enc in decode_header(val):
        out += part.decode(enc or 'utf-8', 'ignore') if isinstance(part, bytes) else part
    return out

def extract_body(msg: email.message.Message) -> str:
    payload = None
    if msg.is_multipart():
        payload = next((p for p in msg.walk() if p.get_content_type()=='text/plain'), None)
        payload = payload or next((p for p in msg.walk() if p.get_content_type()=='text/html'), None)
    else:
        payload = msg
    if payload is None: return ''
    charset = payload.get_content_charset() or 'utf-8'
    raw = payload.get_payload(decode=True) or b''
    text = (raw.decode(charset,'ignore') if payload.get_content_type()=='text/plain'
            else BeautifulSoup(raw,'html.parser').get_text('\n'))
    return re.sub(r'\s+\n', '\n', text.replace('■','●')).strip()

# ---------- 顧客名抽出 ----------
NAME_RE = re.compile(
    r'(?:^[\s\*★●＊・\-]+)?(?:顧客名|お名前|氏名)\s*[:：]\s*([^\n\r]+)',
    re.MULTILINE
)

def guess_customer_name(from_addr: str, body: str) -> str:
    """
    （優先順位）
    ① 本文の『顧客名 / お名前 / 氏名: 〜』行
    ② From: の表示名
    ③ メールアドレスのローカル部
    """
    m = NAME_RE.search(body or '')
    if m:
        return m.group(1).strip()

    name, addr = parseaddr(from_addr or '')
    if name:
        return name.strip()

    return addr.split('@')[0] if addr else ''

# ---------- IMAP 搬送・保存 ----------
def _connect_imap():
    try:
        if not IMAP_USER or not IMAP_PASSWORD:
            print('[WARN] IMAP認証情報が設定されていないため、メール同期をスキップします')
            return None, None

        imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        imap.login(IMAP_USER, IMAP_PASSWORD)
        imap.select(MAILBOX)
        status, d = imap.status(MAILBOX, '(UIDVALIDITY)')
        # d は [b'INBOX (UIDVALIDITY 123456)'] のような bytes リスト想定だが、安全にパース
        uv = None
        if d and len(d) >= 1:
            first = d[0]
            s = first.decode() if isinstance(first, (bytes, bytearray)) else str(first)
            for tok in s.replace(')', ' ').split():
                if tok.isdigit():
                    uv = int(tok); break
        if uv is None:
            raise RuntimeError(f'UIDVALIDITY 解析失敗: {d}')
        return imap, uv
    except Exception as e:
        print(f'[ERROR] IMAP 接続失敗: {e}', file=sys.stderr)
        return None, None

def _extract_raw_from_fetch(data):
    """
    imap.uid('FETCH', uid, '(RFC822)') の応答から本文 bytes を抽出
    data 例:
      [(b'93452 (RFC822 {…})', b'...raw...'), b')']
      [(b'93452 (FLAGS (\\Seen))')]  # 本文無しもあり得る
      [b'...raw...']                # 実装やサーバ差で崩れることも
    """
    if not data:
        return None
    # まず tuple 部分から (header, bytes) を探す
    for part in data:
        if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], (bytes, bytearray)):
            return part[1]
    # つぎに単独 bytes の候補を拾う（ヘッダ断片っぽいものは除外しづらいのでそのまま返す）
    for part in data:
        if isinstance(part, (bytes, bytearray)):
            return part
    return None

def _save_uids(imap, uv, uids):
    sess, saved = Session(), 0
    for uid in uids:
        try:
            status, data = imap.uid('FETCH', str(uid), '(RFC822)')
            raw = _extract_raw_from_fetch(data)
            if not isinstance(raw, (bytes, bytearray)):
                print(f'[WARN] UID={uid} FETCH 失敗: payload not found (data={type(data)})')
                continue
            msg = email.message_from_bytes(raw)
        except Exception as e:
            print(f'[WARN] UID={uid} FETCH 例外: {e}'); continue

        subj = dec_mime(msg.get('Subject'))
        if SUBJECT_FILTER and SUBJECT_FILTER not in subj:
            continue

        mid = (msg.get('Message-ID') or '').strip()
        if not mid:
            # Message-ID 欠落は稀にあるので UID/UIDVALIDITY で代替（ユニーク担保）
            mid = f'<uv{uv}.uid{uid}@local>'
        if sess.query(EmailModel).filter_by(message_id=mid).first():
            continue

        body = extract_body(msg)
        from_addr = dec_mime(msg.get('From'))
        cname = guess_customer_name(from_addr, body)

        try:
            raw_text = raw.decode('utf-8', 'ignore')  # bytes 確認済み
            sess.add(EmailModel(
                uidvalidity=uv, uid=uid, message_id=mid,
                subject=subj, customer_name=cname,
                from_addr=from_addr, to_addr=dec_mime(msg.get('To')),
                date=email.utils.parsedate_to_datetime(msg.get('Date')),
                body=body, raw_content=raw_text
            ))
            sess.commit(); saved += 1
        except Exception as e:
            sess.rollback(); print(f'[ERROR] DB 保存失敗 (UID={uid}): {e}', file=sys.stderr)
    sess.close()
    print(f'[INFO] 保存完了: {saved} 件')

# ---------- 外部公開 ----------
def fetch_and_save(limit=20):
    print(f'[INFO] 最新 {limit} 件取得')
    imap, uv = _connect_imap()
    if not imap: return
    try:
        status, data = imap.uid('SEARCH', None, 'ALL')
        uids = [int(u) for u in (data[0] or b'').split()]
        _save_uids(imap, uv, uids[-limit:][::-1])
    finally:
        imap.logout()

def fetch_past_month_and_save():
    print('[INFO] 過去 1 か月分取得')
    imap, uv = _connect_imap()
    if not imap: return
    try:
        since = (datetime.now(timezone.utc)-timedelta(days=30)).strftime('%d-%b-%Y')
        status, data = imap.uid('SEARCH', None, f'(SINCE {since})')
        uids = [int(u) for u in (data[0] or b'').split()]
        _save_uids(imap, uv, uids)
    finally:
        imap.logout()

# ---------- CLI ----------
if __name__ == '__main__':
    p = argparse.ArgumentParser(description='メール同期')
    p.add_argument('--mode', choices=['latest','month'], default='latest')
    p.add_argument('--limit', type=int, default=20)
    a = p.parse_args()
    (fetch_past_month_and_save if a.mode=='month' else lambda: fetch_and_save(a.limit))()
