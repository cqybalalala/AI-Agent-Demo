"""
SQLite conversation history with named sessions.
"""
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH        = Path(__file__).parent / "history.db"
ATTACHMENTS_DIR = Path(__file__).parent / "attachments"
ATTACHMENTS_DIR.mkdir(exist_ok=True)


def _conn():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                title      TEXT NOT NULL DEFAULT 'New Chat',
                created_at TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                domain     TEXT NOT NULL,
                role       TEXT NOT NULL,
                content    TEXT NOT NULL,
                image_path TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(session_id)
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_session ON messages(session_id, domain)")
        # Migration: add image_path if missing
        cols = [r[1] for r in con.execute("PRAGMA table_info(messages)").fetchall()]
        if "image_path" not in cols:
            con.execute("ALTER TABLE messages ADD COLUMN image_path TEXT")


def create_session(session_id: str):
    with _conn() as con:
        con.execute(
            "INSERT OR IGNORE INTO sessions (session_id, title, created_at) VALUES (?,?,?)",
            (session_id, "New Chat", datetime.now().isoformat()),
        )


def update_session_title(session_id: str, title: str):
    short = (title[:45] + "…") if len(title) > 45 else title
    with _conn() as con:
        con.execute("UPDATE sessions SET title=? WHERE session_id=?", (short, session_id))


def save_message(session_id: str, domain: str, role: str, content, image_bytes: bytes = None, image_ext: str = "png"):
    """Persist one message. Saves image to attachments/ if provided."""
    if isinstance(content, list):
        text = " ".join(p["text"] for p in content if p.get("type") == "text")
        # Extract image from multimodal content if not explicitly passed
        if image_bytes is None:
            for p in content:
                if p.get("type") == "image_url":
                    url = p["image_url"]["url"]
                    if url.startswith("data:"):
                        import base64
                        header, b64data = url.split(",", 1)
                        image_bytes = base64.b64decode(b64data)
                        image_ext = header.split("/")[1].split(";")[0]
                    break
    elif isinstance(content, str):
        text = content
    else:
        return
    if not text.strip():
        return

    image_path = None
    if image_bytes:
        fname = f"{session_id}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}.{image_ext}"
        image_path = str(ATTACHMENTS_DIR / fname)
        with open(image_path, "wb") as f:
            f.write(image_bytes)

    create_session(session_id)
    with _conn() as con:
        con.execute(
            "INSERT INTO messages (session_id, domain, role, content, image_path, created_at) VALUES (?,?,?,?,?,?)",
            (session_id, domain, role, text, image_path, datetime.now().isoformat()),
        )


def load_messages(session_id: str, domain: str) -> list[dict]:
    import base64
    with _conn() as con:
        rows = con.execute(
            "SELECT role, content, image_path FROM messages WHERE session_id=? AND domain=? ORDER BY id",
            (session_id, domain),
        ).fetchall()
    result = []
    for r in rows:
        if r["image_path"] and Path(r["image_path"]).exists():
            ext = Path(r["image_path"]).suffix.lstrip(".")
            mime = f"image/{ext.replace('jpg','jpeg')}"
            b64 = base64.b64encode(Path(r["image_path"]).read_bytes()).decode()
            result.append({"role": r["role"], "content": [
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                {"type": "text", "text": r["content"]},
            ]})
        else:
            result.append({"role": r["role"], "content": r["content"]})
    return result


def list_sessions() -> list[dict]:
    with _conn() as con:
        rows = con.execute("""
            SELECT s.session_id, s.title, s.created_at,
                   COUNT(m.id) as msg_count
            FROM sessions s
            LEFT JOIN messages m ON m.session_id = s.session_id
            GROUP BY s.session_id
            ORDER BY s.created_at DESC
            LIMIT 50
        """).fetchall()
    return [dict(r) for r in rows]


def delete_session(session_id: str):
    with _conn() as con:
        con.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
        con.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
