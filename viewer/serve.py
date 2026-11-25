#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""serve.py —— 本地推文时间线服务（Python 标准库，无第三方依赖）

职责
    * 提供前端静态页（web/index.html、app.css、app.js、theme.js）
    * /api/feed   分页时间线（四维正交筛选：媒体类型 × 来源 × 阅读状态 × 账号，
                  兼容旧 filter 参数；筛选/排序/搜索/合并浏览数据）
    * /api/media  分页媒体墙（类型筛选、搜索、排序）
    * /api/likes  本地点赞读写（POST 点赞/取消，GET 列表；只动 state.db）
    * /api/tweet  推文详情（父级、引用、线程子推文）
    * /api/author 作者汇总（推文数、媒体数、时间范围、简介/粉丝/加入/认证）
    * /api/state* 浏览数据读写（位置、看过、播放次数、断点）
    * /api/stats  观看统计
    * /api/cache/clean  只清理 store/.tmp 下的 *.part 残留（不允许其它路径）
    * /api/store/gc     调用 tools/store_v3.py --gc 回收无效旧档（有 store 锁）
    * /media      媒体墙 SPA 页面（与 /media/* 文件路由区分）
    * /tweet/*    推文详情 SPA 页面
    * /author/*   作者页 SPA 页面
    * /media/*    媒体文件流式送出，**支持 HTTP Range**（视频拖进度条依赖它）

数据来源
    * data/timeline.ndjson + data/meta.json（由 export_timeline.py 生成，只读）
    * data/state.db（由 state_db.py 管理，只写它）
    * 归档 media 目录（默认 ~/.local/share/tweetxvault/media，只读）

用法
    python3 serve.py                 # 默认 0.0.0.0:8787
    python3 serve.py --port 9000 --vault /mnt/f/Download/tx/tweetxvault
"""

import argparse
import datetime
import json
import mimetypes
import os
import posixpath
import re
import socket
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import state_db  # noqa: E402  （同目录）

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
DEFAULT_DATA_DIR = os.path.join(BASE_DIR, "data")
VIDEO_TYPES = ("video", "animated_gif")
FILTERS = ("all", "video", "photo", "text", "unseen", "seen", "fav", "bookmark", "like")
# 四维正交筛选（旧 filter 通过 LEGACY_FILTERS 映射到这些参数）
MEDIA_TYPES = ("all", "photo", "video", "text")
SOURCE_FILTERS = ("all", "x_bookmark", "x_like", "local_fav", "local_like")
READ_STATES = ("all", "unseen", "seen")
# 旧 filter -> 新参数；只在新参数没显式出现时生效，避免两套参数打架
LEGACY_FILTERS = {
    "photo": ("media_type", "photo"),
    "video": ("media_type", "video"),
    "text": ("media_type", "text"),
    "unseen": ("read_state", "unseen"),
    "seen": ("read_state", "seen"),
    "fav": ("source", "local_fav"),
    "bookmark": ("source", "x_bookmark"),
    "like": ("source", "x_like"),
}
MEDIA_FILTERS = ("all", "photo", "video", "gif")
# 排序只保留：发布时间 / 最多喜欢 / 最多查看（"saved" 归档时间已下线）
SORTS = ("created", "likes", "views")
CHUNK = 1024 * 1024
MAX_BODY = 1 << 20  # 1 MB，POST body 上限
STORE_GC_TIMEOUT = 900  # 秒；--gc 是两阶段删除，数据大时可能跑一会儿
STORE_LOCK_BUSY_EXIT = 4  # store_v3.py 的「锁被占用」退出码

CONTENT_TYPES = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".webm": "video/webm", ".mkv": "video/x-matroska",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp",
}


def log(*parts):
    print("[serve]", *parts, flush=True)


def query_date(query, name):
    raw = (query.get(name, [""])[0] or "").strip()
    if not raw:
        return None
    try:
        return datetime.date.fromisoformat(raw).isoformat()
    except ValueError:
        return None


def query_nonneg(query, name):
    try:
        value = int(query.get(name, ["0"])[0])
    except (TypeError, ValueError):
        return 0
    return max(0, min(10 ** 9, value))


def _looks_like_store(path):
    """store 根目录的判定：有 media.db 或 .tmp 才算，避免认错目录后误删。"""
    if not path or not os.path.isdir(path):
        return False
    return os.path.isfile(os.path.join(path, "media.db")) or os.path.isdir(
        os.path.join(path, ".tmp")
    )


def resolve_store_root(explicit, data_dir):
    """找归档 store 根目录（只用于 /api/cache/clean 与 /api/store/gc）。

    顺序：--store 参数 > 环境变量 > 数据目录的兄弟/父级兄弟 > 本仓库默认布局。
    找不到就返回 None —— 两个维护接口会直接报错，绝不猜路径去删文件。
    """
    candidates = []
    if explicit:
        candidates.append(os.path.expanduser(explicit))
    for env_name in ("TXV_STORE", "TWEETXVAULT_STORE"):
        if os.environ.get(env_name):
            candidates.append(os.path.expanduser(os.environ[env_name]))
    viewer_dir = os.path.dirname(data_dir)          # ...\viewer
    project_dir = os.path.dirname(viewer_dir)       # ...\txv
    candidates.extend([
        os.path.join(project_dir, "store"),
        os.path.join(viewer_dir, "store"),
        os.path.join(BASE_DIR, "store"),
    ])
    for candidate in candidates:
        full = os.path.realpath(candidate)
        if _looks_like_store(full):
            return full
    return None


def resolve_store_script(store_root, explicit, data_dir):
    """找 tools/store_v3.py；只执行归档自带的维护脚本，不接受任意路径。"""
    candidates = []
    if explicit:
        candidates.append(os.path.expanduser(explicit))
    if os.environ.get("TXV_STORE_PY"):
        candidates.append(os.path.expanduser(os.environ["TXV_STORE_PY"]))
    viewer_dir = os.path.dirname(data_dir)
    project_dir = os.path.dirname(viewer_dir)
    candidates.extend([
        os.path.join(project_dir, "tools", "store_v3.py"),
        os.path.join(os.path.dirname(project_dir), "tools", "store_v3.py"),
        os.path.join(BASE_DIR, "tools", "store_v3.py"),
    ])
    if store_root:
        candidates.append(os.path.join(os.path.dirname(store_root), "tools", "store_v3.py"))
    for candidate in candidates:
        full = os.path.realpath(candidate)
        if os.path.isfile(full):
            return full
    return None


def human_bytes(value):
    """字节数转人类可读（前端统计页也用同一套进位）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if number <= 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    index = 0
    while number >= 1024 and index < len(units) - 1:
        number /= 1024.0
        index += 1
    if index == 0:
        return f"{int(number)} B"
    return f"{number:.1f} {units[index]}"


class StoreLockBusy(RuntimeError):
    """store/.lock 被另一个进程（下载/回收）占着。"""


class StoreFileLock:
    """和 tools/store_v3.py 用同一把 store/.lock 的进程锁。

    清理 .tmp 前必须拿到它：否则可能删掉正在下载的 *.part。
    Windows 用 msvcrt 锁字节 0，POSIX 用 flock，都是非阻塞的。
    """

    def __init__(self, store_root):
        self.path = os.path.join(store_root, ".lock")
        self.handle = None

    def __enter__(self):
        try:
            self.handle = open(self.path, "a+b")
        except OSError as exc:
            raise RuntimeError(f"无法打开 store/.lock：{exc}") from exc
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise StoreLockBusy(
                "归档存储正在写入（store/.lock 被占用），稍后再清理临时残留") from exc
        return self

    def __exit__(self, *_exc):
        handle = self.handle
        self.handle = None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                handle.close()
            except OSError:
                pass


class Timeline:
    """内存里的时间线数据 + 浏览数据访问。"""

    def __init__(self, data_dir, vault, state_path, store_root=None, store_script=None):
        self.data_dir = data_dir
        self.vault = vault
        self.media_root = os.path.realpath(os.path.join(vault, "media"))
        self.state = state_db.StateDB(state_path)
        # 维护操作（清理 .tmp / store --gc）的目标；None 表示找不到，接口直接拒绝
        self.store_root = store_root
        self.store_script = store_script

        ndjson_path = os.path.join(data_dir, "timeline.ndjson")
        meta_path = os.path.join(data_dir, "meta.json")
        if not os.path.exists(ndjson_path):
            log(f"[错误] 找不到 {ndjson_path}，先运行 export_timeline.py")
            raise SystemExit(2)
        if os.path.getsize(ndjson_path) == 0:
            log(f"[错误] {ndjson_path} 是空文件，重新运行 export_timeline.py")
            raise SystemExit(2)

        self.cards = []
        with open(ndjson_path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    self.cards.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    # 直接抛栈的话，用户看到的只是「窗口闪一堆红字」，完全不知道哪一行坏了
                    log(f"[错误] {ndjson_path} 第 {lineno} 行不是合法 JSON：{exc}")
                    log("[错误] 文件可能被截断或写坏，请重新运行 export_timeline.py")
                    raise SystemExit(2) from None

        try:
            with open(meta_path, encoding="utf-8") as fh:
                self.meta = json.load(fh)
        except FileNotFoundError:
            log(f"[错误] 找不到 {meta_path}，先运行 export_timeline.py")
            raise SystemExit(2) from None
        except json.JSONDecodeError as exc:
            log(f"[错误] {meta_path} 不是合法 JSON：{exc}，先重新运行 export_timeline.py")
            raise SystemExit(2) from None

        self.objects = []
        objects_path = os.path.join(data_dir, "objects.ndjson")
        if os.path.exists(objects_path):
            with open(objects_path, encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    if not line.strip():
                        continue
                    try:
                        self.objects.append(json.loads(line))
                    except json.JSONDecodeError as exc:
                        log(f"[错误] {objects_path} 第 {lineno} 行不是合法 JSON：{exc}")
                        raise SystemExit(2) from None
        self.card_by_id = {c["id"]: c for c in self.cards}
        self.object_by_id = {c["id"]: c for c in self.objects}
        # 详情/关系查找优先用时间线卡片，缺失时回退到完整对象池。
        self.all_by_id = dict(self.object_by_id)
        self.all_by_id.update(self.card_by_id)
        self.all_cards = list(self.all_by_id.values())
        self.media_lookup = {}
        self.card_lookup = {}
        for card in self.cards:
            first = card["media"][0] if card["media"] else None
            self.card_lookup[card["id"]] = {
                "handle": card.get("handle"),
                "name": card.get("name"),
                "text": card.get("text"),
                "created": card.get("created"),
                "avatar": card.get("avatar"),
                "poster": (first or {}).get("poster") or (first or {}).get("src"),
            }
            for media in card["media"]:
                self.media_lookup[media.get("key")] = {
                    "tweet_id": card["id"],
                    "kind": media.get("t"),
                    "poster": media.get("poster") or media.get("src"),
                    "src": media.get("src"),
                }

        # 预计算筛选分组（数据在运行期不变）。photo/video/text 互斥，
        # 这样 media_type 的计数与 /api/feed 分页 total 才能对齐：
        # photo = 有图片且没有视频；video = 有视频/动图；text = 完全没有媒体。
        self._groups = {
            "all": self.cards,
            "video": [c for c in self.cards if self._media_type(c) == "video"],
            "photo": [c for c in self.cards if self._media_type(c) == "photo"],
            "text": [c for c in self.cards if not (c.get("media") or [])],
            "bookmark": [
                c for c in self.cards if "bookmark" in (c.get("collections") or [])
            ],
            "like": [
                c for c in self.cards if "like" in (c.get("collections") or [])
            ],
        }
        # 归档里出现过的作者 handle（小写）；前端渲染 @提及 时用来判断
        # 该 handle 是否有作者页可跳转，避免每点一次 @ 都要发请求。
        self.author_handles = sorted({
            (card.get("handle") or "").strip().lower()
            for card in self.cards
            if (card.get("handle") or "").strip()
        })

    # ---------- 查询 ----------

    @staticmethod
    def _media_type(card):
        """卡片的正交媒体类型：photo（只有图片）/ video（含视频或动图）/ text（无媒体）。

        三种取值互斥，保证 media_type 各分支计数之和 = all。
        """
        media = card.get("media") or []
        if not media:
            return "text"
        kinds = {(m.get("t") or "").lower() for m in media}
        if kinds & set(VIDEO_TYPES):
            return "video"
        # 有媒体但没有视频：photo（媒体墙对未知类型也默认按 photo 展示）
        return "photo"

    def counts(self):
        seen = self.state.seen_ids()
        unseen = sum(1 for c in self.cards if c["id"] not in seen)
        favorites = set(self.state.favorite_ids())
        likes = set(self.state.list_local_likes())
        local_fav = sum(1 for c in self.cards if c["id"] in favorites)
        local_like = sum(1 for c in self.cards if c["id"] in likes)
        return {
            "all": len(self.cards),
            "video": len(self._groups["video"]),
            "photo": len(self._groups["photo"]),
            "text": len(self._groups["text"]),
            "unseen": unseen,
            "seen": len(self.cards) - unseen,
            "fav": local_fav,
            "bookmark": len(self._groups["bookmark"]),
            "like": len(self._groups["like"]),
            # 四维筛选的别名（与 /api/feed 的 source/media_type/read_state 同名）
            "x_bookmark": len(self._groups["bookmark"]),
            "x_like": len(self._groups["like"]),
            "local_fav": local_fav,
            "local_like": local_like,
        }

    def meta_payload(self):
        payload = dict(self.meta)
        payload["filters"] = self.counts()
        payload["state_db"] = self.state.path
        payload["media_root"] = self.media_root
        # 归档里所有作者 handle：前端拿它把正文 @提及 链到 /author/<handle>；
        # 不在这个集合里的 @ 会走本地搜索 /?q=@handle（纯离线）。
        payload["authors"] = self.author_handles
        # 维护能力探测：找不到 store 时前端把两个按钮禁用并说明原因
        gc_script = self.store_script if self.store_root else None
        payload["store"] = {
            "root": self.store_root,
            "gc_script": gc_script,
            "clean_tmp": bool(self.store_root and os.path.isdir(
                os.path.join(self.store_root, ".tmp"))),
            "gc": bool(gc_script and os.path.isfile(gc_script)),
        }
        return payload

    def _item(self, card, plays, positions, seen, favorites, likes=None):
        """给卡片补上状态和每个媒体自己的续播位置。"""
        item = dict(card)
        media = []
        for media_item in card.get("media") or []:
            copy = dict(media_item)
            copy["last_pos_ms"] = int(positions.get(media_item.get("key"), 0) or 0)
            media.append(copy)
        item["media"] = media
        media_keys = [m.get("key") for m in media]
        item["plays"] = sum(plays.get(k, 0) for k in media_keys)
        item["last_pos_ms"] = max(
            [positions.get(k, 0) for k in media_keys] or [0]
        )
        item["seen"] = card["id"] in seen
        item["favorited"] = card["id"] in favorites
        # 四维筛选统一命名：local_fav = 本地收藏，local_like = 本地点赞
        item["local_fav"] = item["favorited"]
        item["local_like"] = card["id"] in (likes or ())
        return item

    @staticmethod
    def _relation(rel):
        tag, _, target = str(rel).partition(":")
        return tag, target

    @staticmethod
    def _advanced_cards(cards, since=None, until=None, min_likes=0, min_views=0):
        if since:
            cards = [c for c in cards if (c.get("created") or "")[:10] >= since]
        if until:
            cards = [c for c in cards if (c.get("created") or "")[:10] <= until]
        if min_likes:
            cards = [c for c in cards if (c.get("like_count") or 0) >= min_likes]
        if min_views:
            cards = [c for c in cards if (c.get("view_count") or 0) >= min_views]
        return cards

    def feed(self, offset, limit, sort, query, media_type="all", source="all",
             read_state="all", author=None, since=None, until=None,
             min_likes=0, min_views=0, account=None):
        """四维正交筛选 + 原有排序/搜索/账号/高级过滤。

        media_type: all|photo|video|text（三者互斥）
        source:     all|x_bookmark|x_like|local_fav|local_like
        read_state: all|unseen|seen
        本地点赞/收藏来自 state.db；total 在分页前算，任意组合下翻页都稳定。
        """
        items = self.cards
        if media_type != "all":
            items = [c for c in items if self._media_type(c) == media_type]
        if source != "all":
            if source == "x_bookmark":
                items = [c for c in items
                         if "bookmark" in (c.get("collections") or [])]
            elif source == "x_like":
                items = [c for c in items
                         if "like" in (c.get("collections") or [])]
            elif source == "local_fav":
                favorites = set(self.state.favorite_ids())
                items = [c for c in items if c["id"] in favorites]
            elif source == "local_like":
                likes = set(self.state.list_local_likes())
                items = [c for c in items if c["id"] in likes]
        if read_state != "all":
            seen = self.state.seen_ids()
            want_seen = read_state == "seen"
            items = [c for c in items if (c["id"] in seen) == want_seen]
        if account:
            # account=acct-2：只留「这个归档账号保存过」的卡片。
            # 一张卡片可能同时在多个账号的收藏里，所以按集合求交集。
            wanted = {
                part.strip().lower() for part in account.split(",") if part.strip()
            }
            if wanted:
                items = [
                    c for c in items
                    if wanted & {
                        str(a).strip().lower() for a in c.get("accounts") or []
                    }
                ]
        if author:
            # 点头像进来的：按 handle 精确匹配「这个人发过的推文」，
            # 不会把别人正文里 @ 到他的推文也算进来
            want = author.strip().lstrip("@").lower()
            items = [c for c in items if (c.get("handle") or "").lower() == want]
        if query:
            # 允许直接搜 "@handle"：去掉前导 @ 再按子串匹配
            needle = query.lower().strip().lstrip("@")
            if needle:
                items = [c for c in items
                         if needle in (c.get("text") or "").lower()
                         or needle in (c.get("handle") or "").lower()
                         or needle in (c.get("name") or "").lower()
                         or any(needle in (u.get("u") or "").lower() for u in c.get("urls") or [])]
        items = self._advanced_cards(items, since, until, min_likes, min_views)
        if sort == "likes":
            items = sorted(
                items,
                key=lambda c: (c.get("like_count") or 0, c.get("created_ts") or 0),
                reverse=True,
            )
        elif sort == "views":
            items = sorted(
                items,
                key=lambda c: (c.get("view_count") or 0, c.get("created_ts") or 0),
                reverse=True,
            )

        total = len(items)
        offset = max(0, offset)
        page = items[offset:offset + limit]

        plays = self.state.plays_map()
        positions = self.state.positions_map()
        seen = self.state.seen_ids()
        favorites = set(self.state.favorite_ids())
        likes = set(self.state.list_local_likes())

        out = [
            self._item(card, plays, positions, seen, favorites, likes)
            for card in page
        ]
        return {"total": total, "offset": offset, "limit": limit, "items": out}

    def stats(self, days=60):
        return self.state.stats(self.card_lookup, self.media_lookup, days=days)

    def media(self, offset, limit, filter_name, sort, query, author=None,
              since=None, until=None, min_likes=0, min_views=0):
        """扁平媒体列表；每项带上所属推文的完整媒体集，供灯箱直接使用。"""
        cards = self.cards
        if author:
            want = author.strip().lstrip("@").lower()
            cards = [c for c in cards if (c.get("handle") or "").lower() == want]
        if query:
            needle = query.lower().strip().lstrip("@")
            if needle:
                cards = [
                    c for c in cards
                    if needle in (c.get("text") or "").lower()
                    or needle in (c.get("handle") or "").lower()
                    or needle in (c.get("name") or "").lower()
                    or any(needle in (u.get("u") or "").lower()
                           for u in c.get("urls") or [])
                ]

        cards = self._advanced_cards(cards, since, until, min_likes, min_views)
        rows = []
        for card in cards:
            for index, media in enumerate(card.get("media") or []):
                kind = media.get("t") or "photo"
                if filter_name == "photo" and kind != "photo":
                    continue
                if filter_name == "video" and kind != "video":
                    continue
                if filter_name == "gif" and kind != "animated_gif":
                    continue
                rows.append((card, index, media))

        if sort == "likes":
            rows.sort(
                key=lambda row: (
                    row[0].get("like_count") or 0,
                    row[0].get("created_ts") or 0,
                ),
                reverse=True,
            )
        elif sort == "views":
            rows.sort(
                key=lambda row: (
                    row[0].get("view_count") or 0,
                    row[0].get("created_ts") or 0,
                ),
                reverse=True,
            )
        else:
            rows.sort(
                key=lambda row: (
                    row[0].get("created_ts") or 0,
                    row[0].get("id") or "",
                ),
                reverse=True,
            )

        total = len(rows)
        offset = max(0, offset)
        page = rows[offset:offset + limit]
        positions = self.state.positions_map()

        out = []
        for card, index, media in page:
            tweet_media = [
                dict(
                    item,
                    last_pos_ms=int(positions.get(item.get("key"), 0) or 0),
                )
                for item in card.get("media") or []
            ]
            out.append({
                "media_key": media.get("key"),
                "kind": media.get("t") or "photo",
                "src": media.get("src"),
                "poster": media.get("poster") or None,
                "w": media.get("w"),
                "h": media.get("h"),
                "ms": media.get("ms"),
                "size": media.get("size"),
                "missing": bool(media.get("missing")),
                "tweet_id": card.get("id"),
                "handle": card.get("handle"),
                "name": card.get("name"),
                "text": card.get("text") or "",
                "created": card.get("created"),
                "saved": card.get("saved"),
                "media_index": index,
                "media_count": len(tweet_media),
                "tweet_media": tweet_media,
                "last_pos_ms": int(positions.get(media.get("key"), 0) or 0),
                # 互动计数（来自归档 raw_json，由 export_timeline.py 写入）
                "like_count": card.get("like_count") or 0,
                "reply_count": card.get("reply_count") or 0,
                "retweet_count": card.get("retweet_count") or 0,
                "quote_count": card.get("quote_count") or 0,
                "bookmark_count": card.get("bookmark_count") or 0,
                "view_count": card.get("view_count") or 0,
            })
        return {"total": total, "offset": offset, "limit": limit, "items": out}

    def author_summary(self, handle):
        want = (handle or "").strip().lstrip("@").lower()
        cards = [
            card for card in self.cards
            if (card.get("handle") or "").lower() == want
        ]
        if not cards:
            return {
                "handle": want,
                "name": None,
                "tweets": 0,
                "media": 0,
                "photo": 0,
                "video": 0,
                "gif": 0,
                "deleted": 0,
                "oldest": None,
                "newest": None,
                "avatar": None,
                "bio": None,
                "followers": None,
                "following": None,
                "location": None,
                "joined": None,
                "verified": False,
                "accounts": [],
            }
        media = [m for card in cards for m in card.get("media") or []]
        kinds = [m.get("t") or "photo" for m in media]
        dates = [card.get("created") for card in cards if card.get("created")]

        # 作者档案字段（来自导出数据的 author_* 列）：取第一条有值的卡片，
        # 不同卡片可能来自不同账号的采集，信息完整性不一样。
        def profile_field(key):
            for card in cards:
                value = card.get(key)
                if value not in (None, "", []):
                    return value
            return None

        accounts = set()
        for card in cards:
            for account in card.get("accounts") or []:
                if account:
                    accounts.add(str(account))

        return {
            "handle": cards[0].get("handle") or want,
            "name": cards[0].get("name"),
            "tweets": len(cards),
            "media": len(media),
            "photo": kinds.count("photo"),
            "video": kinds.count("video"),
            "gif": kinds.count("animated_gif"),
            "deleted": sum(1 for card in cards if card.get("deleted")),
            "oldest": min(dates) if dates else None,
            "newest": max(dates) if dates else None,
            "avatar": cards[0].get("avatar"),
            "bio": profile_field("author_bio"),
            "followers": profile_field("author_followers"),
            "following": profile_field("author_following"),
            "location": profile_field("author_location"),
            "joined": profile_field("author_joined"),
            "verified": any(bool(card.get("author_verified")) for card in cards),
            "accounts": sorted(accounts),
        }

    def tweet_detail(self, tweet_id):
        card = self.all_by_id.get(str(tweet_id))
        if not card:
            return None

        plays = self.state.plays_map()
        positions = self.state.positions_map()
        seen = self.state.seen_ids()
        favorites = set(self.state.favorite_ids())
        likes = set(self.state.list_local_likes())

        parent = None
        quoted = None
        children = []

        for rel in card.get("rels") or []:
            tag, target = self._relation(rel)
            other = self.all_by_id.get(target)
            if not other or other is card:
                continue
            if tag in ("reply_to", "thread_parent") and parent is None:
                parent = other
            elif tag == "quote_of" and quoted is None:
                quoted = other
            elif tag == "thread_child":
                children.append(other)

        # 子推文自身通常没有关系行，从父推文的 thread_child 反查父级。
        if parent is None:
            for other in self.all_cards:
                if other is card:
                    continue
                for rel in other.get("rels") or []:
                    tag, target = self._relation(rel)
                    if tag == "thread_child" and target == card.get("id"):
                        parent = other
                        break
                if parent is not None:
                    break

        # 普通回复和 thread_parent 也构成子级关系。
        for other in self.all_cards:
            if other is card or other is parent or other is quoted:
                continue
            for rel in other.get("rels") or []:
                tag, target = self._relation(rel)
                if target == card.get("id") and tag in ("reply_to", "thread_parent"):
                    children.append(other)
                    break

        unique_children = []
        child_ids = set()
        for child in children:
            child_id = child.get("id")
            if not child_id or child_id in child_ids:
                continue
            child_ids.add(child_id)
            unique_children.append(child)
        unique_children.sort(
            key=lambda child: (
                child.get("created_ts") or 0,
                child.get("id") or "",
            )
        )

        return {
            "tweet": self._item(card, plays, positions, seen, favorites, likes),
            "parent": (
                self._item(parent, plays, positions, seen, favorites, likes)
                if parent is not None else None
            ),
            "quoted": (
                self._item(quoted, plays, positions, seen, favorites, likes)
                if quoted is not None else None
            ),
            "children": [
                self._item(child, plays, positions, seen, favorites, likes)
                for child in unique_children
            ],
            "child_total": len(unique_children),
        }

    # ---------- 媒体 ----------

    def resolve_avatar(self, rel_path):
        """把 /avatars/<file> 解析到 data/avatars；越界返回 None。"""
        rel = posixpath.normpath("/" + unquote(rel_path)).lstrip("/")
        if not rel or rel.startswith(".."):
            return None
        root = os.path.realpath(os.path.join(self.data_dir, "avatars"))
        full = os.path.realpath(os.path.join(root, rel))
        if full != root and not full.startswith(root + os.sep):
            return None
        return full

    def resolve_media(self, rel_path):
        """把 /media/<推文ID>/<文件名> 解析成真实路径，越界返回 None。"""
        rel = posixpath.normpath("/" + unquote(rel_path)).lstrip("/")
        if not rel or rel.startswith(".."):
            return None
        full = os.path.realpath(os.path.join(self.media_root, rel))
        if full != self.media_root and not full.startswith(self.media_root + os.sep):
            return None
        return full

    # ---------- 归档维护（只碰 store/.tmp/*.part 和 store_v3.py --gc） ----------

    def _store_tmp_dir(self):
        """只解析 store/.tmp 本身；越界或不存在返回 None。"""
        if not self.store_root:
            return None
        root = os.path.realpath(self.store_root)
        tmp = os.path.realpath(os.path.join(root, ".tmp"))
        if not tmp.startswith(root + os.sep):
            return None
        if not os.path.isdir(tmp):
            return None
        return tmp

    def cache_clean_tmp(self):
        """删除 store/.tmp 下的 *.part（只这一层，不递归、不跟随符号链接）。

        先拿 store/.lock：正在下载时 .part 是活的，不能删。
        """
        if not self.store_root:
            raise RuntimeError("找不到归档 store 目录（未配置 --store）")
        with StoreFileLock(self.store_root):
            tmp = self._store_tmp_dir()
            if tmp is None:
                raise RuntimeError("找不到 store/.tmp（目录不存在）")
            removed = 0
            freed = 0
            try:
                entries = list(os.scandir(tmp))
            except OSError as exc:
                raise RuntimeError(f"读取 store/.tmp 失败：{exc}") from exc
            for entry in entries:
                if not entry.name.endswith(".part"):
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                full = os.path.realpath(entry.path)
                if not full.startswith(tmp + os.sep):
                    continue
                try:
                    size = entry.stat(follow_symlinks=False).st_size
                except OSError:
                    size = 0
                try:
                    os.unlink(full)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    log(f"[clean] 删除失败 {entry.name}: {exc}")
                    continue
                removed += 1
                freed += size
        log(f"[clean] store/.tmp 下 .part：{removed} 个，释放 {human_bytes(freed)}")
        return {"removed": removed, "freed_bytes": freed, "tmp_dir": tmp}

    def store_gc(self, dry_run=False):
        """调用 tools/store_v3.py --gc（拿 store/.lock；失败给出可读错误）。"""
        if not self.store_root:
            return {"ok": False, "error": "找不到归档 store 目录"}, HTTPStatus.CONFLICT
        if not self.store_script or not os.path.isfile(self.store_script):
            return {"ok": False, "error": "找不到 tools/store_v3.py，无法执行回收"}, \
                HTTPStatus.CONFLICT
        cmd = [sys.executable, self.store_script, "--gc", "--store", self.store_root]
        if dry_run:
            cmd.append("--dry-run")
        log(f"[gc] 执行：{' '.join(cmd)}")
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=STORE_GC_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False,
                    "error": f"回收超时（超过 {STORE_GC_TIMEOUT} 秒），请稍后再试"}, \
                HTTPStatus.CONFLICT
        except OSError as exc:
            return {"ok": False, "error": f"无法启动 store_v3.py：{exc}"}, \
                HTTPStatus.CONFLICT

        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        tail = "\n".join(stdout.splitlines()[-20:])
        if proc.returncode == 0:
            payload = {
                "ok": True,
                "dry_run": bool(dry_run),
                "reclaimed_bytes": None,
                "reclaimable": None,
                "output": tail,
            }
            match = re.search(r"reclaimed\s+(\d+)\s+bytes\s+total", stdout)
            if match:
                payload["reclaimed_bytes"] = int(match.group(1))
            match = re.search(r"reclaimable\s+([0-9.]+\s*[KMGTP]?B)", stdout)
            if match:
                payload["reclaimable"] = match.group(1).strip()
            log(f"[gc] 完成：{tail.splitlines()[-1] if tail else 'ok'}")
            return payload, HTTPStatus.OK
        if proc.returncode == STORE_LOCK_BUSY_EXIT:
            return {
                "ok": False,
                "error": "归档存储正被另一个进程占用（store/.lock），请稍后再试",
                "detail": stderr or tail,
            }, HTTPStatus.CONFLICT
        return {
            "ok": False,
            "error": f"回收失败（store_v3.py 退出码 {proc.returncode}）",
            "detail": stderr or tail,
        }, HTTPStatus.INTERNAL_SERVER_ERROR


class Handler(BaseHTTPRequestHandler):
    server_version = "TimelineHTTP/1.1"
    protocol_version = "HTTP/1.1"
    app = None  # 由 main 注入

    # ---------- 工具 ----------

    def log_message(self, fmt, *args):
        if self.path.startswith("/media/"):
            return  # 媒体请求太吵，不记
        log(f"{self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")

    def send_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # 页面卸载时的 sendBeacon 不会等响应，连接被对面关掉是常态，不是错误
            self.close_connection = True

    def send_error_json(self, status, message):
        self.send_json({"ok": False, "error": message}, status=status)

    def client_is_loopback(self):
        """维护接口只接受本机请求：即便 --host 是 0.0.0.0，也不让局域网触发删除。"""
        try:
            address = str(self.client_address[0])
        except (IndexError, TypeError):
            return False
        return address in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > MAX_BODY:
            # 必须关连接：body 还在 socket 里没读，keep-alive 下会被当成下一个请求的请求行
            self.close_connection = True
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def send_file(self, path, content_type=None, cache="no-store", allow_range=False):
        try:
            size = os.path.getsize(path)
        except OSError:
            return self.send_error_json(HTTPStatus.NOT_FOUND, "文件不存在")
        ctype = content_type or CONTENT_TYPES.get(os.path.splitext(path)[1].lower()) \
            or mimetypes.guess_type(path)[0] or "application/octet-stream"

        start, end = 0, size - 1
        status = HTTPStatus.OK
        range_header = self.headers.get("Range") if allow_range else None
        if range_header and range_header.startswith("bytes=") and "," in range_header:
            # 多段请求要么回 multipart/byteranges、要么回 200 全量；只回第一段是错的
            range_header = None
        if range_header and range_header.startswith("bytes="):
            spec = range_header[6:].split(",")[0].strip()
            first, _, last = spec.partition("-")
            try:
                if first == "":                      # bytes=-N 末尾 N 字节
                    length = int(last)
                    start = max(0, size - length)
                else:
                    start = int(first)
                    end = int(last) if last else size - 1
                end = min(end, size - 1)
                if start > end or start >= size:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = HTTPStatus.PARTIAL_CONTENT
            except ValueError:
                start, end, status = 0, size - 1, HTTPStatus.OK

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache)
        if allow_range:
            self.send_header("Accept-Ranges", "bytes")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(CHUNK, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    # ---------- 路由 ----------

    def do_HEAD(self):
        self.do_GET(head_only=True)

    def do_GET(self, head_only=False):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        app = self.app

        try:
            if path == "/health":
                return self.send_json({"ok": True, "items": len(app.cards),
                                       "generated_at": app.meta.get("generated_at")})

            if path == "/api/meta":
                return self.send_json(app.meta_payload())

            if path == "/api/feed":
                def num(name, default, low, high):
                    try:
                        value = int(query.get(name, [default])[0])
                    except (TypeError, ValueError):
                        value = default
                    return max(low, min(high, value))

                media_type = (query.get("media_type", ["all"])[0] or "all").lower()
                if media_type not in MEDIA_TYPES:
                    media_type = "all"
                source = (query.get("source", ["all"])[0] or "all").lower()
                if source not in SOURCE_FILTERS:
                    source = "all"
                read_state = (query.get("read_state", ["all"])[0] or "all").lower()
                if read_state not in READ_STATES:
                    read_state = "all"
                # 旧 filter 兼容映射：只在对应新参数没显式出现时生效，新参数优先
                filter_name = (query.get("filter", ["all"])[0] or "all").lower()
                if filter_name not in FILTERS:
                    filter_name = "all"
                legacy = LEGACY_FILTERS.get(filter_name)
                if legacy:
                    legacy_name, legacy_value = legacy
                    if legacy_name not in query:
                        if legacy_name == "media_type":
                            media_type = legacy_value
                        elif legacy_name == "source":
                            source = legacy_value
                        elif legacy_name == "read_state":
                            read_state = legacy_value
                sort = (query.get("sort", ["created"])[0] or "created").lower()
                if sort not in SORTS:
                    sort = "created"
                return self.send_json(app.feed(
                    offset=num("offset", 0, 0, 10 ** 9),
                    limit=num("limit", 30, 1, 200),
                    sort=sort,
                    media_type=media_type,
                    source=source,
                    read_state=read_state,
                    query=(query.get("q", [""])[0] or "").strip(),
                    author=(query.get("author", [""])[0] or "").strip(),
                    since=query_date(query, "since"),
                    until=query_date(query, "until"),
                    min_likes=query_nonneg(query, "min_likes"),
                    min_views=query_nonneg(query, "min_views"),
                    account=(query.get("account", [""])[0] or "").strip(),
                ))

            if path == "/api/tweet":
                tweet_id = str(query.get("id", [""])[0] or "").strip()
                detail = app.tweet_detail(tweet_id)
                if detail is None:
                    return self.send_error_json(HTTPStatus.NOT_FOUND, "推文不存在")
                return self.send_json(detail)

            if path == "/api/author":
                handle = str(query.get("handle", [""])[0] or "").strip()
                if not handle:
                    return self.send_error_json(HTTPStatus.BAD_REQUEST, "缺少 handle")
                return self.send_json(app.author_summary(handle))

            if path == "/api/state":
                state = app.state.get_feed_state()
                state["session_id"] = app.state.start_session()
                state["plays"] = app.state.plays_map()
                state["positions"] = app.state.positions_map()
                state["counts"] = app.counts()
                return self.send_json(state)

            if path == "/api/favorites":
                ids = app.state.favorite_ids()
                return self.send_json({
                    "ok": True,
                    "total": len(ids),
                    "ids": ids,
                    "items": [app.card_lookup.get(i, {"tweet_id": i}) for i in ids],
                })

            if path == "/api/likes":
                ids = app.state.list_local_likes()
                return self.send_json({"ok": True, "total": len(ids), "items": ids})

            if path == "/api/stats":
                try:
                    days = int(query.get("days", ["14"])[0])
                except (TypeError, ValueError):
                    days = 14
                days = max(7, min(365, days))
                return self.send_json(app.stats(days=days))

            if path == "/api/media":
                def media_num(name, default, low, high):
                    try:
                        value = int(query.get(name, [default])[0])
                    except (TypeError, ValueError):
                        value = default
                    return max(low, min(high, value))

                media_filter = (query.get("filter", ["all"])[0] or "all").lower()
                if media_filter not in MEDIA_FILTERS:
                    media_filter = "all"
                media_sort = (query.get("sort", ["created"])[0] or "created").lower()
                if media_sort not in SORTS:
                    media_sort = "created"
                return self.send_json(app.media(
                    offset=media_num("offset", 0, 0, 10 ** 9),
                    limit=media_num("limit", 36, 1, 200),
                    filter_name=media_filter,
                    sort=media_sort,
                    query=(query.get("q", [""])[0] or "").strip(),
                    author=(query.get("author", [""])[0] or "").strip(),
                    since=query_date(query, "since"),
                    until=query_date(query, "until"),
                    min_likes=query_nonneg(query, "min_likes"),
                    min_views=query_nonneg(query, "min_views"),
                ))

            if path == "/api/state/export":
                return self.send_json(app.state.export())

            if path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            # 媒体墙页面必须排在 /media/ 文件路由之前
            if path in ("/media", "/media/"):
                return self.send_file(os.path.join(WEB_DIR, "index.html"),
                                      "text/html; charset=utf-8")

            if path.startswith("/media/"):
                full = app.resolve_media(path[len("/media/"):])
                if full is None:
                    return self.send_error_json(HTTPStatus.FORBIDDEN, "路径越界")
                if not os.path.isfile(full):
                    return self.send_error_json(HTTPStatus.NOT_FOUND, "媒体文件不存在")
                return self.send_file(full, cache="max-age=604800", allow_range=True)

            if path.startswith("/avatars/"):
                full = app.resolve_avatar(path[len("/avatars/"):])
                if full is None:
                    return self.send_error_json(HTTPStatus.FORBIDDEN, "路径越界")
                if not os.path.isfile(full):
                    return self.send_error_json(HTTPStatus.NOT_FOUND, "头像文件不存在")
                return self.send_file(full, cache="max-age=604800")

            # 详情页 / 作者页同样走 SPA 外壳
            if path.startswith("/tweet/") or path.startswith("/author/"):
                return self.send_file(os.path.join(WEB_DIR, "index.html"),
                                      "text/html; charset=utf-8")

            # 静态页：时间线 / 收藏 / 统计共用同一个 SPA 外壳
            if path in ("/", "/index.html", "/favorites", "/favorites.html",
                        "/stats", "/stats.html"):
                return self.send_file(os.path.join(WEB_DIR, "index.html"),
                                      "text/html; charset=utf-8")
            if path in ("/app.css", "/app.js", "/stats.js", "/theme.js"):
                name = path.lstrip("/")
                ctype = "text/css; charset=utf-8" if name.endswith(".css") else "text/javascript; charset=utf-8"
                return self.send_file(os.path.join(WEB_DIR, name), ctype)

            return self.send_error_json(HTTPStatus.NOT_FOUND, "没有这个地址")
        except (BrokenPipeError, ConnectionResetError):
            # 浏览器关掉视频连接是常事
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001
            log("请求处理异常:", repr(exc))
            try:
                self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            except Exception:  # noqa: BLE001
                self.close_connection = True

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        app = self.app
        try:
            body = self.read_json_body()
        except (ValueError, json.JSONDecodeError) as exc:
            return self.send_error_json(HTTPStatus.BAD_REQUEST, f"请求体不是合法 JSON：{exc}")

        try:
            if path == "/api/state/feed":
                state = app.state.set_feed_state(
                    last_tweet_id=body.get("last_tweet_id"),
                    last_created=body.get("last_created"),
                    filter=body.get("filter"),
                    sort=body.get("sort"),
                    autoplay=body.get("autoplay"),
                )
                return self.send_json({"ok": True, "state": state})

            if path == "/api/state/seen":
                raw_ids = body.get("tweet_ids") or []
                if not isinstance(raw_ids, list):
                    return self.send_error_json(HTTPStatus.BAD_REQUEST, "tweet_ids 必须是数组")
                ids = [str(i) for i in raw_ids if i][:500]     # 一次最多认 500 条，防滥用
                return self.send_json(app.state.mark_seen(ids, body.get("session_id")))

            if path == "/api/state/seen-all":
                ids = [card["id"] for card in app.cards]
                return self.send_json(app.state.mark_seen(ids, body.get("session_id")))

            if path == "/api/state/play":
                media_key = body.get("media_key")
                if not isinstance(media_key, str) or not media_key:
                    return self.send_error_json(HTTPStatus.BAD_REQUEST, "缺少 media_key")
                if len(media_key) > 128:
                    return self.send_error_json(HTTPStatus.BAD_REQUEST, "media_key 太长")
                action = (body.get("action") or "progress").lower()
                if action not in ("start", "progress", "pos", "end"):
                    return self.send_error_json(HTTPStatus.BAD_REQUEST, "action 不对")
                # 数字参数先校验：否则 int() 抛的 Python 原文会以 500 形式吐给前端
                nums = {}
                for field in ("watched_ms", "pos_ms", "duration_ms"):
                    value = body.get(field)
                    if value is None or value == "":
                        nums[field] = None
                        continue
                    try:
                        nums[field] = int(value)
                    except (TypeError, ValueError):
                        return self.send_error_json(
                            HTTPStatus.BAD_REQUEST, f"{field} 必须是整数")
                instance_id = body.get("instance_id")
                if instance_id is not None and not isinstance(instance_id, str):
                    instance_id = str(instance_id)
                tweet_id = body.get("tweet_id")
                if tweet_id is not None and not isinstance(tweet_id, str):
                    tweet_id = str(tweet_id)
                return self.send_json(app.state.record_play(
                    media_key=media_key,
                    tweet_id=tweet_id,
                    instance_id=instance_id,
                    action=action,
                    watched_ms=nums["watched_ms"] or 0,
                    pos_ms=nums["pos_ms"],
                    duration_ms=nums["duration_ms"],
                ))

            if path == "/api/state/reset":
                if body.get("confirm") != "yes":
                    return self.send_error_json(HTTPStatus.BAD_REQUEST, "需要 confirm=yes")
                return self.send_json(app.state.reset())

            if path == "/api/favorites":
                action = (body.get("action") or "toggle").lower()
                if action not in ("add", "remove", "toggle"):
                    return self.send_error_json(HTTPStatus.BAD_REQUEST, "action 只能是 add/remove/toggle")
                tweet_id = str(body.get("tweet_id") or "")
                if tweet_id not in app.card_by_id:
                    # 不校验的话收藏表会累积孤儿，而且 total 和 counts.fav 会当场对不上
                    return self.send_error_json(HTTPStatus.BAD_REQUEST, "未知 tweet_id")
                try:
                    result = app.state.set_favorite(tweet_id, action, body.get("note"))
                except ValueError as exc:
                    return self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
                result["counts"] = app.counts()
                return self.send_json(result)

            if path == "/api/likes":
                # 本地点赞：只写 state.db，不碰归档/媒体库
                tweet_id = str(body.get("tweet_id") or "").strip()
                if not tweet_id:
                    return self.send_error_json(HTTPStatus.BAD_REQUEST, "缺少 tweet_id")
                if tweet_id not in app.card_by_id:
                    # 和收藏一样校验，避免点赞表累积孤儿、counts 对不上
                    return self.send_error_json(HTTPStatus.BAD_REQUEST, "未知 tweet_id")
                liked = body.get("liked")
                if not isinstance(liked, bool):
                    return self.send_error_json(
                        HTTPStatus.BAD_REQUEST, "liked 必须是 true 或 false")
                try:
                    result = app.state.set_local_like(tweet_id, liked)
                except ValueError as exc:
                    return self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
                result["counts"] = app.counts()
                return self.send_json(result)

            if path == "/api/cache/clean":
                # 安全操作：target 只认 "tmp"，服务端只删 store/.tmp 下的 *.part
                if not self.client_is_loopback():
                    return self.send_error_json(
                        HTTPStatus.FORBIDDEN, "维护接口只接受本机请求")
                target = str(body.get("target") or "").strip().lower()
                if target != "tmp":
                    return self.send_error_json(
                        HTTPStatus.BAD_REQUEST, "target 只支持 tmp")
                try:
                    result = app.cache_clean_tmp()
                except RuntimeError as exc:
                    return self.send_error_json(HTTPStatus.CONFLICT, str(exc))
                result.update({"ok": True, "target": "tmp"})
                return self.send_json(result)

            if path == "/api/store/gc":
                # 安全操作：只调用归档自带的 store_v3.py --gc，且必须拿到 store 锁
                if not self.client_is_loopback():
                    return self.send_error_json(
                        HTTPStatus.FORBIDDEN, "维护接口只接受本机请求")
                payload, status = app.store_gc(dry_run=bool(body.get("dry_run")))
                return self.send_json(payload, status=status)

            return self.send_error_json(HTTPStatus.NOT_FOUND, "没有这个地址")
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001
            log("POST 处理异常:", repr(exc))
            try:
                self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            except Exception:  # noqa: BLE001 - 连接已经断了，别再喊
                self.close_connection = True


def local_ip():
    """取本机在局域/WSL 网络里的地址，给「localhost 打不开」时兜底。"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        return None


def default_vault():
    """归档根目录：显式参数 > 环境变量 > 项目同级目录 > 平台默认。"""
    env = os.environ
    if env.get("TWEETXVAULT_VAULT"):
        return env["TWEETXVAULT_VAULT"]
    base = env.get("XDG_DATA_HOME")
    if base:
        return os.path.join(base, "tweetxvault")
    # 本机布局：归档就放在项目目录的上一级
    sibling = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tweetxvault"
    )
    if os.path.exists(os.path.join(sibling, "archive.lancedb")):
        return sibling
    if os.name == "nt" and env.get("LOCALAPPDATA"):
        return os.path.join(env["LOCALAPPDATA"], "tweetxvault")
    return os.path.expanduser("~/.local/share/tweetxvault")


def main():
    parser = argparse.ArgumentParser(description="本地推文时间线服务")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--vault", default=default_vault())
    parser.add_argument("--data", default=DEFAULT_DATA_DIR)
    parser.add_argument("--state", default=None, help="状态库路径，默认 <data>/state.db")
    parser.add_argument("--store", default=None,
                        help="归档 store 根目录（维护接口用），默认自动探测")
    parser.add_argument("--store-py", default=None,
                        help="store_v3.py 路径（维护接口用），默认自动探测")
    args = parser.parse_args()

    data_dir = os.path.abspath(os.path.expanduser(args.data))
    ndjson = os.path.join(data_dir, "timeline.ndjson")
    if not os.path.isfile(ndjson):
        log(f"[错误] 找不到 {ndjson}，请先运行 export_timeline.py")
        return 2

    vault = os.path.abspath(os.path.expanduser(args.vault))
    if not os.path.isdir(os.path.join(vault, "media")):
        log(f"[错误] 归档媒体目录不存在：{os.path.join(vault, 'media')}")
        return 2

    state_path = args.state or os.path.join(data_dir, "state.db")
    store_root = resolve_store_root(args.store, data_dir)
    if store_root is None:
        log("[提示] 未找到归档 store 目录：统计页的清理/回收按钮会被禁用")
    store_script = resolve_store_script(store_root, args.store_py, data_dir)
    if store_root and store_script is None:
        log("[提示] 找到 store 但没找到 tools/store_v3.py：回收按钮会被禁用")
    if store_root:
        log(f"归档 store：{store_root}")
        if store_script:
            log(f"维护脚本：{store_script}")
    app = Timeline(data_dir, vault, state_path,
                   store_root=store_root, store_script=store_script)
    Handler.app = app

    server = None
    port = args.port
    for candidate in range(args.port, args.port + 20):
        try:
            server = ThreadingHTTPServer((args.host, candidate), Handler)
            port = candidate
            break
        except OSError as exc:
            log(f"端口 {candidate} 不可用（{exc}），换一个试试")
    if server is None:
        log("[错误] 8787-8806 都被占用了，用 --port 指定一个空端口")
        return 2
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    urls = [f"http://localhost:{port}"]
    ip = local_ip()
    if ip:
        urls.append(f"http://{ip}:{port}")
    try:
        with open(os.path.join(BASE_DIR, "last-url.txt"), "w", encoding="utf-8") as fh:
            fh.write(urls[0] + "\n")
    except OSError:
        pass

    log("=" * 60)
    log(f"时间线已启动：{urls[0]}    （统计页 {urls[0]}/stats）")
    if len(urls) > 1:
        log(f"如果上面打不开，试试：{urls[1]}")
    log(f"推文 {len(app.cards)} 条｜筛选 {app.counts()}")
    log(f"归档：{vault}")
    log(f"状态库：{state_path}")
    log("按 Ctrl+C 停止")
    log("=" * 60)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log("收到 Ctrl+C，正在停止……")
    finally:
        server.shutdown()
        server.server_close()
        app.state.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
