import sqlite3
import threading

DB_NAME = "sessions.db"
_db_lock = threading.Lock()

def _get_conn():
    conn = sqlite3.connect(DB_NAME, timeout=30.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn

def init_db():
    with _db_lock:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                session_name TEXT,
                session_string TEXT,
                safe_mode INTEGER DEFAULT 0
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_user_id ON user_sessions(user_id);")
        conn.commit()
        conn.close()

def add_session(user_id, session_name, session_string):
    with _db_lock:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO user_sessions (user_id, session_name, session_string, safe_mode) VALUES (?, ?, ?, 0)",
            (user_id, session_name, session_string)
        )
        conn.commit()
        conn.close()

def get_user_sessions(user_id):
    with _db_lock:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("SELECT id, session_name, safe_mode FROM user_sessions WHERE user_id = ?", (user_id,))
        rows = cur.fetchall()
        conn.close()
        return rows

def get_session_by_id(sess_id, user_id):
    with _db_lock:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("SELECT session_string, session_name, safe_mode FROM user_sessions WHERE id = ? AND user_id = ?", (sess_id, user_id))
        row = cur.fetchone()
        conn.close()
        return row

def get_all_user_sessions_full(user_id):
    with _db_lock:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("SELECT id, session_name, session_string, safe_mode FROM user_sessions WHERE user_id = ?", (user_id,))
        rows = cur.fetchall()
        conn.close()
        return rows

def delete_session_by_id(sess_id, user_id):
    with _db_lock:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM user_sessions WHERE id = ? AND user_id = ?", (sess_id, user_id))
        conn.commit()
        conn.close()

def toggle_safe_mode(sess_id, user_id):
    with _db_lock:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("SELECT safe_mode FROM user_sessions WHERE id = ? AND user_id = ?", (sess_id, user_id))
        row = cur.fetchone()
        if row:
            new_val = 0 if row[0] == 1 else 1
            cur.execute("UPDATE user_sessions SET safe_mode = ? WHERE id = ? AND user_id = ?", (new_val, sess_id, user_id))
            conn.commit()
            conn.close()
            return new_val
        conn.close()
        return 0
