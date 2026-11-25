# 架构说明

## 数据流

```
Chrome (X 登录态)
   │  扩展读 auth_token / ct0 / auth_multi（HttpOnly，页面 JS 读不到）
   ▼
cookie_bridge.py  (127.0.0.1:8791, 令牌 + 固定扩展 ID)
   │  写入 work/accounts.json
   ▼
tweetxvault  ──sync──▶  accounts/<acct>/tweetxvault/archive.lancedb
   │                     （tweet / media / tweet_relation / raw_capture …）
   ▼
store_v3.py  ──download──▶  store/media.db + store/assets/<sha>.<ext>
   │                         (media_key, tier) → sha256，落盘只读
   ▼
export_viewer.py  ──▶  viewer/data/timeline.ndjson + avatars + 视频封面
   ▼
viewer/serve.py  ──▶  浏览器（只读 viewer/data 与 viewer/vault）
```

## 存储模型

| 表 | 主键/唯一键 | 说明 |
|---|---|---|
| `assets` | `UNIQUE(media_key, tier)` | 一个可下载对象；`state` = pending/done/failed/superseded |
| `refs` | `UNIQUE(account, collection, tweet_id, media_key, media_index)` | 一张卡片对媒体的引用；`collection` 区分 like/bookmark |
| `meta` | `key` | `schema_version` |

- **tier**：照片 `orig > large`；视频/动图 `best > mid > low`（按 `variants_json` 的 mp4 码率排序）。
- **档位支配**：请求低档时若已有更高档 `done`，直接复用；反向升级后旧档置 `superseded`，`--gc` 回收。
- **内容寻址**：blob 路径由 `sha256` 决定，同字节只存一份；落盘即 `FILE_ATTRIBUTE_READONLY`。
- **失败语义**：仅永久 HTTP 错误（4xx）允许降级，临时错误保留待重试并计入 `refs_unresolved`。

## 并发与一致性

- 所有写路径（下载、gc、投影、备份）抢 `store/.lock`（Windows `msvcrt` / POSIX `flock`），抢不到退出码 4。
- 备份用 `sqlite3.Connection.backup()` 生成一致性快照，全程持锁，避免与 `--gc` 交叉。
- `--verify` 只读、无锁；`--quick` 查存在/大小，完整模式逐文件 sha256。

## 网页数据契约

`viewer/data/timeline.ndjson` 每行一张卡片：

```json
{
  "id": "...", "handle": "...", "name": "...",
  "created": "...", "collections": ["like", "bookmark"], "accounts": ["acct-1"],
  "like_count": 0, "retweet_count": 0, "reply_count": 0, "quote_count": 0,
  "bookmark_count": 0, "view_count": 0,
  "avatar": "....jpg", "author_bio": "...", "author_followers": 0,
  "media": [{"key": "...", "t": "photo", "src": "/media/<id>/<key>.jpg",
             "poster": null, "w": 0, "h": 0, "ms": null, "size": 0, "missing": false}]
}
```

静态资源：头像走 `/avatars/<file>`（`viewer/data/avatars`），媒体与封面走 `/media/<tweet_id>/<file>`（`viewer/vault/media`）。
