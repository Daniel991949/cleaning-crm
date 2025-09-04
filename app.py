import os
from pathlib import Path
from typing import List

from flask import (
    Flask, request, jsonify, send_from_directory,
    redirect, url_for, render_template_string
)

# DBアクセスと同期関数は email_sync_app.py に合わせる
from email_sync_app import (
    Session, Base, EmailModel, fetch_and_save
)

# ---------------------------------
# 基本設定（/data を使う：Railway Volume）
# ---------------------------------
SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-secret")
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/data/uploads")
UPLOAD_PATH = Path(UPLOAD_DIR)
UPLOAD_PATH.mkdir(parents=True, exist_ok=True)

# ---------------------------------
# Flask 本体
# ---------------------------------
app = Flask(__name__)
app.config.update(
    SECRET_KEY=SECRET_KEY,
    UPLOAD_FOLDER=str(UPLOAD_PATH),
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,  # 16MB
)

# ---------------------------------
# UI テンプレート（アーカイブ機能なし）
# ---------------------------------
INDEX_HTML = """
<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Email CRM (/data persistent)</title>
  <style>
    body { font-family: system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,"Noto Sans JP",Meiryo,sans-serif; padding:24px; }
    h1 { margin:0 0 12px; }
    .bar { display:flex; gap:12px; margin-bottom:16px; }
    table{ width:100%; border-collapse:collapse; }
    th,td{ padding:8px; border-bottom:1px solid #ddd; vertical-align:top; }
    .btn{ display:inline-block; padding:6px 10px; border:1px solid #888; border-radius:6px; text-decoration:none; color:#222; background:#fafafa; }
    .btn:hover{ background:#f0f0f0; }
    .mono{ font-family: ui-monospace,Consolas,monospace; }
    .muted{ color:#666; font-size:12px; }
    pre{ white-space:pre-wrap; font-size:12px; background:#f7f7f7; padding:8px; border-radius:6px; }
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
        <th style="width:70px;">ID</th>
        <th>件名 / 顧客名 / From / To</th>
        <th style="width:180px;">送信日時</th>
      </tr>
    </thead>
    <tbody>
      {% for e in emails %}
      <tr>
        <td class="mono">#{{ e.id if hasattr(e,'id') else (str(e.uidvalidity)+'-'+str(e.uid)) }}</td>
        <td>
          <div><strong>{{ e.subject or '(no subject)' }}</strong></div>
          {% if e.customer_name %}
            <div class="muted">顧客名: {{ e.customer_name }}</div>
          {% endif %}
          <div class="muted">From: {{ e.from_addr or '' }}</div>
          <div class="muted">To:   {{ e.to_addr or '' }}</div>
          {% if e.body %}
            <details>
              <summary>本文を表示</summary>
              <pre>{{ e.body }}</pre>
            </details>
          {% endif %}
        </td>
        <td class="mono">{{ e.date or '' }}</td>
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

# ---------------------------------
# ルーティング
# ---------------------------------

@app.get("/")
def index():
    session = Session()
    try:
        # date の降順、次いで PK 降順（id が無いスキーマでも落ちないように配慮）
        q = session.query(EmailModel)
        rows: List[EmailModel] = q.order_by(
            getattr(EmailModel, "date", None).desc().nullslast()
            if hasattr(EmailModel, "date") else
            getattr(EmailModel, "fetched_at").desc().nullslast()
        ).all()
        return render_template_string(INDEX_HTML, emails=rows)
    finally:
        session.close()


@app.get("/uploads/<path:filename>")
def uploads(filename: str):
    # いまは未使用だが将来のファイル配信用に残す
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename, as_attachment=False)


@app.get("/emails")
def list_emails_api():
    session = Session()
    try:
        rows: List[EmailModel] = session.query(EmailModel).all()
        out = []
        for e in rows:
            out.append({
                "uidvalidity": getattr(e, "uidvalidity", None),
                "uid": getattr(e, "uid", None),
                "message_id": getattr(e, "message_id", None),
                "subject": getattr(e, "subject", None),
                "customer_name": getattr(e, "customer_name", None),
                "from": getattr(e, "from_addr", None),
                "to": getattr(e, "to_addr", None),
                "date": str(getattr(e, "date", None)) if getattr(e, "date", None) else None,
                "status": getattr(e, "status", None),
                "fetched_at": str(getattr(e, "fetched_at", None)) if getattr(e, "fetched_at", None) else None,
                "body": getattr(e, "body", None),
            })
        return jsonify(out)
    finally:
        session.close()


@app.post("/sync_now")
def sync_now():
    # email_sync_app.py の実装に合わせて最新N件を取得
    fetch_and_save(limit=50)
    return redirect(url_for("index"))


# ---------------------------------
# メイン
# ---------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
