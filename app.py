import os
from pathlib import Path
from typing import List

from flask import (
    Flask, request, jsonify, send_from_directory,
    redirect, url_for, render_template_string
)

from email_sync_app import (
    Session, EmailModel, fetch_and_save
)

SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-secret")
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/data/uploads")
UPLOAD_PATH = Path(UPLOAD_DIR)
UPLOAD_PATH.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config.update(
    SECRET_KEY=SECRET_KEY,
    UPLOAD_FOLDER=str(UPLOAD_PATH),
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,
)

INDEX_HTML = """
<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Email CRM (/data persistent)</title>
  <style>
    body { font-family: system-ui, sans-serif; padding:24px; }
    table{ width:100%; border-collapse:collapse; }
    th,td{ padding:8px; border-bottom:1px solid #ddd; vertical-align:top; }
    .mono{ font-family: monospace; }
    .muted{ color:#666; font-size:12px; }
    pre{ white-space:pre-wrap; font-size:12px; background:#f7f7f7; padding:8px; border-radius:6px; }
    .btn{ padding:6px 10px; border:1px solid #888; border-radius:6px; background:#fafafa; }
  </style>
</head>
<body>
  <h1>Email CRM</h1>
  <form method="post" action="{{ url_for('sync_now') }}">
    <button class="btn" type="submit">IMAP同期</button>
  </form>
  <table>
    <thead>
      <tr>
        <th>Key</th><th>件名</th><th>顧客名</th><th>From</th><th>To</th><th>送信日時</th>
      </tr>
    </thead>
    <tbody>
      {% for e in emails %}
      <tr>
        <td class="mono">{{ e.uidvalidity }}-{{ e.uid }}</td>
        <td>{{ e.subject or '' }}</td>
        <td>{{ e.customer_name or '' }}</td>
        <td>{{ e.from_addr or '' }}</td>
        <td>{{ e.to_addr or '' }}</td>
        <td>{{ e.date or '' }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</body>
</html>
"""

@app.get("/")
def index():
    session = Session()
    try:
        rows: List[EmailModel] = (
            session.query(EmailModel)
            .order_by(EmailModel.date.desc().nullslast())
            .all()
        )
        return render_template_string(INDEX_HTML, emails=rows)
    finally:
        session.close()

@app.get("/emails")
def list_emails_api():
    session = Session()
    try:
        rows: List[EmailModel] = session.query(EmailModel).all()
        out = []
        for e in rows:
            out.append({
                "uidvalidity": e.uidvalidity,
                "uid": e.uid,
                "message_id": e.message_id,
                "subject": e.subject,
                "customer_name": e.customer_name,
                "from": e.from_addr,
                "to": e.to_addr,
                "date": str(e.date) if e.date else None,
                "status": e.status,
                "fetched_at": str(e.fetched_at) if e.fetched_at else None,
                "body": e.body,
            })
        return jsonify(out)
    finally:
        session.close()

@app.post("/sync_now")
def sync_now():
    fetch_and_save(limit=50)
    return redirect(url_for("index"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))

