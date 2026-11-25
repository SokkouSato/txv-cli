#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""state_db.py —— 浏览数据/观看记录（SQLite，仅标准库）

只写本文件管理的时间线状态库（默认 timeline/data/state.db），**不碰 tweetxvault 的归档数据库**。

计数规则（与前端约定一致）
  * 「看过一条推文」：卡片在视口里停留够久（前端判定），上报后 seen_count +1，
    同一分钟内重复上报不会重复累加。
  * 「播放一次」：视频真的播起来时前端发 action=start，服务端用 instance_id 去重，
    因此刷新、重试、补报都不会把一次播放算成多次。
  * 「观看时长」：播放中每 5 秒上报一次增量，单次上报超过 60 秒会被截断（防脏数据）。

表结构
  feed_state      单行：上次看到哪条、筛选/排序/自动播放偏好
  tweets_seen     每条的看过次数与首末次时间
  media_plays     每个媒体（按 media_key）的播放次数、累计观看时长、断点位置
  play_events     播放明细（统计「最近看过」用）
  sessions        浏览会话（打开页面即一条）
  daily_activity  按天聚合（浏览柱状图用）
  favorites       本地收藏（和原站书签无关）
  local_likes     本地点赞（tweet_id + created_at；/api/likes 的唯一真相源）
  local_marks     v4 遗留的本地喜欢/转发标记；liked 列与 local_likes 保持同步
"""

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA_VERSION = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS feed_state (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    last_tweet_id TEXT,
    last_created  TEXT,
    last_seen_at  TEXT,
    filter        TEXT NOT NULL DEFAULT 'all',
    sort          TEXT NOT NULL DEFAULT 'created',
    autoplay      INTEGER NOT NULL DEFAULT 1,
    updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS tweets_seen (
    tweet_id      TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    seen_count    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS media_plays (
    media_key       TEXT PRIMARY KEY,
    tweet_id        TEXT,
    plays           INTEGER NOT NULL DEFAULT 0,
    watched_ms      INTEGER NOT NULL DEFAULT 0,
    last_pos_ms     INTEGER NOT NULL DEFAULT 0,
    duration_ms     INTEGER NOT NULL DEFAULT 0,
    first_played_at TEXT,
    last_played_at  TEXT,
    last_instance_id TEXT
);

CREATE TABLE IF NOT EXISTS play_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    media_key   TEXT NOT NULL,
    tweet_id    TEXT,
    instance_id TEXT,
    started_at  TEXT NOT NULL,
    watched_ms  INTEGER NOT NULL DEFAULT 0,
    pos_ms      INTEGER NOT NULL DEFAULT 0,
    counted     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    last_active_at TEXT,
    tweets_viewed INTEGER NOT NULL DEFAULT 0,
    plays         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS daily_activity (
    d          TEXT PRIMARY KEY,
    seen       INTEGER NOT NULL DEFAULT 0,
    plays      INTEGER NOT NULL DEFAULT 0,
    watched_ms INTEGER NOT NULL DEFAULT 0
);

-- 收藏夹：这是「我自己的收藏」，不属于浏览统计，「清空统计」不会动它
CREATE TABLE IF NOT EXISTS favorites (
    tweet_id TEXT PRIMARY KEY,
    added_at TEXT NOT NULL,
    note     TEXT
);

-- 本地点赞（和原站 like 无关）：/api/likes 的持久化载体，和 favorites 同级语义，
-- 同样属于「我自己的整理」，「清空统计」不会动它。
CREATE TABLE IF NOT EXISTS local_likes (
    tweet_id   TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

-- 本地喜欢 / 本地转发标记：同样是「我自己的整理」，清空统计不动它。
-- 只记录本地操作，和原站的 like/retweet 计数无关。
CREATE TABLE IF NOT EXISTS local_marks (
    tweet_id   TEXT PRIMARY KEY,
    liked      INTEGER NOT NULL DEFAULT 0,
    reposted   INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_play_events_started ON play_events(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_seen_last ON tweets_seen(last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_plays_count ON media_plays(plays DESC, watched_ms DESC);
CREATE INDEX IF NOT EXISTS idx_local_marks_liked ON local_marks(liked);
CREATE INDEX IF NOT EXISTS idx_local_marks_reposted ON local_marks(reposted);
CREATE INDEX IF NOT EXISTS idx_local_likes_created ON local_likes(created_at DESC);
"""

MAX_WATCHED_DELTA_MS = 60_000  # 单次上报的观看时长上限，防止脏数据把总量灌爆
MIN_PLAY_MS = 1000             # 累计观看满 1 秒才算「播放一次」（滚过去没看的不算）
SEEN_DEDUP_SECONDS = 60        # 同一分钟内重复上报「看过」不重复计数


def utc_now():
    return datetime.now(timezone.utc)


def iso(dt=None):
    return (dt or utc_now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def today():
    return utc_now().strftime("%Y-%m-%d")


def parse_iso(text):
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


class StateDB:
    """浏览数据存取。所有写操作串行化，读操作也走同一把锁（单用户本地场景足够）。"""

    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.lock = threading.RLock()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.conn = self._open()
        self._init_schema()

    # ---------- 基础设施 ----------

    def _open(self):
        try:
            conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            return conn
        except sqlite3.DatabaseError:
            # 文件损坏：改名备份后重建，绝不静默丢数据
            backup = f"{self.path}.corrupt-{utc_now().strftime('%Y%m%d%H%M%S')}"
            moved = []
            try:
                # -wal/-shm 也要一起挪走：里面可能有已提交但还没 checkpoint 的数据
                for suffix in ("", "-wal", "-shm"):
                    src = self.path + suffix
                    if os.path.exists(src):
                        os.replace(src, backup + suffix)
                        moved.append(suffix or ".db")
                if moved:
                    print(f"[state] 状态库损坏，已备份为 {backup}（{', '.join(moved)}）并重建")
            except OSError:
                pass
            conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            return conn

    def _init_schema(self):
        with self.lock:
            self.conn.executescript(SCHEMA)
            # 老库升级：v1 的 play_events 没有 counted 列
            columns = {r[1] for r in self.conn.execute("PRAGMA table_info(play_events)")}
            if "counted" not in columns:
                self.conn.execute("ALTER TABLE play_events ADD COLUMN counted INTEGER NOT NULL DEFAULT 0")
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self.conn.execute(
                "INSERT OR IGNORE INTO feed_state (id, filter, sort, autoplay, updated_at) VALUES (1, 'all', 'created', 1, ?)",
                (iso(),),
            )
            # v4 -> v5 迁移：v4 只有 local_marks.liked，把已有本地点赞补进 local_likes。
            # 幂等；之后两张表的 liked 状态由 set_local_like / set_local_mark 同步维护。
            self.conn.execute(
                "INSERT OR IGNORE INTO local_likes (tweet_id, created_at) "
                "SELECT tweet_id, coalesce(NULLIF(updated_at, ''), ?) FROM local_marks WHERE liked = 1",
                (iso(),),
            )
            self.conn.commit()

    def close(self):
        with self.lock:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass

    # ---------- 位置与偏好 ----------

    def get_feed_state(self):
        with self.lock:
            row = self.conn.execute("SELECT * FROM feed_state WHERE id = 1").fetchone()
        if not row:
            return {"last_tweet_id": None, "last_created": None, "last_seen_at": None,
                    "filter": "all", "sort": "created", "autoplay": True}
        return {
            "last_tweet_id": row["last_tweet_id"],
            "last_created": row["last_created"],
            "last_seen_at": row["last_seen_at"],
            "filter": row["filter"] or "all",
            "sort": row["sort"] or "created",
            "autoplay": bool(row["autoplay"]),
        }

    def set_feed_state(self, last_tweet_id=None, last_created=None, filter=None,
                       sort=None, autoplay=None):
        """只更新传进来的字段；滚动上报很频繁，所以走一条 UPDATE。"""
        fields, values = [], []
        if last_tweet_id is not None:
            fields.append("last_tweet_id = ?")
            values.append(last_tweet_id)
            fields.append("last_seen_at = ?")
            values.append(iso())
        if last_created is not None:
            fields.append("last_created = ?")
            values.append(last_created)
        if filter is not None:
            fields.append("filter = ?")
            values.append(filter)
        if sort is not None:
            fields.append("sort = ?")
            values.append(sort)
        if autoplay is not None:
            fields.append("autoplay = ?")
            values.append(1 if autoplay else 0)
        if not fields:
            return self.get_feed_state()
        fields.append("updated_at = ?")
        values.append(iso())
        with self.lock:
            self.conn.execute(f"UPDATE feed_state SET {', '.join(fields)} WHERE id = 1", values)
            self.conn.commit()
        return self.get_feed_state()

    # ---------- 会话 ----------

    def start_session(self):
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO sessions (started_at, last_active_at) VALUES (?, ?)", (iso(), iso()))
            self.conn.commit()
            return cur.lastrowid

    def touch_session(self, session_id, seen=0, plays=0):
        if not session_id:
            return
        with self.lock:
            self.conn.execute(
                "UPDATE sessions SET last_active_at = ?, tweets_viewed = tweets_viewed + ?, plays = plays + ? WHERE id = ?",
                (iso(), int(seen), int(plays), int(session_id)))
            self.conn.commit()

    # ---------- 看过 ----------

    def mark_seen(self, tweet_ids, session_id=None):
        if not tweet_ids:
            return {"ok": True, "seen_total": self.seen_count()}
        now = iso()
        day = today()
        added = 0
        with self.lock:
            for tweet_id in tweet_ids:
                row = self.conn.execute(
                    "SELECT last_seen_at FROM tweets_seen WHERE tweet_id = ?", (tweet_id,)).fetchone()
                if row is None:
                    self.conn.execute(
                        "INSERT INTO tweets_seen (tweet_id, first_seen_at, last_seen_at, seen_count) VALUES (?, ?, ?, 1)",
                        (tweet_id, now, now))
                    self._bump_daily(day, seen=1)
                    added += 1
                else:
                    last = parse_iso(row["last_seen_at"])
                    fresh = (last is None) or ((utc_now() - last).total_seconds() > SEEN_DEDUP_SECONDS)
                    if fresh:
                        self.conn.execute(
                            "UPDATE tweets_seen SET last_seen_at = ?, seen_count = seen_count + 1 WHERE tweet_id = ?",
                            (now, tweet_id))
                        if (last is None) or (last.strftime("%Y-%m-%d") != day):
                            self._bump_daily(day, seen=1)
                    else:
                        # 近距离重复上报：只刷新时间，不加计数
                        self.conn.execute(
                            "UPDATE tweets_seen SET last_seen_at = ? WHERE tweet_id = ?", (now, tweet_id))
            self.conn.commit()
        self.touch_session(session_id, seen=added)
        return {"ok": True, "seen_total": self.seen_count()}

    def seen_total(self):
        with self.lock:
            return self.conn.execute("SELECT count(*) FROM tweets_seen").fetchone()[0]

    seen_count = seen_total

    def seen_ids(self):
        with self.lock:
            return {r[0] for r in self.conn.execute("SELECT tweet_id FROM tweets_seen")}

    # ---------- 播放 ----------

    def record_play(self, media_key, tweet_id, instance_id, action,
                    watched_ms=0, pos_ms=None, duration_ms=None):
        """播放事件入库。

        计数规则：一次播放（一个 instance_id）必须**累计看满 MIN_PLAY_MS** 才算一次播放数。
        静音自动播放时快速滚过去（只播了零点几秒）不会把播放次数刷上去。
        """
        watched_ms = max(0, min(int(watched_ms or 0), MAX_WATCHED_DELTA_MS))
        now = iso()
        day = today()
        counted = False
        with self.lock:
            row = self.conn.execute("SELECT * FROM media_plays WHERE media_key = ?", (media_key,)).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO media_plays (media_key, tweet_id, plays, watched_ms, last_pos_ms, duration_ms, "
                    "first_played_at, last_played_at, last_instance_id) VALUES (?, ?, 0, 0, 0, 0, ?, ?, NULL)",
                    (media_key, tweet_id, now, now))
                row = self.conn.execute("SELECT * FROM media_plays WHERE media_key = ?", (media_key,)).fetchone()

            if action == "start" and (row["last_instance_id"] or "") != (instance_id or ""):
                # 换了 instance_id 才是新的一段播放；同一段播放反复 start 不会新开记录
                self.conn.execute(
                    "UPDATE media_plays SET last_instance_id = ?, last_played_at = ? WHERE media_key = ?",
                    (instance_id, now, media_key))
                self.conn.execute(
                    "INSERT INTO play_events (media_key, tweet_id, instance_id, started_at, watched_ms, pos_ms, counted) "
                    "VALUES (?, ?, ?, ?, 0, ?, 0)",
                    (media_key, tweet_id, instance_id, now, int(pos_ms or 0)))

            # 找这一段的明细行。没有 instance_id 时不去猜：NULL 比较恒为假，
            # 原来「退回最近一条」会把新一段的时长记到上一段头上。
            if action in ("start", "progress", "end"):
                event = None
                if instance_id:
                    event = self.conn.execute(
                        "SELECT id, watched_ms, counted FROM play_events WHERE media_key = ? AND instance_id = ? "
                        "ORDER BY id DESC LIMIT 1", (media_key, instance_id)).fetchone()
                if event is None and action in ("progress", "end"):
                    # start 丢包（beacon 抢先出网 / 网络失败）时补一条明细行，
                    # 否则这一段的观看时长会被静默丢弃、播放数也可能永远不到阈值
                    self.conn.execute(
                        "INSERT INTO play_events (media_key, tweet_id, instance_id, started_at, watched_ms, pos_ms, counted) "
                        "VALUES (?, ?, ?, ?, 0, 0, 0)", (media_key, tweet_id, instance_id, now))
                    event = self.conn.execute(
                        "SELECT id, watched_ms, counted FROM play_events WHERE media_key = ? AND instance_id IS ? "
                        "ORDER BY id DESC LIMIT 1", (media_key, instance_id)).fetchone()
                # 累计观看时长：不能依赖 event 是否存在
                if watched_ms:
                    self.conn.execute("UPDATE media_plays SET watched_ms = watched_ms + ? WHERE media_key = ?",
                                      (watched_ms, media_key))
                    self._bump_daily(day, watched_ms=watched_ms)
                    if event:
                        self.conn.execute("UPDATE play_events SET watched_ms = watched_ms + ? WHERE id = ?",
                                          (watched_ms, event["id"]))
                        event = self.conn.execute(
                            "SELECT id, watched_ms, counted FROM play_events WHERE id = ?", (event["id"],)).fetchone()
                # 看满 1 秒才记一次播放（每段播放最多记一次）
                if event and not event["counted"] and event["watched_ms"] >= MIN_PLAY_MS:
                    self.conn.execute("UPDATE play_events SET counted = 1 WHERE id = ?", (event["id"],))
                    self.conn.execute("UPDATE media_plays SET plays = plays + 1 WHERE media_key = ?", (media_key,))
                    self._bump_daily(day, plays=1)
                    counted = True

            # 位置与时长：pos / progress / end 刷新位置；start 只补时长，绝不用 0 覆盖已有断点
            if action in ("pos", "progress", "end", "start"):
                sets, vals = [], []
                if pos_ms is not None and action in ("pos", "progress", "end"):
                    pos = max(0, int(pos_ms))
                    if duration_ms:                      # 有总长就钳到总长，否则断点会出现「从 277:46:39 继续」
                        pos = min(pos, max(0, int(duration_ms)))
                    sets.append("last_pos_ms = ?")
                    vals.append(pos)
                if duration_ms is not None and int(duration_ms) > 0:
                    sets.append("duration_ms = ?")
                    vals.append(int(duration_ms))
                if sets:
                    sets.append("last_played_at = ?")
                    vals.append(now)
                    vals.append(media_key)
                    self.conn.execute(f"UPDATE media_plays SET {', '.join(sets)} WHERE media_key = ?", vals)
                if action == "end" and pos_ms is not None and duration_ms:
                    # 快看完了就把断点清零，下次不会提示「继续」
                    if int(duration_ms) - int(pos_ms) < 10_000:
                        self.conn.execute("UPDATE media_plays SET last_pos_ms = 0 WHERE media_key = ?", (media_key,))
            self.conn.commit()
            row = self.conn.execute("SELECT plays, watched_ms, last_pos_ms FROM media_plays WHERE media_key = ?",
                                    (media_key,)).fetchone()
        if counted:
            self.touch_session(None, plays=1)
        return {"ok": True, "plays": row["plays"], "watched_ms": row["watched_ms"],
                "last_pos_ms": row["last_pos_ms"]}

    def plays_map(self):
        with self.lock:
            return {r["media_key"]: r["plays"] for r in
                    self.conn.execute("SELECT media_key, plays FROM media_plays WHERE plays > 0")}

    def positions_map(self):
        with self.lock:
            return {r["media_key"]: r["last_pos_ms"] for r in
                    self.conn.execute("SELECT media_key, last_pos_ms FROM media_plays WHERE last_pos_ms > 0")}

    # ---------- 收藏夹 ----------

    def favorite_ids(self):
        with self.lock:
            return [r["tweet_id"] for r in
                    self.conn.execute("SELECT tweet_id FROM favorites ORDER BY added_at DESC")]

    def favorite_total(self):
        with self.lock:
            return self.conn.execute("SELECT count(*) FROM favorites").fetchone()[0]

    def set_favorite(self, tweet_id, action="toggle", note=None):
        """收藏 / 取消收藏。action: add / remove / toggle，返回 {ok, favorited, total}"""
        if not tweet_id:
            raise ValueError("缺少 tweet_id")
        with self.lock:
            exists = self.conn.execute(
                "SELECT 1 FROM favorites WHERE tweet_id = ?", (tweet_id,)).fetchone() is not None
            if action == "toggle":
                action = "remove" if exists else "add"
            if action == "add":
                self.conn.execute(
                    "INSERT INTO favorites (tweet_id, added_at, note) VALUES (?, ?, ?) "
                    "ON CONFLICT(tweet_id) DO UPDATE SET note = coalesce(excluded.note, favorites.note)",
                    (tweet_id, iso(), note))
                favorited = True
            else:
                self.conn.execute("DELETE FROM favorites WHERE tweet_id = ?", (tweet_id,))
                favorited = False
            total = self.conn.execute("SELECT count(*) FROM favorites").fetchone()[0]
            self.conn.commit()
        return {"ok": True, "favorited": favorited, "total": total}

    # ---------- 本地点赞（local_likes） ----------

    def _set_local_likes_row(self, tweet_id, liked, now=None):
        """写入 local_likes 单行（调用方必须已持有 self.lock）。"""
        now = now or iso()
        if liked:
            # 重复点赞只更新一次时间：created_at 保持第一次点赞的时间
            self.conn.execute(
                "INSERT INTO local_likes (tweet_id, created_at) VALUES (?, ?) "
                "ON CONFLICT(tweet_id) DO NOTHING", (tweet_id, now))
        else:
            self.conn.execute("DELETE FROM local_likes WHERE tweet_id = ?", (tweet_id,))

    def _sync_local_mark_like(self, tweet_id, liked, now=None):
        """把点赞状态同步到 v4 遗留的 local_marks.liked（调用方持有 self.lock）。

        local_marks 已没有接口在读，但 /api/state/export 还会导出它；
        两边保持一致，避免同一件事在导出里出现两种答案。
        """
        now = now or iso()
        if liked:
            self.conn.execute(
                "INSERT INTO local_marks (tweet_id, liked, reposted, updated_at) "
                "VALUES (?, 1, 0, ?) "
                "ON CONFLICT(tweet_id) DO UPDATE SET liked = 1, updated_at = excluded.updated_at",
                (tweet_id, now))
        else:
            self.conn.execute(
                "UPDATE local_marks SET liked = 0, updated_at = ? WHERE tweet_id = ?",
                (now, tweet_id))
            # 取消点赞只删「本来就没有转发标记」的行
            self.conn.execute(
                "DELETE FROM local_marks WHERE tweet_id = ? AND liked = 0 AND reposted = 0",
                (tweet_id,))

    def list_local_likes(self):
        """本地点赞的 tweet_id 列表，最近点赞的在前。"""
        with self.lock:
            return [r[0] for r in self.conn.execute(
                "SELECT tweet_id FROM local_likes "
                "ORDER BY created_at DESC, tweet_id DESC")]

    def is_local_like(self, tweet_id):
        if not tweet_id:
            return False
        with self.lock:
            return self.conn.execute(
                "SELECT 1 FROM local_likes WHERE tweet_id = ?", (tweet_id,)).fetchone() is not None

    def local_like_total(self):
        with self.lock:
            return self.conn.execute("SELECT count(*) FROM local_likes").fetchone()[0]

    def set_local_like(self, tweet_id, liked=True):
        """本地点赞 / 取消点赞。返回 {ok, tweet_id, liked, total}，幂等。"""
        if not tweet_id:
            raise ValueError("缺少 tweet_id")
        tweet_id = str(tweet_id)
        with self.lock:
            now = iso()
            self._set_local_likes_row(tweet_id, bool(liked), now)
            self._sync_local_mark_like(tweet_id, bool(liked), now)
            total = self.conn.execute("SELECT count(*) FROM local_likes").fetchone()[0]
            self.conn.commit()
        return {"ok": True, "tweet_id": tweet_id, "liked": bool(liked), "total": total}

    # ---------- 本地喜欢 / 本地转发标记（v4 遗留） ----------

    MARK_KINDS = {"like": "liked", "repost": "reposted"}

    def local_marks_map(self):
        """{tweet_id: {liked: bool, reposted: bool}}，只含至少一项为真的行。"""
        with self.lock:
            rows = self.conn.execute(
                "SELECT tweet_id, liked, reposted FROM local_marks"
            ).fetchall()
        return {
            r["tweet_id"]: {"liked": bool(r["liked"]), "reposted": bool(r["reposted"])}
            for r in rows
        }

    def local_mark_ids(self, kind="like"):
        column = self.MARK_KINDS.get(kind)
        if not column:
            raise ValueError("kind 只能是 like/repost")
        with self.lock:
            return {r[0] for r in self.conn.execute(
                f"SELECT tweet_id FROM local_marks WHERE {column} = 1")}

    def local_mark_total(self, kind=None):
        with self.lock:
            if kind:
                column = self.MARK_KINDS.get(kind)
                if not column:
                    raise ValueError("kind 只能是 like/repost")
                return self.conn.execute(
                    f"SELECT count(*) FROM local_marks WHERE {column} = 1").fetchone()[0]
            return self.conn.execute(
                "SELECT count(*) FROM local_marks WHERE liked = 1 OR reposted = 1"
            ).fetchone()[0]

    def set_local_mark(self, tweet_id, kind="like", action="toggle"):
        """本地喜欢/转发标记的增删。返回 {ok, kind, active, marks:{liked,reposted}}。"""
        if not tweet_id:
            raise ValueError("缺少 tweet_id")
        column = self.MARK_KINDS.get(kind)
        if not column:
            raise ValueError("kind 只能是 like/repost")
        if action not in ("add", "remove", "toggle"):
            raise ValueError("action 只能是 add/remove/toggle")
        with self.lock:
            row = self.conn.execute(
                "SELECT liked, reposted FROM local_marks WHERE tweet_id = ?",
                (tweet_id,)).fetchone()
            current = bool(row[column]) if row else False
            if action == "toggle":
                action = "remove" if current else "add"
            value = 1 if action == "add" else 0
            if row is None:
                if value:
                    self.conn.execute(
                        "INSERT INTO local_marks (tweet_id, liked, reposted, updated_at) "
                        "VALUES (?, ?, ?, ?)",
                        (tweet_id, value if column == "liked" else 0,
                         value if column == "reposted" else 0, iso()))
            else:
                self.conn.execute(
                    f"UPDATE local_marks SET {column} = ?, updated_at = ? WHERE tweet_id = ?",
                    (value, iso(), tweet_id))
                self.conn.execute(
                    "DELETE FROM local_marks WHERE tweet_id = ? AND liked = 0 AND reposted = 0",
                    (tweet_id,))
            if column == "liked":
                # v4 的 local_marks 与 v5 的 local_likes 是同一件事，保持同步
                self._set_local_likes_row(tweet_id, bool(value))
            self.conn.commit()
            kept = self.conn.execute(
                "SELECT liked, reposted FROM local_marks WHERE tweet_id = ?",
                (tweet_id,)).fetchone()
        marks = {
            "liked": bool(kept["liked"]) if kept else False,
            "reposted": bool(kept["reposted"]) if kept else False,
        }
        return {"ok": True, "kind": kind, "active": bool(value), "marks": marks}

    def import_local_marks(self, mapping, replace=False):
        """批量导入旧 localStorage 里的标记。replace=True 时先清空（用于导入文件）。"""
        if not isinstance(mapping, dict):
            raise ValueError("marks 必须是对象")
        imported = 0
        now = iso()
        with self.lock:
            if replace:
                self.conn.execute("DELETE FROM local_marks")
                # 点赞是同一件事：整表替换时 local_likes 也一起换掉
                self.conn.execute("DELETE FROM local_likes")
            for tweet_id, mark in mapping.items():
                if not tweet_id or not isinstance(mark, dict):
                    continue
                liked = 1 if mark.get("liked") else 0
                reposted = 1 if mark.get("reposted") else 0
                if not liked and not reposted:
                    continue
                self.conn.execute(
                    "INSERT INTO local_marks (tweet_id, liked, reposted, updated_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(tweet_id) DO UPDATE SET "
                    "liked = max(liked, excluded.liked), "
                    "reposted = max(reposted, excluded.reposted), "
                    "updated_at = excluded.updated_at",
                    (str(tweet_id), liked, reposted, now))
                if liked:
                    # 导入的本地喜欢同时写进 local_likes（只增不减，保留首次点赞时间）
                    self._set_local_likes_row(str(tweet_id), True, now)
                imported += 1
            self.conn.commit()
        return {"ok": True, "imported": imported, "total": self.local_mark_total()}

    # ---------- 每日聚合 ----------

    def _bump_daily(self, day, seen=0, plays=0, watched_ms=0):
        self.conn.execute(
            "INSERT INTO daily_activity (d, seen, plays, watched_ms) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(d) DO UPDATE SET seen = seen + excluded.seen, plays = plays + excluded.plays, "
            "watched_ms = watched_ms + excluded.watched_ms",
            (day, int(seen), int(plays), int(watched_ms)))

    # ---------- 统计 ----------

    def stats(self, card_lookup=None, media_lookup=None, top_limit=10, recent_limit=20, days=60):
        """card_lookup: tweet_id -> {handle,name,text,created,poster}
           media_lookup: media_key -> {tweet_id,kind,poster,src,handle,name,text,created}"""
        card_lookup = card_lookup or {}
        media_lookup = media_lookup or {}
        with self.lock:
            totals_row = self.conn.execute(
                "SELECT count(*) AS seen, "
                "(SELECT coalesce(sum(plays),0) FROM media_plays) AS plays, "
                "(SELECT coalesce(sum(watched_ms),0) FROM media_plays) AS watched, "
                "(SELECT count(*) FROM sessions) AS sessions, "
                "(SELECT max(last_seen_at) FROM tweets_seen) AS last_seen, "
                "(SELECT coalesce(sum(seen),0) FROM daily_activity WHERE d = ?) AS seen_today, "
                "(SELECT count(*) FROM media_plays WHERE plays > 0) AS watched_media, "
                "(SELECT count(*) FROM favorites) AS favorites, "
                "(SELECT count(*) FROM local_likes) AS local_likes "
                "FROM tweets_seen", (today(),)).fetchone()

            daily = [dict(r) for r in self.conn.execute(
                "SELECT d, seen, plays, watched_ms FROM daily_activity ORDER BY d DESC LIMIT ?", (days,))]
            daily.reverse()

            top_rows = [dict(r) for r in self.conn.execute(
                "SELECT media_key, tweet_id, plays, watched_ms, last_pos_ms, duration_ms, last_played_at "
                "FROM media_plays WHERE plays > 0 ORDER BY plays DESC, watched_ms DESC LIMIT ?", (top_limit,))]

            recent_rows = [dict(r) for r in self.conn.execute(
                "SELECT tweet_id, last_seen_at, seen_count FROM tweets_seen "
                "ORDER BY last_seen_at DESC LIMIT ?", (recent_limit,))]

        def card_brief(tweet_id):
            info = card_lookup.get(tweet_id) or {}
            return {
                "tweet_id": tweet_id,
                "handle": info.get("handle"),
                "name": info.get("name"),
                "text": (info.get("text") or "")[:80],
                "created": info.get("created"),
                "avatar": info.get("avatar"),
                "poster": info.get("poster"),
            }

        top_media = []
        for row in top_rows:
            info = media_lookup.get(row["media_key"]) or {}
            brief = card_brief(info.get("tweet_id") or row["tweet_id"])
            top_media.append({
                "media_key": row["media_key"],
                "tweet_id": info.get("tweet_id") or row["tweet_id"],
                "plays": row["plays"],
                "watched_ms": row["watched_ms"],
                "last_pos_ms": row["last_pos_ms"],
                "duration_ms": row["duration_ms"],
                "kind": info.get("kind") or "video",
                "poster": info.get("poster"),
                "src": info.get("src"),
                "handle": brief["handle"],
                "name": brief["name"],
                "text": brief["text"],
                "created": brief["created"],
            })

        recent = []
        for row in recent_rows:
            brief = card_brief(row["tweet_id"])
            brief["last_seen_at"] = row["last_seen_at"]
            brief["seen_count"] = row["seen_count"]
            recent.append(brief)

        return {
            "totals": {
                "seen_tweets": totals_row["seen"],
                "plays": totals_row["plays"],
                "watched_ms": totals_row["watched"],
                "sessions": totals_row["sessions"],
                "last_seen_at": totals_row["last_seen"],
                "seen_today": totals_row["seen_today"],
                "watched_media": totals_row["watched_media"],
                "favorites": totals_row["favorites"],
                "local_likes": totals_row["local_likes"],
            },
            "daily": daily,
            "top_media": top_media,
            "recent": recent,
        }

    # ---------- 导出 / 清空 ----------

    def export(self):
        with self.lock:
            data = {"exported_at": iso(), "schema_version": SCHEMA_VERSION}
            for table in ("feed_state", "tweets_seen", "media_plays", "sessions",
                          "daily_activity", "favorites", "local_likes", "local_marks"):
                data[table] = [dict(r) for r in self.conn.execute(f"SELECT * FROM {table}")]
            data["play_events_count"] = self.conn.execute("SELECT count(*) FROM play_events").fetchone()[0]
        return data

    def reset(self):
        """清空浏览统计；保留 feed_state（你看到哪了）、favorites、local_likes 和 local_marks。"""
        with self.lock:
            for table in ("tweets_seen", "media_plays", "play_events", "sessions", "daily_activity"):
                self.conn.execute(f"DELETE FROM {table}")
            self.conn.commit()
        return {"ok": True}


if __name__ == "__main__":
    # 自测：临时库跑一遍全部接口
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        db = StateDB(os.path.join(tmp, "state.db"))
        sid = db.start_session()
        db.set_feed_state(last_tweet_id="t1", filter="video", autoplay=False)
        db.mark_seen(["t1", "t2"], sid)
        db.mark_seen(["t1"], sid)                     # 一分钟内重复，不该重复计数
        assert db.seen_total() == 2, db.seen_total()
        first = db.record_play("m1", "t1", "inst-1", "start", pos_ms=0, duration_ms=60000)
        assert first["plays"] == 0, first                      # 刚开播还不算一次
        short = db.record_play("m1", "t1", "inst-1", "progress", watched_ms=300, pos_ms=300, duration_ms=60000)
        assert short["plays"] == 0, short                      # 只看了 0.3 秒，不算
        one = db.record_play("m1", "t1", "inst-1", "progress", watched_ms=900, pos_ms=1200, duration_ms=60000)
        assert one["plays"] == 1, one                          # 累计 1.2 秒 → 计 1 次
        same = db.record_play("m1", "t1", "inst-1", "progress", watched_ms=5000, pos_ms=6200, duration_ms=60000)
        assert same["plays"] == 1, same                        # 同一段播放不重复计
        db.record_play("m1", "t1", "inst-2", "start", pos_ms=0, duration_ms=60000)
        second = db.record_play("m1", "t1", "inst-2", "progress", watched_ms=1500, pos_ms=1500, duration_ms=60000)
        assert second["plays"] == 2, second                    # 新的一段播放 → 第 2 次
        end = db.record_play("m1", "t1", "inst-2", "end", watched_ms=1000, pos_ms=59000, duration_ms=60000)
        assert end["watched_ms"] == 8700, end                  # 300+900+5000+1500+1000
        assert db.positions_map() == {}, db.positions_map()    # 快看完 → 断点清零
        st = db.stats()
        assert st["totals"]["plays"] == 2 and st["totals"]["seen_tweets"] == 2, st["totals"]
        assert st["daily"][-1]["plays"] == 2, st["daily"]
        assert db.export()["play_events_count"] == 2

        # start 丢包（beacon 抢先出网）时，观看时长不能被静默丢弃
        lost = db.record_play("m2", "t2", "inst-9", "progress", watched_ms=4000, pos_ms=4000, duration_ms=60000)
        assert lost["watched_ms"] == 4000, lost                # 没有 start 也要累加时长
        assert lost["plays"] == 1, lost                        # 明明看了 4 秒，就该算一次播放
        # 断点不能被超出总长的 pos 顶飞
        far = db.record_play("m2", "t2", "inst-9", "progress", watched_ms=0, pos_ms=999999999, duration_ms=30000)
        assert far["last_pos_ms"] == 30000, far

        # 收藏夹
        assert db.favorite_total() == 0
        assert db.set_favorite("t1", "add") == {"ok": True, "favorited": True, "total": 1}
        assert db.set_favorite("t1", "add")["total"] == 1        # 重复收藏不会多加一条
        assert db.set_favorite("t2", "toggle")["favorited"] is True
        assert db.set_favorite("t2", "toggle")["favorited"] is False
        assert sorted(db.favorite_ids()) == ["t1"], db.favorite_ids()
        assert db.stats()["totals"]["favorites"] == 1, db.stats()["totals"]
        assert len(db.export()["favorites"]) == 1

        # 本地喜欢 / 本地转发标记
        assert db.local_mark_total() == 0
        first = db.set_local_mark("t1", "like", "toggle")
        assert first["active"] is True and first["marks"] == {"liked": True, "reposted": False}, first
        assert db.set_local_mark("t1", "like", "toggle")["marks"]["liked"] is False
        assert db.local_mark_total() == 0                       # 两项都归零的行会被删掉
        db.set_local_mark("t1", "like", "add")
        db.set_local_mark("t1", "repost", "add")
        assert db.local_mark_total() == 1
        assert db.local_mark_ids("like") == {"t1"}
        assert db.local_mark_ids("repost") == {"t1"}
        assert len(db.export()["local_marks"]) == 1
        db.import_local_marks({"t2": {"liked": True}, "t3": {"reposted": True}})
        assert db.local_mark_total() == 3, db.local_mark_total()
        assert db.local_marks_map()["t2"] == {"liked": True, "reposted": False}

        # 本地点赞（v5 主存储，与 v4 的 local_marks.liked 同步）
        assert set(db.list_local_likes()) == {"t1", "t2"}, db.list_local_likes()
        assert db.is_local_like("t1") and db.is_local_like("t2")
        assert not db.is_local_like("t3") and not db.is_local_like("")
        assert db.local_like_total() == 2
        first = db.set_local_like("t4", True)
        assert first["liked"] is True and first["total"] == 3, first
        assert db.set_local_like("t4", True)["total"] == 3      # 重复点赞幂等
        assert db.local_marks_map()["t4"]["liked"] is True      # 同步到遗留表
        assert db.set_local_like("t1", False)["total"] == 2
        assert not db.is_local_like("t1")
        assert db.local_marks_map()["t1"] == {"liked": False, "reposted": True}
        assert db.set_local_like("t2", False)["total"] == 1
        assert "t2" not in db.local_marks_map()                # liked 归零且无转发 → 行删除
        assert len(db.export()["local_likes"]) == 1
        assert db.stats()["totals"]["local_likes"] == 1

        db.reset()
        assert db.stats()["totals"]["plays"] == 0
        assert db.get_feed_state()["last_tweet_id"] == "t1"   # 位置保留
        assert db.favorite_total() == 1                       # 清空统计不动收藏夹
        assert db.local_mark_total() == 3                     # 清空统计不动本地标记
        assert db.local_like_total() == 1                      # 清空统计不动本地点赞

        # 整表导入：replace=True 时 local_likes 与 local_marks 一起替换
        db.import_local_marks({"t5": {"liked": True}, "t6": {"reposted": True}}, replace=True)
        assert db.list_local_likes() == ["t5"], db.list_local_likes()
        assert db.local_mark_ids("like") == {"t5"}
        db.close()
        print("state_db 自测通过")
