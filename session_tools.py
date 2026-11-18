"""
session_tools.py - Pyrogram v2 <-> Telethon session conversion, import and export.

Core (no third-party deps):
  * parse / build Pyrogram string sessions   (struct ">BI?256sQ?", base64url, no padding)
  * parse / build Telethon string sessions   (struct ">B{4|16}sH256s", base64url, "1" prefix)
  * read / write Telethon SQLite .session files (schema version 7, Telethon 1.x)
  * read Pyrogram SQLite .session files (v1 and v2 schema)
  * import .session / .zip / .txt / raw text into a SQLite `user_sessions` table
  * export single or bulk sessions as .session files, ZIP archives, or strings

Optional (needs `telethon`): validate_online() to confirm a session is alive and resolve user_id.

Only use this with accounts you own or are authorised to operate: an auth key is full
account access. Never run the same session from two machines at once (AUTH_KEY_DUPLICATED).
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import ipaddress
import os
import re
import shutil
import sqlite3
import struct
import tempfile
import threading
import time
import zipfile
from contextlib import closing
from dataclasses import dataclass, field
from typing import Iterable, Optional

# --------------------------------------------------------------------------- constants
PYRO_FMT_V2 = ">BI?256sQ?"   # dc_id, api_id, test_mode, auth_key, user_id(64), is_bot   -> 271 bytes / 362 chars
PYRO_FMT_64 = ">B?256sQ?"    # dc_id, test_mode, auth_key, user_id(64), is_bot           -> 267 bytes / 356 chars
PYRO_FMT_32 = ">B?256sI?"    # legacy 32-bit user id                                     -> 263 bytes / 351 chars
PYRO_SIZES = {struct.calcsize(PYRO_FMT_V2): PYRO_FMT_V2,
              struct.calcsize(PYRO_FMT_64): PYRO_FMT_64,
              struct.calcsize(PYRO_FMT_32): PYRO_FMT_32}

TELETHON_VERSION_PREFIX = "1"
TELETHON_SQLITE_VERSION = 7
SQLITE_MAGIC = b"SQLite format 3\x00"

DC_IPV4 = {1: "149.154.175.53", 2: "149.154.167.51", 3: "149.154.175.100",
           4: "149.154.167.91", 5: "91.108.56.130"}
DC_IPV4_TEST = {1: "149.154.175.10", 2: "149.154.167.40", 3: "149.154.175.117"}
TEST_IPS = set(DC_IPV4_TEST.values())

TOKEN_RE = re.compile(r"[A-Za-z0-9_\-+/]{300,}={0,2}")

MAX_ZIP_MEMBERS = 5000
MAX_MEMBER_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 200 * 1024 * 1024


class SessionError(ValueError):
    """Raised for any invalid / unsupported / corrupt session input."""


# --------------------------------------------------------------------------- model
@dataclass
class SessionData:
    dc_id: int
    auth_key: bytes
    api_id: int = 0            # not stored in Telethon sessions; supply it when converting to Pyrogram
    user_id: int = 0           # not stored in Telethon sessions; resolve with validate_online()
    is_bot: bool = False
    test_mode: bool = False
    ip: Optional[str] = None   # Telethon only; derived from dc_id when missing
    port: int = 443

    def __post_init__(self):
        self.auth_key = bytes(self.auth_key)
        self.validate()
        if not self.ip:
            self.ip = (DC_IPV4_TEST if self.test_mode else DC_IPV4).get(self.dc_id) or DC_IPV4[self.dc_id]

    def validate(self) -> None:
        if not isinstance(self.dc_id, int) or not 1 <= self.dc_id <= 5:
            raise SessionError(f"invalid dc_id: {self.dc_id!r}")
        if len(self.auth_key) != 256:
            raise SessionError(f"auth_key must be 256 bytes, got {len(self.auth_key)}")
        if not any(self.auth_key):
            raise SessionError("auth_key is all zeroes (never-authorised session)")
        if self.api_id < 0 or self.user_id < 0:
            raise SessionError("api_id/user_id must be >= 0")

    @property
    def key_hash(self) -> str:
        return hashlib.sha256(self.auth_key).hexdigest()


# --------------------------------------------------------------------------- string helpers
def _clean(s: str) -> str:
    return re.sub(r"[\s\"'`,;]", "", s or "")


def _b64d(s: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(s.rstrip("=") + "=" * (-len(s.rstrip("=")) % 4))
    except (binascii.Error, ValueError) as e:
        raise SessionError(f"bad base64: {e}") from None


# --------------------------------------------------------------------------- Pyrogram strings
def parse_pyrogram_string(s: str) -> SessionData:
    s = _clean(s)
    raw = _b64d(s)
    fmt = PYRO_SIZES.get(len(raw))
    if not fmt:
        raise SessionError(f"not a Pyrogram session string (decoded {len(raw)} bytes)")
    if fmt == PYRO_FMT_V2:
        dc, api_id, test, key, uid, bot = struct.unpack(fmt, raw)
    else:
        dc, test, key, uid, bot = struct.unpack(fmt, raw)
        api_id = 0
    return SessionData(dc_id=dc, auth_key=key, api_id=api_id, user_id=uid, is_bot=bot, test_mode=test)


def to_pyrogram_string(sd: SessionData) -> str:
    sd.validate()
    raw = struct.pack(PYRO_FMT_V2, sd.dc_id, sd.api_id, sd.test_mode, sd.auth_key, sd.user_id, sd.is_bot)
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


# --------------------------------------------------------------------------- Telethon strings
def parse_telethon_string(s: str) -> SessionData:
    s = _clean(s)
    if not s.startswith(TELETHON_VERSION_PREFIX):
        raise SessionError("Telethon string must start with '1'")
    body = s[1:]
    ip_len = 4 if len(body.rstrip("=")) in (350, 351, 352) and len(body) <= 352 else 16
    raw = _b64d(body)
    if len(raw) != 1 + ip_len + 2 + 256:
        raise SessionError(f"not a Telethon session string (decoded {len(raw)} bytes)")
    dc, ip_b, port, key = struct.unpack(f">B{ip_len}sH256s", raw)
    ip = ipaddress.ip_address(ip_b).compressed
    return SessionData(dc_id=dc, auth_key=key, ip=ip, port=port, test_mode=ip in TEST_IPS)


def to_telethon_string(sd: SessionData) -> str:
    sd.validate()
    ip_b = ipaddress.ip_address(sd.ip).packed
    raw = struct.pack(f">B{len(ip_b)}sH256s", sd.dc_id, ip_b, sd.port, sd.auth_key)
    return TELETHON_VERSION_PREFIX + base64.urlsafe_b64encode(raw).decode()


def parse_string_session(s: str) -> SessionData:
    """Auto-detect Pyrogram vs Telethon string."""
    c = _clean(s)
    if c.startswith(TELETHON_VERSION_PREFIX) and len(c) in (353, 369):
        return parse_telethon_string(c)
    try:
        return parse_pyrogram_string(c)
    except SessionError:
        return parse_telethon_string(c)


# --------------------------------------------------------------------------- SQLite files
_TELETHON_SCHEMA = """
CREATE TABLE version (version integer primary key);
CREATE TABLE sessions (dc_id integer primary key, server_address text, port integer,
                       auth_key blob, takeout_id integer);
CREATE TABLE entities (id integer primary key, hash integer not null, username text,
                       phone integer, name text, date integer);
CREATE TABLE sent_files (md5_digest blob, file_size integer, type integer, id integer,
                         hash integer, primary key(md5_digest, file_size, type));
CREATE TABLE update_state (id integer primary key, pts integer, qts integer, date integer, seq integer);
"""


def write_telethon_sqlite(sd: SessionData, path: str) -> None:
    """Write a fresh Telethon .session atomically (tmp file + os.replace); no lock/journal leftovers."""
    sd.validate()
    tmp = f"{path}.{os.getpid()}.tmp"
    for p in (tmp, tmp + "-journal"):
        if os.path.exists(p):
            os.remove(p)
    try:
        with closing(sqlite3.connect(tmp, timeout=30)) as con:
            con.execute("PRAGMA journal_mode=DELETE")
            con.executescript(_TELETHON_SCHEMA)
            con.execute("INSERT INTO version VALUES (?)", (TELETHON_SQLITE_VERSION,))
            con.execute("INSERT INTO sessions VALUES (?,?,?,?,NULL)",
                        (sd.dc_id, sd.ip, sd.port, sqlite3.Binary(sd.auth_key)))
            con.commit()
        os.replace(tmp, path)
    finally:
        for p in (tmp, tmp + "-journal"):
            if os.path.exists(p):
                os.remove(p)


def _snapshot(path: str, workdir: str) -> str:
    """Copy db (+wal/shm) so we never lock or modify the original."""
    dst = os.path.join(workdir, "snap.db")
    shutil.copy2(path, dst)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(path + suffix):
            shutil.copy2(path + suffix, dst + suffix)
    return dst


def read_session_sqlite(path: str) -> SessionData:
    """Read a Telethon or Pyrogram SQLite session (auto-detected)."""
    with open(path, "rb") as f:
        if f.read(16) != SQLITE_MAGIC:
            raise SessionError("not an SQLite file")
    with tempfile.TemporaryDirectory() as wd:
        snap = _snapshot(path, wd)
        try:
            with closing(sqlite3.connect(snap, timeout=30)) as con:
                cols = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
                if not cols:
                    raise SessionError("no 'sessions' table")
                if "server_address" in cols:  # Telethon
                    row = con.execute("SELECT dc_id, server_address, port, auth_key FROM sessions "
                                      "WHERE auth_key IS NOT NULL LIMIT 1").fetchone()
                    if not row:
                        raise SessionError("Telethon session has no auth key")
                    dc, ip, port, key = row
                    return SessionData(dc_id=dc, auth_key=key, ip=ip, port=port or 443,
                                       test_mode=ip in TEST_IPS)
                # Pyrogram (v1: no api_id; v2: api_id)
                want = [c for c in ("dc_id", "api_id", "test_mode", "auth_key", "user_id", "is_bot") if c in cols]
                row = dict(zip(want, con.execute(f"SELECT {','.join(want)} FROM sessions LIMIT 1").fetchone() or ()))
                if not row.get("auth_key"):
                    raise SessionError("Pyrogram session has no auth key")
                return SessionData(dc_id=row["dc_id"], auth_key=row["auth_key"],
                                   api_id=row.get("api_id") or 0, user_id=row.get("user_id") or 0,
                                   is_bot=bool(row.get("is_bot")), test_mode=bool(row.get("test_mode")))
        except sqlite3.DatabaseError as e:
            raise SessionError(f"corrupt SQLite session: {e}") from None


# --------------------------------------------------------------------------- import
@dataclass
class ImportReport:
    added: int = 0
    duplicates: int = 0
    failed: list = field(default_factory=list)   # [(source, reason)]

    def summary(self) -> str:
        s = f"added={self.added} duplicates={self.duplicates} failed={len(self.failed)}"
        return s + "".join(f"\n  - {src}: {why}" for src, why in self.failed[:20])


def _extract_from_text(text: str) -> list[tuple[str, str]]:
    """Return [(label, token)] for every session-string-looking token."""
    out, seen = [], set()
    for m in TOKEN_RE.finditer(text):
        tok = m.group(0)
        if tok not in seen:
            seen.add(tok)
            out.append((tok[:12] + "…", tok))
    return out


def parse_any(data: bytes, filename: str = "") -> Iterable[tuple[str, SessionData | Exception]]:
    """Yield (source, SessionData-or-Exception) from .session / .zip / text input."""
    name = os.path.basename(filename or "input")

    def from_bytes(b: bytes, src: str):
        if b.startswith(SQLITE_MAGIC):
            with tempfile.TemporaryDirectory() as wd:
                p = os.path.join(wd, "in.session")
                with open(p, "wb") as f:
                    f.write(b)
                try:
                    yield src, read_session_sqlite(p)
                except Exception as e:  # noqa: BLE001
                    yield src, e
            return
        try:
            text = b.decode("utf-8-sig", errors="ignore")
        except Exception as e:  # noqa: BLE001
            yield src, SessionError(f"undecodable: {e}")
            return
        found = _extract_from_text(text)
        if not found:
            yield src, SessionError("no session string or SQLite data found")
        for label, tok in found:
            try:
                yield f"{src}:{label}", parse_string_session(tok)
            except Exception as e:  # noqa: BLE001
                yield f"{src}:{label}", e

    if zipfile.is_zipfile(io.BytesIO(data)):
        total = 0
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = [i for i in z.infolist() if not i.is_dir() and "__MACOSX" not in i.filename
                     and not i.filename.endswith((".session-journal", ".session-wal", ".session-shm"))]
            if len(infos) > MAX_ZIP_MEMBERS:
                yield name, SessionError("too many files in ZIP")
                return
            for i in infos:
                total += i.file_size
                if i.file_size > MAX_MEMBER_BYTES or total > MAX_TOTAL_BYTES:
                    yield f"{name}/{i.filename}", SessionError("file too large (zip-bomb guard)")
                    continue
                try:
                    yield from from_bytes(z.read(i), f"{name}/{i.filename}")
                except zipfile.BadZipFile as e:
                    yield f"{name}/{i.filename}", SessionError(f"bad zip member: {e}")
        return
    yield from from_bytes(data, name)


class SessionStore:
    """SQLite-backed `user_sessions` table. Thread-safe; call via asyncio.to_thread() from async code."""

    def __init__(self, db_path: str = "sessions.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS user_sessions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id   INTEGER NOT NULL,
                key_hash   TEXT    NOT NULL,
                dc_id      INTEGER NOT NULL,
                api_id     INTEGER NOT NULL DEFAULT 0,
                user_id    INTEGER NOT NULL DEFAULT 0,
                is_bot     INTEGER NOT NULL DEFAULT 0,
                test_mode  INTEGER NOT NULL DEFAULT 0,
                ip         TEXT,
                port       INTEGER NOT NULL DEFAULT 443,
                auth_key   BLOB    NOT NULL,   -- encrypt at rest in production (e.g. Fernet)
                label      TEXT,
                created_at INTEGER NOT NULL,
                UNIQUE(owner_id, key_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_us_owner ON user_sessions(owner_id);
            """)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA synchronous=NORMAL")
        c.row_factory = sqlite3.Row
        return c

    @staticmethod
    def _row_to_sd(r: sqlite3.Row) -> SessionData:
        return SessionData(dc_id=r["dc_id"], auth_key=r["auth_key"], api_id=r["api_id"], user_id=r["user_id"],
                           is_bot=bool(r["is_bot"]), test_mode=bool(r["test_mode"]), ip=r["ip"], port=r["port"])

    def add(self, owner_id: int, sd: SessionData, label: str = "") -> bool:
        """Insert or merge. Returns True if new. Duplicate keys fill in missing api_id/user_id."""
        sd.validate()
        with self._lock, closing(self._conn()) as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO user_sessions (owner_id,key_hash,dc_id,api_id,user_id,is_bot,test_mode,"
                "ip,port,auth_key,label,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (owner_id, sd.key_hash, sd.dc_id, sd.api_id, sd.user_id, int(sd.is_bot), int(sd.test_mode),
                 sd.ip, sd.port, sqlite3.Binary(sd.auth_key), label, int(time.time())))
            if cur.rowcount:
                return True
            c.execute("UPDATE user_sessions SET api_id=CASE WHEN api_id=0 THEN ? ELSE api_id END,"
                      "user_id=CASE WHEN user_id=0 THEN ? ELSE user_id END WHERE owner_id=? AND key_hash=?",
                      (sd.api_id, sd.user_id, owner_id, sd.key_hash))
            return False

    def import_data(self, owner_id: int, data: bytes, filename: str = "") -> ImportReport:
        rep = ImportReport()
        for src, item in parse_any(data, filename):
            if isinstance(item, Exception):
                rep.failed.append((src, str(item)))
            elif self.add(owner_id, item, label=src[:64]):
                rep.added += 1
            else:
                rep.duplicates += 1
        return rep

    def import_text(self, owner_id: int, text: str) -> ImportReport:
        return self.import_data(owner_id, text.encode(), "pasted.txt")

    def get(self, owner_id: int, session_id: int) -> SessionData:
        with closing(self._conn()) as c:
            r = c.execute("SELECT * FROM user_sessions WHERE owner_id=? AND id=?", (owner_id, session_id)).fetchone()
        if not r:
            raise SessionError("session not found")
        return self._row_to_sd(r)

    def list(self, owner_id: int) -> list[tuple[int, SessionData]]:
        with closing(self._conn()) as c:
            rows = c.execute("SELECT * FROM user_sessions WHERE owner_id=? ORDER BY id", (owner_id,)).fetchall()
        return [(r["id"], self._row_to_sd(r)) for r in rows]

    def delete(self, owner_id: int, session_id: int) -> bool:
        with self._lock, closing(self._conn()) as c:
            return c.execute("DELETE FROM user_sessions WHERE owner_id=? AND id=?",
                             (owner_id, session_id)).rowcount > 0

    def set_user_id(self, owner_id: int, session_id: int, user_id: int, api_id: int | None = None) -> None:
        with self._lock, closing(self._conn()) as c:
            c.execute("UPDATE user_sessions SET user_id=?, api_id=COALESCE(?,api_id) WHERE owner_id=? AND id=?",
                      (user_id, api_id, owner_id, session_id))


# --------------------------------------------------------------------------- export
def export_telethon_file(sd: SessionData) -> bytes:
    """Return the bytes of a valid Telethon .session file (send with BytesIO + name)."""
    with tempfile.TemporaryDirectory() as wd:
        p = os.path.join(wd, "out.session")
        write_telethon_sqlite(sd, p)
        with open(p, "rb") as f:
            return f.read()


def _export_name(sd: SessionData, used: set[str]) -> str:
    base = str(sd.user_id) if sd.user_id else sd.key_hash[:10]
    name, n = f"{base}.session", 1
    while name in used:
        n += 1
        name = f"{base}_{n}.session"
    used.add(name)
    return name


def export_zip(sessions: Iterable[SessionData], include_strings: bool = False) -> bytes:
    """ZIP of Telethon .session files. include_strings adds strings.txt with both string formats."""
    buf, used, lines = io.BytesIO(), set(), []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for sd in sessions:
            name = _export_name(sd, used)
            z.writestr(name, export_telethon_file(sd))
            if include_strings:
                lines.append(f"{name}\n  telethon: {to_telethon_string(sd)}\n  pyrogram: {to_pyrogram_string(sd)}")
        if include_strings and lines:
            z.writestr("strings.txt", "\n".join(lines))
    return buf.getvalue()


def export_strings_txt(sessions: Iterable[SessionData], fmt: str = "pyrogram") -> str:
    fn = {"pyrogram": to_pyrogram_string, "telethon": to_telethon_string}[fmt]
    return "\n".join(fn(sd) for sd in sessions)


# --------------------------------------------------------------------------- optional online validation
async def validate_online(sd: SessionData, api_id: int, api_hash: str, timeout: float = 25.0):
    """Connect with Telethon, return the `User` if authorised else None. Does not modify the session."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(to_telethon_string(sd)), api_id, api_hash)
    try:
        await asyncio.wait_for(client.connect(), timeout)
        if not await client.is_user_authorized():
            return None
        return await client.get_me()
    finally:
        await client.disconnect()


async def fill_identity(sd: SessionData, api_id: int, api_hash: str) -> SessionData:
    """Set user_id / is_bot / api_id on a Telethon-sourced session so the Pyrogram string is usable."""
    me = await validate_online(sd, api_id, api_hash)
    if me is None:
        raise SessionError("session is not authorised (revoked or expired)")
    sd.user_id, sd.is_bot, sd.api_id = me.id, bool(me.bot), api_id
    return sd


# --------------------------------------------------------------------------- self-test
if __name__ == "__main__":
    key = os.urandom(256)
    py = SessionData(dc_id=2, auth_key=key, api_id=12345, user_id=777000123, is_bot=False)
    pys = to_pyrogram_string(py)
    ths = to_telethon_string(py)
    print("pyrogram len", len(pys), "| telethon len", len(ths))

    assert parse_pyrogram_string(pys) == py or parse_pyrogram_string(pys).auth_key == key
    back = parse_telethon_string(ths)
    assert back.auth_key == key and back.dc_id == 2 and back.ip == "149.154.167.51"
    assert parse_string_session(pys).user_id == 777000123 and parse_string_session(ths).dc_id == 2

    # telethon string -> pyrogram string (with identity supplied)
    back.api_id, back.user_id = 12345, 777000123
    assert to_pyrogram_string(back) == pys

    # sqlite round-trip
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "t.session")
        write_telethon_sqlite(py, p)
        assert not [f for f in os.listdir(d) if f != "t.session"], "leftover journal/tmp files"
        r = read_session_sqlite(p)
        assert r.auth_key == key and r.dc_id == 2
        assert to_telethon_string(r) == ths

    # import: txt + zip + raw sqlite, with duplicates & garbage
    k2 = SessionData(dc_id=4, auth_key=os.urandom(256), api_id=1, user_id=42)
    z = export_zip([py, k2], include_strings=True)
    store = SessionStore(os.path.join(tempfile.mkdtemp(), "s.db"))
    rep = store.import_data(1, z, "bulk.zip")
    print("zip import:", rep.summary())
    rep = store.import_text(1, f"junk\n{pys}\n{to_pyrogram_string(k2)}, {ths}\nnot-a-session")
    print("txt import:", rep.summary())
    assert len(store.list(1)) == 2
    assert zipfile.ZipFile(io.BytesIO(export_zip(s for _, s in store.list(1)))).testzip() is None
    assert store.import_data(1, export_telethon_file(py), "x.session").duplicates == 1
    bad = store.import_data(1, b"SQLite format 3\x00garbage", "bad.session")
    assert len(bad.failed) == 1
    print("ALL OK")
