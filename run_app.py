"""
run_app.py – ダブルクリック用ランチャー
  1. 既にサーバーが動いていればブラウザを開いて終了
  2. 初回は Flask サーバーを起動
  3. http://127.0.0.1:5000/ を自動オープン
  4. run_app.log に最大 1 MB × 3 世代のログ
"""
import sys, threading, time, webbrowser, logging, socket
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ── ログ設定 ───────────────────────────────
RUN_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
handler = RotatingFileHandler(RUN_DIR / "run_app.log",
                              maxBytes=1_000_000, backupCount=3, encoding="utf-8")
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", handlers=[handler])
logger = logging.getLogger(__name__)

# ── 既存サーバー確認（ポート 5000） ──────────
def server_running() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", 5000)) == 0

def open_browser():
    url = "http://127.0.0.1:5000/"
    time.sleep(1)            # サーバー起動待ち
    logger.info(f"ブラウザで {url} を起動")
    try: webbrowser.open(url)
    except Exception: logger.exception("ブラウザ起動に失敗")

if server_running():
    logger.info("既にサーバーが動作中 → ブラウザのみ開いて終了")
    open_browser(); sys.exit(0)

logger.info("=== run_app.py 初回起動 ===")

# ── Flask アプリ読み込み ───────────────────
try:
    import app                               # app.py の Flask インスタンス(app.app)
except Exception:
    logger.exception("app.py のインポート失敗"); sys.exit(1)

app.app.logger.setLevel(logging.INFO)
app.app.logger.handlers.clear(); app.app.logger.addHandler(handler)

# ── ブラウザ後追い ─────────────────────────
threading.Thread(target=open_browser, daemon=True).start()

# ── サーバー起動 ───────────────────────────
try:
    logger.info("Flask サーバーを起動します")
    app.app.run(host="0.0.0.0", port=5000, debug=False)
except Exception:
    logger.exception("Flask サーバーで例外発生")
finally:
    logger.info("=== run_app.py 終了 ===")
