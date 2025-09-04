import os
from pathlib import Path
from typing import List
from pathlib import Path
from flask import (
    Flask, request, jsonify, send_from_directory,
    redirect, url_for, render_template
)

from sqlalchemy import create_engine, inspect, text, Boolean, Column
from sqlalchemy.orm import sessionmaker

# あなたの元コードに合わせて、モデルや同期関数は email_sync_app から読み込み
# （sync 関数は実装違いに備えて両対応：sync_emails / fetch_and_save）
from email_sync_app import Base, EmailModel  # 必須
try:
    from email_sync_app import sync_emails as _sync_func  # 旧来の名前
except Exception:
    _sync_func = None
try:
    from email_sync_app import fetch_and_save as _fetch_and_save  # 現行の名前
except Exception:
    _fetch_and_save = None


# =========================
# 基本設定（ここだけ変更）
# =========================
DB_URL = os.getenv("DATABASE_URL", "sqlite:////data/emails.db")   # ← 変更点1
SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-secret")
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/data/uploads")             # ← 変更点2

UPLOAD_PATH = Path(UPLOAD_DIR)
UPLOAD_PATH.mkdir(parents=True, exist_ok=True)
TEMPLATE_DIR = str(Path(__file__).parent)
# =========================
# DB 初期化
# =========================
engine = create_engine(DB_URL, echo=False, future=True)
Session = sessionmaker(bind=engine, expire_on_commit=False)

# テーブルが無ければ作成
Base.metadata.create_all(engine)

# 「archived」列の後方互換（今使っている DB に対してだけ実行） ← 変更点3
try:
    # モデルに属性が無い場合は生やしておく（既存UI互換のための保険）
    if not hasattr(EmailModel, "archived"):
        setattr(EmailModel, "archived", Column(Boolean, default=False))

    insp = inspect(engine)
    cols = [c["name"] for c in insp.get_columns("emails")]
    if "archived" not in cols:
        with engine.begin() as c:
            # 元の実装互換：INTEGER 0/1 として追加（SQLite/Postgres双方で動く）
            c.execute(text("ALTER TABLE emails ADD COLUMN archived BOOLEAN DEFAULT 0"))
except Exception:
    # 既にある・権限なし等は無視してアプリは起動継続
    pass


# =========================
# Flask 本体
# =========================
# emails.html をプロジェクト直下に置いている想定なので template_folder='.' にする
app = Flask(__name__, template_folder=TEMPLATE_DIR)
app.config.update(
    SECRET_KEY=SECRET_KEY,
    UPLOAD_FOLDER=str(UPLOAD_PATH),
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,
)


# -------------------------
# ルーティング
# -------------------------
@app.get("/")
def index():
    """一覧画面（元の emails.html をそのまま使う）"""
    session = Session()
    try:
        # 並び順は元コードに依存するので、シンプルに全件取得してテンプレ側に任せる
        rows: List[EmailModel] = session.query(EmailModel).all()
        return render_template("emails.html", emails=rows)
    finally:
        session.close()


@app.get("/emails")
def list_emails_api():
    """JSON API（元の形を維持）"""
    session = Session()
    try:
        rows: List[EmailModel] = session.query(EmailModel).all()
        out = []
        for e in rows:
            # 既存スキーマの差異に耐えるよう hasattr で詰め替え
            out.append({
                "id": getattr(e, "id", None),
                "uidvalidity": getattr(e, "uidvalidity", None),
                "uid": getattr(e, "uid", None),
                "message_id": getattr(e, "message_id", None),
                "subject": getattr(e, "subject", None),
                "customer_name": getattr(e, "customer_name", None),
                "from": getattr(e, "from_addr", None),
                "to": getattr(e, "to_addr", None),
                "date": str(getattr(e, "date", None)) if getattr(e, "date", None) else None,
                "sent_at": str(getattr(e, "sent_at", None)) if getattr(e, "sent_at", None) else None,
                "status": getattr(e, "status", None),
                "archived": getattr(e, "archived", False),
                "body": getattr(e, "body", None),
            })
        return jsonify(out)
    finally:
        session.close()


@app.get("/uploads/<path:filename>")
def uploads(filename: str):
    """アップロード配信（元のまま）"""
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename, as_attachment=False)


@app.post("/sync_now")
def sync_now():
    """同期エンドポイント（元の関数名に合わせて呼び分け）"""
    if _sync_func is not None:
        # 旧来：sync_emails(max_fetch=50) など
        try:
            _sync_func(50)
        except TypeError:
            # 引数名違いに備え、名前付きでも試す
            _sync_func(max_fetch=50)
    elif _fetch_and_save is not None:
        # 現行：fetch_and_save(limit=50)
        try:
            _fetch_and_save(50)
        except TypeError:
            _fetch_and_save(limit=50)
    # いずれの場合も終わったら一覧へ
    return redirect(url_for("index"))


@app.post("/archive/<int:email_id>")
def toggle_archive(email_id: int):
    """アーカイブ切替（元のUI互換）"""
    session = Session()
    try:
        # id が無いスキーマもあるため、防御的に取得
        e = session.query(EmailModel).filter(getattr(EmailModel, "id", None) == email_id).first() if hasattr(EmailModel, "id") else None
        if e is not None and hasattr(e, "archived"):
            e.archived = not bool(getattr(e, "archived", False))
            session.commit()
        return redirect(url_for("index"))
    finally:
        session.close()


# -------------------------
# メイン
# -------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
