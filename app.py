# -*- coding: utf-8 -*-
"""
Flask UI + API  – 手動登録＋写真＋ステータス更新（Detached 回避）
"""
import os, logging, time
from datetime import datetime as dt, timezone
from pathlib import Path
# from dotenv import load_dotenv
from flask import (
    Flask, render_template, request, url_for, send_from_directory,
    jsonify, abort, Response
)
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import (
    create_engine, Column, Integer, BigInteger, String,
    DateTime, Text, Boolean, inspect
)
from sqlalchemy.orm import sessionmaker
from werkzeug.utils import secure_filename
import requests

# ── メール同期ユーティリティ ─────────────────────────
import email_sync_app
from email_sync_app import Base, EmailModel, fetch_past_month_and_save, fetch_and_save

# ── Note / Photo テーブル ────────────────────────────
class NoteModel(Base):
    __tablename__ = 'notes'
    id          = Column(Integer, primary_key=True)
    uidvalidity = Column(BigInteger, nullable=False)
    uid         = Column(BigInteger, nullable=False)
    page        = Column(Integer,  nullable=False)
    content     = Column(Text,     default='')
    uploaded_at = Column(DateTime, default=lambda: dt.now(timezone.utc))

class PhotoModel(Base):
    __tablename__ = 'photos'
    id          = Column(Integer, primary_key=True)
    uidvalidity = Column(BigInteger, nullable=False)
    uid         = Column(BigInteger, nullable=False)
    filename    = Column(String(255), nullable=False)
    uploaded_at = Column(DateTime, default=lambda: dt.now(timezone.utc))

# ── EmailModel に archived 列（初回のみ自動追加） ──────
if not hasattr(EmailModel, 'archived'):
    EmailModel.archived = Column(Boolean, default=False)

engine_tmp = create_engine("sqlite:///emails.db", future=True)
if 'archived' not in [c['name'] for c in inspect(engine_tmp).get_columns('emails')]:
    with engine_tmp.begin() as c:
        c.exec_driver_sql("ALTER TABLE emails ADD COLUMN archived INTEGER DEFAULT 0;")

# ── Flask / DB ─────────────────────────────────────────
# load_dotenv()  # コメントアウト
DB_URL  = os.getenv('DATABASE_URL', 'sqlite:///emails.db')
SECRET  = os.getenv('FLASK_SECRET_KEY', 'dev')
UPLOAD  = Path(__file__).with_name('uploads'); UPLOAD.mkdir(exist_ok=True)

app = Flask(__name__)
app.config.update(SECRET_KEY=SECRET,
                  UPLOAD_FOLDER=str(UPLOAD),
                  MAX_CONTENT_LENGTH=16*1024*1024)

# expire_on_commit=False で Detached を防止
engine  = create_engine(DB_URL, echo=False, future=True)
Session = sessionmaker(bind=engine, expire_on_commit=False)
Base.metadata.create_all(engine)

# ── APScheduler ───────────────────────────────────────
def sync_last_month():
    app.logger.info('▶ Initial 30-day fetch'); fetch_past_month_and_save()

def sync_latest(limit=50):
    app.logger.info('▶ Periodic fetch'); fetch_and_save(limit=limit)

sched = BackgroundScheduler(timezone='Asia/Tokyo')
sched.add_job(sync_last_month, id='initial_once',
              next_run_time=dt.now(timezone.utc))
sched.add_job(sync_latest, 'interval', minutes=15,
              id='loop', kwargs={'limit':50})
sched.start(); app.logger.info('Scheduler started')

# ── ルーティング ────────────────────────────────────
@app.route('/')
def index():
    with Session() as s:
        emails = s.query(EmailModel).order_by(EmailModel.date.desc()).all()
    return render_template('emails.html', emails=emails)


# ---------- 手動登録（画像複数 OK） ----------
@app.route('/manual_add', methods=['POST'])
def manual_add():
    name = request.form.get('name','').strip()
    memo = request.form.get('memo','').strip()
    if not name:
        return jsonify({'ok':False,'error':'名前は必須'}), 400

    uid  = int(time.time()*1000)
    with Session() as s:
        rec = EmailModel(
            uidvalidity=0, uid=uid, message_id=f'manual-{uid}',
            subject='手動登録', customer_name=name, body=memo,
            date=dt.now(timezone.utc), status='手動入力', archived=False)
        s.add(rec); s.commit()

        for i, f in enumerate(request.files.getlist('photos')):
            if not f or not f.filename: continue
            ext = Path(f.filename).suffix.lower()
            if ext not in {'.jpg','.jpeg','.png','.gif','.webp'}: continue
            fname = secure_filename(f"0_{uid}_{int(time.time())}_{i}{ext}")
            f.save(UPLOAD/fname)
            s.add(PhotoModel(uidvalidity=0, uid=uid, filename=fname))
        s.commit()
    return jsonify({'ok':True})


# ---------- 手動同期 ----------
@app.route('/sync_now', methods=['POST'])
def sync_now():
    try: sync_latest(limit=10); return jsonify({'ok':True})
    except Exception as e:
        app.logger.exception('sync error'); return jsonify({'ok':False,'error':str(e)}),500


# ---------- 詳細 ----------
@app.route('/email/<int:uv>/<int:uid>')
def email_detail(uv, uid):
    with Session() as s:
        m = s.query(EmailModel).filter_by(uidvalidity=uv, uid=uid).first()
        if not m: abort(404)
        notes  = s.query(NoteModel ).filter_by(uidvalidity=uv, uid=uid).all()
        photos = s.query(PhotoModel).filter_by(uidvalidity=uv, uid=uid).all()
        return jsonify({
            'uidvalidity':m.uidvalidity,'uid':m.uid,'subject':m.subject or '',
            'customer_name':m.customer_name or '',
            'from_addr':m.from_addr or '','date':m.date.isoformat() if m.date else '',
            'body':m.body or '','status':m.status or '未対応','archived':bool(m.archived),
            'notes':{n.page:n.content for n in notes},
            'photos':[url_for('uploaded_file',filename=p.filename) for p in photos]
        })


# ---------- NEW: ステータス更新 ----------
@app.route('/emails/<int:uv>/<int:uid>/update_status', methods=['POST'])
def update_status(uv, uid):
    new_st = request.form.get('status','').strip()
    with Session() as s:
        m = s.query(EmailModel).filter_by(uidvalidity=uv, uid=uid).first()
        if not m: abort(404)
        if new_st: m.status = new_st
        s.commit()
    return '', 204


# ---------- アーカイブ ----------
@app.route('/emails/<int:uv>/<int:uid>/toggle_archive', methods=['POST'])
def toggle_archive(uv, uid):
    with Session() as s:
        m = s.query(EmailModel).filter_by(uidvalidity=uv, uid=uid).first()
        if not m: abort(404)
        m.archived = not m.archived; s.commit()
        return jsonify({'archived':m.archived})


# ---------- メモ保存 ----------
@app.route('/emails/<int:uv>/<int:uid>/save_note', methods=['POST'])
def save_note(uv, uid):
    page=int(request.form.get('page',1)); content=request.form.get('content','')
    with Session() as s:
        note = s.query(NoteModel).filter_by(uidvalidity=uv,uid=uid,page=page).first()
        if not note: note = NoteModel(uidvalidity=uv,uid=uid,page=page)
        note.content = content; s.add(note); s.commit()
    return '',204


# ---------- 写真追加 ----------
@app.route('/emails/<int:uv>/<int:uid>/upload_photo', methods=['POST'])
def upload_photo(uv, uid):
    if 'photo' not in request.files or not request.files['photo'].filename: abort(400,'no file')
    f=request.files['photo']; ext=Path(f.filename).suffix.lower()
    if ext not in {'.jpg','.jpeg','.png','.gif','.webp'}: abort(400,'ext')
    fname=secure_filename(f"{uv}_{uid}_{int(time.time())}{ext}"); f.save(UPLOAD/fname)
    with Session() as s:
        s.add(PhotoModel(uidvalidity=uv,uid=uid,filename=fname)); s.commit()
    return '',204


# ---------- 静的 ----------
@app.route('/uploads/<path:filename>')
def uploaded_file(filename): return send_from_directory(UPLOAD, filename)

@app.route('/proxy')
def proxy():
    url=request.args.get('url','')
    if not url.startswith(('http://','https://')): abort(400)
    try:r=requests.get(url,timeout=10)
    except requests.exceptions.RequestException: abort(502)
    return Response(r.content,status=r.status_code,
                    content_type=r.headers.get('Content-Type','application/octet-stream'),
                    headers={'Cache-Control':'public, max-age=86400'})

if __name__=='__main__':
    logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
