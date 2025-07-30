# -*- coding: utf-8 -*-
"""
IMAP → SQLite 同期ツール
  - customer_name カラムあり
  - 「* ● お名前: ◯◯」などの記号付き行も抽出可能
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
IMAP_HOST     = os.getenv('IMAP_HOST', 'imap.gmail.com')
IMAP_PORT     = int(os.getenv('IMAP_PORT', '993'))
IMAP_USER     = os.getenv('IMAP_USER')
IMAP_PASSWORD = os.getenv('IMAP_PASSWORD')
MAILBOX       = os.getenv('IMAP_MAILBOX', 'INBOX')
DB_URL        = os.getenv('DATABASE_URL', 'sqlite:///emails.db')
if not IMAP_USER or not IMAP_PASSWORD:
    print('[ERROR] 環境変数に IMAP_USER / IMAP_PASSWORD がありません', file=sys.stderr)
    # sys.exit(1)  # デプロイ時はエラー終了を回避

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
# 「● お名前: 〜」「* 顧客名: 〜」など、先頭に記号や空白があっても拾う
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
    # ① 本文優先
    m = NAME_RE.search(body or '')
    if m:
        return m.group(1).strip()

    # ② From の表示名
    name, addr = parseaddr(from_addr or '')
    if name:
        return name.strip()

    # ③ addr のローカル部
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
        status,d = imap.status(MAILBOX,'(UIDVALIDITY)')
        uv = int(d[0].decode().split()[2].rstrip(')'))
        return imap, uv
    except Exception as e:
        print(f'[ERROR] IMAP 接続失敗: {e}', file=sys.stderr)
        return None, None

def _save_uids(imap, uv, uids):
    sess, saved = Session(), 0
    for uid in uids:
        try:
            status, data = imap.uid('FETCH', str(uid), '(RFC822)')
            if not data or data[0] is None: continue
            raw = data[0][1]; msg = email.message_from_bytes(raw)
        except Exception as e:
            print(f'[WARN] UID={uid} FETCH 失敗: {e}'); continue

        subj = dec_mime(msg.get('Subject'))
        if 'クリーニング見積もり' not in subj: continue
        mid = msg.get('Message-ID')
        if sess.query(EmailModel).filter_by(message_id=mid).first(): continue

        body = extract_body(msg)
        from_addr = dec_mime(msg.get('From'))
        cname = guess_customer_name(from_addr, body)

        try:
            sess.add(EmailModel(
                uidvalidity=uv, uid=uid, message_id=mid,
                subject=subj, customer_name=cname,
                from_addr=from_addr, to_addr=dec_mime(msg.get('To')),
                date=email.utils.parsedate_to_datetime(msg.get('Date')),
                body=body, raw_content=raw.decode('utf-8','ignore')
            ))
            sess.commit(); saved += 1
        except Exception as e:
            sess.rollback(); print(f'[ERROR] DB 保存失敗: {e}', file=sys.stderr)
    sess.close()
    print(f'[INFO] 保存完了: {saved} 件')

# ---------- 外部公開 ----------
def fetch_and_save(limit=20):
    print(f'[INFO] 最新 {limit} 件取得')
    imap, uv = _connect_imap()
    if not imap: return
    status,data = imap.uid('SEARCH',None,'ALL')
    _save_uids(imap, uv, [int(u) for u in data[0].split()][-limit:][::-1])
    imap.logout()

def fetch_past_month_and_save():
    print('[INFO] 過去 1 か月分取得')
    imap, uv = _connect_imap()
    if not imap: return
    since = (datetime.now(timezone.utc)-timedelta(days=30)).strftime('%d-%b-%Y')
    status,data = imap.uid('SEARCH',None,f'(SINCE {since})')
    _save_uids(imap, uv, [int(u) for u in data[0].split()])
    imap.logout()

# ---------- CLI ----------
if __name__ == '__main__':
    p = argparse.ArgumentParser(description='メール同期')
    p.add_argument('--mode', choices=['latest','month'], default='latest')
    p.add_argument('--limit', type=int, default=20)
    a = p.parse_args()
    (fetch_past_month_and_save if a.mode=='month' else lambda: fetch_and_save(a.limit))()
