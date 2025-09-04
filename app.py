import os
from pathlib import Path
from typing import List

from flask import (
    Flask, request, jsonify, send_from_directory,
    redirect, url_for, render_template_string
)
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from email_sync_app import (
    Base, EmailModel, init_db, sync_emails
)

# ---------------------------
# 基本設定（/data を使う）
# ---------------------------
DB_URL = os.getenv("DATABASE_URL", "sqlite:////data/emails.db")
SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-secret")
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/data/uploads")

UPLOAD_PATH = Path(UPLOAD_DIR)
UPLOAD_PATH.mkdir(parents=True, exist_ok=True)

# ---------------------------
# DB 初期化（必要なら列追加）
# ---------------------------
init_db()  # Base.metadata.create_all(engine) を実行

engine = create_engine(DB_URL, echo=False, future=True)
Session = sessionmaker(bind=engine, expire_on_commit=False)

insp = inspect(engine)
cols = [c["name"] for c in insp.get_columns("emails")]
if "archived" not in cols:
    with engine.begin() as c:
        c.execute(text("ALTER TABLE emails ADD COLUMN archived BOOLEAN DEFAULT 0"))

# ---------------------------
# Flask 本体
# ---------------------------
app = Flask(__name__)
app.config.update(
    SECRET_KEY=SECRET_KEY,
    UPLOAD_FOLDER=str(UPLOAD_PATH),
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,  # 16MB
)

# 簡易 UI（テンプレート）
INDEX_HTML = """
<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Email CRM (Persistent /data)</title>
  <style>
    body { font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Noto Sans JP", Meiryo, sans-serif; padding: 24px; }
    h1 { margin: 0 0 12px; }
    .bar { display:flex; gap:12px; margin-bottom: 16px; }
    table { width: 100%; border-collapse: collapse; }
    th, td { padding: 8px; border-bottom: 1px solid #ddd; vertical-align: top; }
    tr.archived { opacity: .45; }
    .btn { display:inline-block; padding: 6px 10px; border: 1px solid #888; border-radius: 6px; text-decoration:none; color:#222; background:#fafafa; }
    .btn:hover { background:#f0f0f0; }
    .mono { font-family: ui-monospace, Consolas, monospace; }
    .muted { color:#666; font-size:12px; }
    pre { white-space: pre-wrap; font-size: 12px; background: #f7f7f7; padding: 8px; border-radius: 6px; }
  </style>
</head>
<body>
  <h1>Email CRM <span class="muted">(DB: /data/emails.db)</span></h1>
  <div class="bar">
    <form method="post" action="{{ url_for('sync_now') }}">
      <button class="btn" type="submit">IMAP同期を今すぐ実行</button>
    </form>
    <a class="btn" href="{{ url_for('list_emails_api') }}">JSONを表示</a>
  </div>

  <table>
    <thead>
      <tr>
        <th style="width: 60px;">ID</th>
        <th>件名 / From / To</th>
        <th style="width: 160px;">送信日時</th>
        <th style="width: 120px;">操作</th>
      </tr>
    </thead>
    <tbody>
      {% for e in emails %}
      <tr class="{{ 'archived' if e.archived else '' }}">
        <td class="mono">#{{ e.id }}</td>
        <td>
          <div><strong>{{ e.subject or '(no subject)' }}</strong></div>
          <div class="muted">From: {{ e.from_addr or '' }}</div>
          <div class="muted">To:   {{ e.to_addr or '' }}</div>
          {% if e.body %}
            <details>
              <summary>本文を表示</summary>
              <pre>{{ e.body }}</pre>
            </details>
          {% endif %}
        </td>
        <td class="mono">{{ e.sent_at or '' }}</td>
        <td>
          <form method="post" action="{{ url_for('toggle_archive', email_id=e.id) }}">
            <button class="btn" type="submit">{{ 'Unarchive' if e.archived else 'Archive' }}</button>
          </form>
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>

  {% if not emails %}
    <p>まだデータがありません。上の「IMAP同期」を押すか、IMAP_* 環境変数を設定してください。</p>
  {% endif %}
</body>
</html>
"""

# ---------------------------
# ルーティング
# ---------------------------

@app.get("/")
def index():
    session = Session()
    try:
        rows: List[EmailModel] = (
            session.query(EmailModel)
            .order_by(EmailModel.sent_at.desc().nullslast(), EmailModel.id.desc())
            .all()
        )
        return render_template_string(INDEX_HTML, emails=rows)
    finally:
        session.close()


@app.get("/uploads/<path:filename>")
def uploads(filename: str):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename, as_attachment=False)


@app.get("/emails")
def list_emails_api():
    session = Session()
    try:
        rows: List[EmailModel] = (
            session.query(EmailModel)
            .order_by(EmailModel.sent_at.desc().nullslast(), EmailModel.id.desc())
            .all()
        )
        out = []
        for e in rows:
            out.append({
                "id": e.id,
                "subject": e.subject,
                "from": e.from_addr,
                "to": e.to_addr,
                "sent_at": str(e.sent_at) if e.sent_at else None,
                "archived": e.archived,
                "body": e.body,
            })
        return jsonify(out)
    finally:
        session.close()


@app.post("/sync_now")
def sync_now():
    result = sync_emails(max_fetch=50)
    return redirect(url_for("index"))


@app.post("/archive/<int:email_id>")
def toggle_archive(email_id: int):
    session = Session()
    try:
        e = session.query(EmailModel).get(email_id)
        if e:
            e.archived = not e.archived
            session.commit()
        return redirect(url_for("index"))
    finally:
        session.close()


# ---------------------------
# メイン
# ---------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
