"""SQLite 存储层（P1）。

设计取舍：内存里的 `self.index` 仍然是读取的唯一入口（32 处消费点不动），
DB 承担三件事：
1. **原子持久化**：WAL + 事务替代 write_text 全量重写；
2. **MD5 索引**：按关键词查 MD5，替代"遍历目录逐文件算"（冷启动也不再全盘扫）；
3. **关系数据**：别名组、管理员、审计（P2 用）。

JSON 镜像继续写（`index.json`），保证旧版可恢复、备份格式不变。
`STICKER_STORAGE=json` 时整个模块不启用，行为回到 1.4.1。
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

SCHEMA_VERSION = 2

DDL = """
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS keyword (
    k TEXT PRIMARY KEY,
    blocked INTEGER NOT NULL DEFAULT 0,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS image (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kw TEXT NOT NULL REFERENCES keyword(k) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    md5 TEXT,
    ext TEXT,
    bytes INTEGER,
    created_at REAL,
    weight INTEGER NOT NULL DEFAULT 1,
    UNIQUE(kw, filename)
);
CREATE INDEX IF NOT EXISTS ix_image_kw ON image(kw, id);
CREATE INDEX IF NOT EXISTS ix_image_md5 ON image(md5);
CREATE TABLE IF NOT EXISTS alias_group (
    primary_kw TEXT PRIMARY KEY REFERENCES keyword(k) ON DELETE CASCADE,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS alias (
    alias TEXT PRIMARY KEY,
    primary_kw TEXT NOT NULL REFERENCES alias_group(primary_kw) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS admin (
    platform_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    created_at REAL,
    granted_by TEXT,
    PRIMARY KEY (platform_id, user_id)
);
CREATE TABLE IF NOT EXISTS admin_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    action TEXT NOT NULL,
    actor TEXT,
    target TEXT,
    result TEXT
);
"""


def storage_mode() -> str:
    """存储模式：sqlite（默认）或 json（回滚开关）。"""
    value = (os.environ.get("STICKER_STORAGE") or "").strip().lower()
    return "json" if value == "json" else "sqlite"


class Store:
    """薄封装：只做 SQL，不含业务判断。"""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # 备份/回填都在 asyncio.to_thread 里跑，连接必须允许跨线程；
        # 单连接多线程写会互相踩，所以再用一把可重入锁串行化写操作。
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.db_path), timeout=5.0, check_same_thread=False)
        # 两个进程同时首次打开时 PRAGMA journal_mode 会撞 database is locked（实测 6.7%）：
        # 重试几次；仍失败就沿用默认 journal，不因此拒绝启动。
        for attempt in range(5):
            try:
                self.conn.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 4:
                    break   # 仍锁着就沿用默认 journal，不因此拒绝启动
                time.sleep(0.05 * (attempt + 1))
        self.conn.row_factory = sqlite3.Row
        # 注意：这里不要再调一次 PRAGMA journal_mode=WAL——上面重试循环已尽力，
        # 重复调用会让"锁住也照样启动"的承诺失效（终审实测锁 45s 时在此抛出）。
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(DDL)
        self.conn.commit()
        # 早期版本在 (kw, md5) 上建了 UNIQUE：同一关键词里本来就可能有多张相同图片
        # （去重场景），撞唯一约束会让整批回填写入回滚。这里一次性纠正。
        # v2：image.weight（图片出现权重，默认 1，0 = 永不发送）
        columns = {
            row[1] for row in self.conn.execute("PRAGMA table_info(image)")
        }
        if "weight" not in columns:
            try:
                self.conn.execute(
                    "ALTER TABLE image ADD COLUMN weight INTEGER NOT NULL DEFAULT 1"
                )
            except sqlite3.OperationalError as exc:
                # 另一个进程刚好也在补列：duplicate column 视为"已存在"，继续走
                if "duplicate column" not in str(exc).lower():
                    raise
        self.conn.execute("DROP INDEX IF EXISTS ux_image_md5")
        self.conn.execute("CREATE INDEX IF NOT EXISTS ix_image_md5 ON image(md5)")
        # 版本号必须跟着写入：只补列不改版本会让下次误判"还是老库"
        self.conn.execute(
            "INSERT INTO meta(k, v) VALUES(?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            ("schema_version", str(SCHEMA_VERSION)),
        )
        self.conn.commit()

    # ---- 基础 ----
    def schema_version(self) -> int:
        row = self.conn.execute("SELECT v FROM meta WHERE k=?", ("schema_version",)).fetchone()
        try:
            return int(row["v"]) if row else 0
        except (TypeError, ValueError):
            return 0

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    def counts(self) -> Dict[str, int]:
        keywords = self.conn.execute("SELECT COUNT(*) AS c FROM keyword").fetchone()["c"]
        images = self.conn.execute("SELECT COUNT(*) AS c FROM image").fetchone()["c"]
        return {"keywords": int(keywords), "images": int(images)}

    def integrity(self) -> str:
        row = self.conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row else "unknown"

    # ---- 索引读写 ----
    def load_index(self) -> Dict[str, List[str]]:
        # LEFT JOIN：没有图片的关键词也要保留，否则重启后关键词凭空消失（P1 审计 G3）
        index: Dict[str, List[str]] = {}
        with self._lock:
            for row in self.conn.execute(
                "SELECT k FROM keyword ORDER BY k"
            ):
                index[row["k"]] = []
            for row in self.conn.execute(
                "SELECT kw, filename FROM image ORDER BY kw, id"
            ):
                index.setdefault(row["kw"], []).append(row["filename"])
        return index

    def sync_index(self, index: Dict[str, List[str]], md5_of=None) -> None:
        """把内存索引整体写进库（单事务）。

        md5_of: 可选的 `callable(kw, filename) -> (md5, bytes, ext)`，用于补全 MD5 列。
        数据量级（线上 2268 行）全量重写是毫秒级，比做 diff 更简单可靠。
        """
        with self._lock, self.conn:
            # 差量同步：绝不能 DELETE keyword——alias/alias_group 对它有外键级联，
            # 每存一次索引就把别名/管理员清空一次（P1 审计 G2）。
            wanted_kw = set(index)
            existing_kw = {
                row["k"] for row in self.conn.execute("SELECT k FROM keyword")
            }
            for stale in existing_kw - wanted_kw:
                self.conn.execute("DELETE FROM keyword WHERE k=?", (stale,))
            for kw, names in index.items():
                # 用 DO NOTHING 而不是 OR REPLACE：REPLACE 会先删后插，
                # 触发 alias/alias_group 的 ON DELETE CASCADE，把别名表清空。
                self.conn.execute(
                    "INSERT INTO keyword(k, blocked, created_at) VALUES(?, 0, ?) "
                    "ON CONFLICT(k) DO NOTHING",
                    (kw, 0),
                )
                for filename in names:
                    md5 = ext = None
                    size = None
                    if md5_of is not None:
                        try:
                            md5, size, ext = md5_of(kw, filename)
                        except Exception:
                            md5 = ext = size = None
                    # 不能用 INSERT OR REPLACE：那是先删后插，会把 weight 重置成 1
                    self.conn.execute(
                        "INSERT INTO image(kw, filename, md5, ext, bytes, created_at) "
                        "VALUES(?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(kw, filename) DO NOTHING",
                        (kw, filename, md5, ext, size, 0),
                    )
                    if md5 is not None:
                        self.conn.execute(
                            "UPDATE image SET md5=COALESCE(md5, ?), bytes=COALESCE(bytes, ?), "
                            "ext=COALESCE(ext, ?) WHERE kw=? AND filename=?",
                            (md5, size, ext, kw, filename),
                        )
                placeholders = ",".join("?" for _ in names) or "''"
                if names:
                    self.conn.execute(
                        f"DELETE FROM image WHERE kw=? AND filename NOT IN ({placeholders})",
                        (kw, *names),
                    )
                else:
                    self.conn.execute("DELETE FROM image WHERE kw=?", (kw,))

    def md5s_of(self, keyword: str) -> Dict[str, str]:
        rows = self.conn.execute(
            "SELECT filename, md5 FROM image WHERE kw=? AND md5 IS NOT NULL", (keyword,)
        )
        return {row["filename"]: row["md5"] for row in rows}

    def set_md5(self, keyword: str, filename: str, md5: str, size: Optional[int], ext: Optional[str]) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE image SET md5=?, bytes=?, ext=? WHERE kw=? AND filename=?",
                (md5, size, ext, keyword, filename),
            )

    def set_md5s(self, keyword: str, mapping: Dict[str, str]) -> None:
        """批量补 MD5（避免逐条事务）。"""
        if not mapping:
            return
        with self._lock, self.conn:
            for filename, digest in mapping.items():
                self.conn.execute(
                    "UPDATE image SET md5=? WHERE kw=? AND filename=?",
                    (digest, keyword, filename),
                )

    def weights_of(self, keyword: str) -> Dict[str, int]:
        """该关键词下 {文件名: 权重}；未设置的按 1 返回（列默认值即 1）。"""
        rows = self.conn.execute(
            "SELECT filename, weight FROM image WHERE kw=?", (keyword,)
        ).fetchall()
        # 不能用 weight or 1：0 是合法值（永不发送），会被 or 吞成 1
        return {
            row["filename"]: (1 if row["weight"] is None else int(row["weight"]))
            for row in rows
        }

    def weight_of(self, keyword: str, filename: str) -> int:
        row = self.conn.execute(
            "SELECT weight FROM image WHERE kw=? AND filename=?", (keyword, filename)
        ).fetchone()
        if not row:
            return 1
        return 1 if row["weight"] is None else int(row["weight"])

    def set_weight(self, keyword: str, filename: str, weight: int) -> bool:
        with self._lock, self.conn:
            cursor = self.conn.execute(
                "UPDATE image SET weight=? WHERE kw=? AND filename=?",
                (int(weight), keyword, filename),
            )
        return cursor.rowcount > 0

    def set_weights(self, keyword: str, mapping: Dict[str, int]) -> int:
        changed = 0
        with self._lock, self.conn:
            for filename, weight in mapping.items():
                cursor = self.conn.execute(
                    "UPDATE image SET weight=? WHERE kw=? AND filename=?",
                    (int(weight), keyword, filename),
                )
                changed += 1 if cursor.rowcount > 0 else 0
        return changed

    def reset_weights(self, keyword: Optional[str] = None) -> int:
        with self._lock, self.conn:
            if keyword:
                cursor = self.conn.execute(
                    "UPDATE image SET weight=1 WHERE kw=? AND weight<>1", (keyword,)
                )
            else:
                cursor = self.conn.execute("UPDATE image SET weight=1 WHERE weight<>1")
        return cursor.rowcount

    def export_weights(self) -> Dict[str, int]:
        """导出非默认权重：{"关键词/文件名": 权重}，用于备份。"""
        rows = self.conn.execute(
            "SELECT kw, filename, weight FROM image WHERE weight<>1"
        ).fetchall()
        return {f"{row['kw']}/{row['filename']}": int(row["weight"]) for row in rows}

    def import_weights(self, mapping: Dict[str, int]) -> int:
        applied = 0
        with self._lock, self.conn:
            for key, weight in (mapping or {}).items():
                key = str(key)
                if "/" not in key:
                    continue
                keyword, filename = key.split("/", 1)
                cursor = self.conn.execute(
                    "UPDATE image SET weight=? WHERE kw=? AND filename=?",
                    (int(weight), keyword, filename),
                )
                applied += 1 if cursor.rowcount > 0 else 0
        return applied

    def delete_by_md5(self, md5s: Iterable[str], keyword: Optional[str] = None) -> List[Tuple[str, str]]:
        wanted = [m for m in md5s if m]
        if not wanted:
            return []
        placeholders = ",".join("?" for _ in wanted)
        if keyword:
            rows = self.conn.execute(
                f"SELECT kw, filename FROM image WHERE md5 IN ({placeholders}) AND kw=?",
                (*wanted, keyword),
            ).fetchall()
        else:
            rows = self.conn.execute(
                f"SELECT kw, filename FROM image WHERE md5 IN ({placeholders})",
                tuple(wanted),
            ).fetchall()
        return [(row["kw"], row["filename"]) for row in rows]

    def remove_files(self, pairs: Iterable[Tuple[str, str]]) -> None:
        with self._lock, self.conn:
            for kw, filename in pairs:
                self.conn.execute("DELETE FROM image WHERE kw=? AND filename=?", (kw, filename))

    def clear_content(self) -> None:
        """清空贴纸内容，但保留 meta / admin / admin_audit。"""
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM image")
            self.conn.execute("DELETE FROM keyword")
            self.conn.execute("DELETE FROM alias")
            self.conn.execute("DELETE FROM alias_group")

    def prune_keyword(self, keyword: str, keep: Iterable[str]) -> None:
        keep_set = set(keep)
        with self._lock, self.conn:
            for row in self.conn.execute(
                "SELECT filename FROM image WHERE kw=?", (keyword,)
            ).fetchall():
                if row["filename"] not in keep_set:
                    self.conn.execute(
                        "DELETE FROM image WHERE kw=? AND filename=?", (keyword, row["filename"])
                    )

    # ---- 迁移 ----
    def migrate(self, index: Dict[str, List[str]], md5_of=None) -> Dict[str, int]:
        """幂等迁移：已有数据时只补差，返回值用于和 JSON 对账。"""
        if self.schema_version() < SCHEMA_VERSION:
            with self._lock, self.conn:
                self.conn.execute(
                    "INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)",
                    ("schema_version", str(SCHEMA_VERSION)),
                )
        self.sync_index(index, md5_of)
        return self.counts()