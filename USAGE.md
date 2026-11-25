# 使用说明（CLI 参考）

所有命令都在仓库根目录执行，`$py` 指本仓库的虚拟环境解释器：

```powershell
$py = ".\.venv\Scripts\python.exe"        # Windows
# py=".venv/bin/python"                    # Linux/macOS
```

## 1. 首次配置

```powershell
# 依赖
& $py -m pip install -r requirements.txt
& $py -m pip install tweetxvault          # 采集器（或在 Windows 上用 patches/ 的补丁）

# 固定扩展 ID（写 ext/manifest.json + work/extension_id.txt + work/extension_key.pem）
& $py tools/setup_extension.py

# 起 cookie bridge（首次会生成 work/bridge_token.txt）
& $py tools/cookie_bridge.py --port 8791
```

Chrome → `chrome://extensions` → 开发者模式 → 加载已解压的扩展程序 → 选 `ext/`；
把 `work/bridge_token.txt` 的内容粘进扩展弹窗的“桥接令牌”；登录 X 后切换一次账号即自动上报。

> 一个归档只对应一个 X 账号。多账号：每个账号跑一次同步，归档目录用 `accounts/<id>` 区分
> （`sync_accounts.py` 会按 `work/accounts.json` 里的账号自动建目录）。

## 2. 采集与下载

```powershell
& $py txv.py sync --collections likes,bookmarks      # 只写元数据
& $py txv.py sync --accounts acct-2 --limit 5        # 只抓某个账号的前 5 页

& $py txv.py download --max-mb 3000                  # 下载已注册媒体（幂等）
& $py txv.py download --max-mb 0                     # 不限量
& $py txv.py download --policy low                   # 全部低码率（更省空间）
```

可用策略：`default`（书签 best + 点赞/unknown mid）、`recommended`（同 default）、`best`、`low`。

## 3. 浏览

```powershell
& $py txv.py export                 # 重建 viewer/data（含头像与视频封面，增量）
& $py txv.py serve --port 8799      # 导出并起网页
& $py txv.py serve --no-export      # 只起服务
```

网页参数：

```
?media_type=all|photo|video|text
?source=all|x_bookmark|x_like|local_fav|local_like
?read_state=all|unseen|seen
?sort=created|likes|views
?q=...&author=...&since=YYYY-MM-DD&until=YYYY-MM-DD&min_likes=N&min_views=N
```

播放与已读状态存在 `viewer/state.db`；本地点赞/收藏只写这个库，不回写 X。

## 4. 维护

```powershell
& $py txv.py verify --quick          # 存在性 + 大小
& $py txv.py verify                  # 逐文件 sha256（慢）
& $py txv.py gc --dry-run            # 看能回收多少
& $py txv.py gc                      # 回收被取代的旧档与孤儿 blob
& $py txv.py backup --dest D:\backup\x --copy-assets --include-archives
& $py txv.py hide --on               # 隐藏媒体树（防相册/搜索扫描）
& $py txv.py hide --status
& $py txv.py hide --off
& $py txv.py stats
```

- `backup` 全程持 `store/.lock`，与下载/回收互斥。
- `hide` 写 `work/privacy.json`；之后 `download`/`export` 会自动给新文件补打属性。
- 退出码：`4` 表示 store 锁被占用（另一个进程在写）。

## 5. 数据位置

| 路径 | 内容 |
|---|---|
| `accounts/<id>/tweetxvault/` | tweetxvault 的 LanceDB 归档（只读） |
| `store/media.db` | assets / refs / 下载状态 |
| `store/assets/<xx>/<sha>.<ext>` | 媒体正本（只读） |
| `store/by-account/` | 可读硬链接（可重建） |
| `viewer/data/` | 网页数据 + 头像 |
| `viewer/vault/media/` | 网页用的硬链接 + 视频封面 |
| `viewer/state.db` | 已读 / 收藏 / 本地点赞 |
| `work/` | 账号、令牌、日志（`.gitignore`） |

冷备三件套：`store/assets/` + `store/media.db` 快照 + `accounts/`。

## 6. 排错

| 症状 | 处理 |
|---|---|
| `store lock busy` | 有下载/备份在跑；看 `store/.lock.info` 里的 pid |
| 同步报 cookie 失效 | Chrome 重新登录 X → 扩展重填令牌 → 切账号 → 重跑 `sync` |
| 头像/封面缺 | 重跑 `txv export`；视频缺封面时用 ffmpeg 抽帧 |
| 想换扩展 ID | `tools/setup_extension.py --force`，重新加载扩展，重启 bridge |
| 磁盘紧张 | `txv gc --dry-run`；`store/by-account` 可整体删除重建 |
