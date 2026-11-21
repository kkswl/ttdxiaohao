"""Plugin-owned selection, one-use previews and sanitized audit records only."""

import json
import re
import secrets
import sqlite3
import time
from pathlib import Path

from .errors import UserError

QR_RETENTION_SECONDS = 300
QR_FILENAME = re.compile(r"login-qr-(?:[0-9a-f]{32}|[0-9a-f]{16}-[0-9a-f]{16})[.]png")


class State:
    def __init__(self, data_dir):
        directory = Path(data_dir)
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory.resolve()
        self.db = sqlite3.connect(directory / "account-manager.sqlite3", timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS current_scope (singleton INTEGER PRIMARY KEY CHECK(singleton=1), scope TEXT);
            CREATE TABLE IF NOT EXISTS selection (scope TEXT, user_id TEXT, PRIMARY KEY(scope,user_id));
            CREATE TABLE IF NOT EXISTS target (scope TEXT PRIMARY KEY, account_id TEXT);
            CREATE TABLE IF NOT EXISTS qr_images (
                scope TEXT, account_id TEXT, filename TEXT, created REAL, expires REAL,
                PRIMARY KEY(scope,account_id));
            CREATE TABLE IF NOT EXISTS qr_flows (
                scope TEXT, account_id TEXT, encrypted BLOB, created REAL, expires REAL,
                preview_id TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(scope,account_id));
            CREATE TABLE IF NOT EXISTS previews (
                id TEXT PRIMARY KEY, scope TEXT, created REAL, status TEXT, payload TEXT);
            CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, preview_id TEXT, user_id TEXT, status TEXT, detail TEXT, updated REAL);
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY, created REAL, scope TEXT, action TEXT,
                account_id TEXT, user_id TEXT, result TEXT);
        """)
        if "expires" not in {row[1] for row in self.db.execute("PRAGMA table_info(qr_images)")}:
            with self.db:
                self.db.execute("ALTER TABLE qr_images ADD COLUMN expires REAL")
        if "preview_id" not in {row[1] for row in self.db.execute("PRAGMA table_info(qr_flows)")}:
            with self.db:
                self.db.execute("ALTER TABLE qr_flows ADD COLUMN preview_id TEXT NOT NULL DEFAULT ''")

    def close(self):
        self.db.close()

    def enter_scope(self, scope):
        with self.db:
            row = self.db.execute("SELECT scope FROM current_scope WHERE singleton=1").fetchone()
            changed = row is not None and row[0] != scope
            if changed:
                self.db.execute("DELETE FROM selection")
                self.db.execute("DELETE FROM target")
                self.db.execute("UPDATE previews SET status='stale' WHERE status='open'")
            self.db.execute("INSERT OR REPLACE INTO current_scope VALUES(1,?)", (scope,))
        return changed

    def target(self, scope, account_id=None):
        if account_id is not None:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO target VALUES(?,?)", (scope, account_id))
                self.db.execute("UPDATE previews SET status='stale' WHERE scope=? AND status='open'", (scope,))
        row = self.db.execute("SELECT account_id FROM target WHERE scope=?", (scope,)).fetchone()
        return row[0] if row else ""

    def selected(self, scope):
        return [r[0] for r in self.db.execute("SELECT user_id FROM selection WHERE scope=? ORDER BY rowid", (scope,))]

    def select(self, scope, user_id, add):
        with self.db:
            if add:
                selected = self.selected(scope)
                if user_id in selected:
                    return
                if len(selected) >= 30:
                    raise UserError("每批最多选择 30 位用户，请分批操作。")
                self.db.execute("INSERT OR IGNORE INTO selection VALUES(?,?)", (scope, user_id))
            else:
                self.db.execute("DELETE FROM selection WHERE scope=? AND user_id=?", (scope, user_id))
            self.db.execute("UPDATE previews SET status='stale' WHERE scope=? AND status='open'", (scope,))

    def clear(self, scope):
        with self.db:
            self.db.execute("DELETE FROM selection WHERE scope=?", (scope,))
            self.db.execute("UPDATE previews SET status='stale' WHERE scope=? AND status='open'", (scope,))

    def preview(self, scope, payload):
        preview_id = secrets.token_hex(16)
        with self.db:
            self.db.execute("UPDATE previews SET status='stale' WHERE scope=? AND status='open'", (scope,))
            self.db.execute("INSERT INTO previews VALUES(?,?,?,?,?)",
                            (preview_id, scope, time.time(), "open", json.dumps(payload, ensure_ascii=False)))
        return preview_id

    def load_preview(self, scope, preview_id):
        row = self.db.execute("SELECT * FROM previews WHERE id=? AND scope=?", (preview_id, scope)).fetchone()
        if not row or row["status"] != "open" or not 0 <= time.time() - row["created"] <= 300:
            raise UserError("预览已过期、被使用或属于其他规则，请重新生成预览。")
        return json.loads(row["payload"])

    def claim(self, scope, preview_id):
        payload = self.load_preview(scope, preview_id)
        with self.db:
            now = time.time()
            count = self.db.execute("""UPDATE previews SET status='executing'
                WHERE id=? AND scope=? AND status='open' AND created BETWEEN ? AND ?""",
                                    (preview_id, scope, now - 300, now)).rowcount
            if count != 1:
                raise UserError("该操作已经提交，不能重复执行。")
        return payload

    def _qr_path(self, filename):
        if not isinstance(filename, str) or not QR_FILENAME.fullmatch(filename):
            raise UserError("二维码图片记录无效，已停止读取。")
        # Keep the literal child path: resolving a symlink before unlink would delete its target.
        return self.directory / filename

    def remember_qr(self, scope, account_id, filename, expires=None):
        self._qr_path(filename)
        prior = self.db.execute("SELECT filename FROM qr_images WHERE scope=? AND account_id=?",
                                (scope, account_id)).fetchone()
        with self.db:
            now = time.time()
            self.db.execute("INSERT OR REPLACE INTO qr_images VALUES(?,?,?,?,?)",
                            (scope, account_id, filename, now, expires or now + QR_RETENTION_SECONDS))
        if prior and prior[0] != filename:
            self._remove_qr_file(prior[0])

    def _remove_qr_file(self, filename):
        try:
            self._qr_path(filename).unlink(missing_ok=True)
        except OSError:
            raise UserError("无法清理二维码图片，请检查插件数据目录权限。") from None

    def clear_qr(self, scope, account_id):
        row = self.db.execute("SELECT filename FROM qr_images WHERE scope=? AND account_id=?",
                              (scope, account_id)).fetchone()
        if row:
            self._remove_qr_file(row[0])
        with self.db:
            self.db.execute("DELETE FROM qr_images WHERE scope=? AND account_id=?", (scope, account_id))
            self.db.execute("DELETE FROM qr_flows WHERE scope=? AND account_id=?", (scope, account_id))

    def remember_flow(self, scope, account_id, encrypted, expires, preview_id=""):
        if not isinstance(encrypted, bytes):
            raise UserError("扫码上下文格式无效。")
        with self.db:
            self.db.execute("""INSERT OR REPLACE INTO qr_flows
                (scope,account_id,encrypted,created,expires,preview_id) VALUES(?,?,?,?,?,?)""",
                            (scope, account_id, encrypted, time.time(), expires, preview_id))

    def qr_approval(self, scope, account_id):
        if not self.qr_flow(scope, account_id):
            return None
        row = self.db.execute("""SELECT p.payload,p.status FROM qr_flows q JOIN previews p
            ON p.id=q.preview_id AND p.scope=q.scope WHERE q.scope=? AND q.account_id=?""",
                              (scope, account_id)).fetchone()
        if not row or row["status"] not in {"executing", "done"}:
            return None
        payload = json.loads(row["payload"])
        account = payload.get("account")
        if payload.get("kind") != "qr_start" or not isinstance(account, dict) or account.get("id") != account_id:
            return None
        return account

    def qr_flow(self, scope, account_id):
        row = self.db.execute("SELECT * FROM qr_flows WHERE scope=? AND account_id=?",
                              (scope, account_id)).fetchone()
        if not row:
            return None
        if not 0 <= time.time() - row["created"] <= QR_RETENTION_SECONDS or row["expires"] <= time.time():
            self.clear_qr(scope, account_id)
            return None
        return row["encrypted"]

    def qr_image(self, scope, account_id):
        row = self.db.execute("SELECT filename,created,expires FROM qr_images WHERE scope=? AND account_id=?",
                              (scope, account_id)).fetchone()
        if not row:
            return None
        if (not 0 <= time.time() - row["created"] <= QR_RETENTION_SECONDS
                or (row["expires"] is not None and row["expires"] <= time.time())):
            self.clear_qr(scope, account_id)
            return None
        if not self._qr_path(row["filename"]).is_file():
            self.clear_qr(scope, account_id)
            return None
        return "data://" + row["filename"]

    def cleanup_qr(self):
        """A bounded retention policy on task entry, not a claim about server QR expiry."""
        now = time.time()
        rows = self.db.execute("SELECT * FROM qr_images").fetchall()
        for row in rows:
            if (not 0 <= now - row["created"] <= QR_RETENTION_SECONDS
                    or (row["expires"] is not None and row["expires"] <= now)):
                self.clear_qr(row["scope"], row["account_id"])
        for row in self.db.execute("SELECT * FROM qr_flows").fetchall():
            if not 0 <= now - row["created"] <= QR_RETENTION_SECONDS or row["expires"] <= now:
                self.clear_qr(row["scope"], row["account_id"])
        tracked = {r[0] for r in self.db.execute("SELECT filename FROM qr_images")}
        for path in self.directory.glob("login-qr-*.png"):
            if QR_FILENAME.fullmatch(path.name) and path.name not in tracked:
                try:
                    if not 0 <= now - path.stat().st_mtime <= QR_RETENTION_SECONDS:
                        self._remove_qr_file(path.name)
                except FileNotFoundError:
                    pass

    def finish(self, preview_id):
        with self.db:
            self.db.execute("UPDATE previews SET status='done' WHERE id=?", (preview_id,))

    def pending(self, preview_id, user_id):
        operation_id = secrets.token_hex(16)
        with self.db:
            self.db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?)",
                            (operation_id, preview_id, user_id, "pending", "已提交，待核实", time.time()))
        return operation_id

    def result(self, operation_id, status, detail):
        with self.db:
            self.db.execute("UPDATE operations SET status=?,detail=?,updated=? WHERE id=?",
                            (status, detail, time.time(), operation_id))

    def unresolved(self, scope):
        return [dict(r) for r in self.db.execute("""
            SELECT o.*,p.payload FROM operations o JOIN previews p ON p.id=o.preview_id
            WHERE p.scope=? AND o.status IN ('pending','unknown') ORDER BY o.updated
        """, (scope,))]

    def log(self, scope, action, account_id, user_id, result):
        # Every caller supplies allowlisted identifiers and fixed result text, never raw responses.
        with self.db:
            self.db.execute("INSERT INTO audit(created,scope,action,account_id,user_id,result) VALUES(?,?,?,?,?,?)",
                            (time.time(), scope, action, account_id, user_id, result))
            self.db.execute("DELETE FROM audit WHERE id NOT IN (SELECT id FROM audit ORDER BY id DESC LIMIT 2000)")

    def history(self, scope):
        return [dict(r) for r in self.db.execute("SELECT * FROM audit WHERE scope=? ORDER BY id DESC LIMIT 30", (scope,))]
