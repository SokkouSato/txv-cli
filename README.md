# txv — 本地 X（Twitter）归档工具集

简体中文 | [English](README.en.md)

把 X 的书签、点赞、自己发的推文归档到本地，媒体按内容寻址存储，配一个纯离线的 X 风格浏览界面。

- **采集**：复用 [tweetxvault](https://github.com/lhl/tweetxvault) 抓时间线（`sync` 只写元数据，不下载媒体）
- **存储**：SQLite 状态表 + 内容寻址 blob（`sha256` 命名、落盘只读、原子替换）
- **浏览**：打包的离线网页（时间线 / 媒体墙 / 作者页 / 统计），零外部请求
- **维护**：`verify` 校验、`gc` 回收、`backup` 一致性冷备、`hide` 隐藏媒体

## 特性

- 一个 X 账号一个归档（`accounts/<id>/tweetxvault`），天然满足 tweetxvault 的 owner 约束
- 媒体 `(media_key, tier)` 复合键：书签留最高码率、点赞留中档，重复引用不重复下载
- 内容寻址 + 只读 blob，天然去重；`--gc` 只回收被取代且无人引用的旧档
- 进程锁（`store/.lock`）+ 原子落盘 + `--verify` 全量 sha256 校验
- 冷备走 `sqlite3.Connection.backup()`，与写操作互斥
- Chrome 扩展读 cookie（App-Bound 加密下唯一稳的方式），本机 bridge 强制令牌 + 固定扩展 ID
- `hide` 给媒体树打 Hidden+System+NotContentIndexed，防相册/搜索/部分云备份扫描

## 组成

```
txv.py                 统一 CLI
tools/                 存储、导出、备份、安全、统计脚本
viewer/                离线网页（serve.py + web/）
ext/                   Chrome 扩展（读 auth_token / ct0 / auth_multi）
patches/               Windows 下跑 tweetxvault 的补丁（可选）
```

## 安装

前置：Python 3.12+、[uv](https://docs.astral.sh/uv/)（推荐）或 pip，以及一个已登录 X 的 Chrome。

```bash
# 1) 依赖
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt      # Windows
# .venv/bin/python -m pip install -r requirements.txt        # Linux/macOS

# 2) 采集器（上游项目）
pip install tweetxvault
#    或者用 patches/windows-support.patch 给上游打补丁后在 Windows 上运行

# 3) 生成固定的扩展 ID（会写 ext/manifest.json 的 key 与 work/extension_id.txt）
.venv/Scripts/python tools/setup_extension.py
```

## 快速开始

```powershell
$py = ".\.venv\Scripts\python.exe"

# 1. 启动 cookie bridge（首次会自动生成 work/bridge_token.txt）
& $py tools/cookie_bridge.py --port 8791

# 2. Chrome → chrome://extensions → 开发者模式 → 加载已解压的扩展程序 → 选 ext/
#    把 work/bridge_token.txt 的内容粘进扩展弹窗的“桥接令牌”，登录 X 后切一次账号

# 3. 抓取（只写元数据）
& $py txv.py sync --collections likes,bookmarks

# 4. 下载已注册的媒体
& $py txv.py download --max-mb 3000

# 5. 导出网页数据并起服务
& $py txv.py serve --port 8799
```

打开 <http://127.0.0.1:8799/>。

## CLI

| 子命令 | 作用 |
|---|---|
| `sync` | 抓新书签/点赞（需要 X 凭据） |
| `download` | 下载已注册但未落盘的媒体（幂等） |
| `export` | 重建网页数据（硬链接 + 头像 + 视频封面） |
| `serve` | 导出并起离线网页 |
| `backup` | 冷备（`--copy-assets` 连媒体） |
| `refresh-counts` | 手动、限速地回抓近期互动数据 |
| `verify` | 校验落盘（`--quick` 只查存在+大小） |
| `gc` | 回收旧档与孤儿 blob |
| `hide` | 隐藏/恢复媒体（`--on/--off/--status`） |
| `stats` | 打印统计 |

```powershell
& $py txv.py --help
& $py txv.py download --max-mb 0          # 不限量
& $py txv.py verify --quick
& $py txv.py hide --on
& $py txv.py backup --dest D:\backup\x --copy-assets --include-archives
```

## 目录结构（运行时生成）

```
accounts/<acct>/tweetxvault/   LanceDB 归档（只读）
store/
  media.db                      SQLite：assets + refs + 下载状态
  assets/<xx>/<sha>.<ext>       媒体正本（只读、内容寻址）
  by-account/<acct>/<集合>/<年-月>/  可读硬链接（可重建）
viewer/
  data/timeline.ndjson          网页数据
  data/avatars/                 作者头像
  vault/media/<tweet_id>/       媒体硬链接 + 视频封面
  state.db                      已读/收藏/本地点赞
work/                           配置、令牌、日志（.gitignore）
```

冷备三件套：`store/assets/` + `store/media.db`（用 `backup` 的快照）+ `accounts/<id>/tweetxvault/`。
`by-account/`、`viewer/vault/`、`viewer/data/` 都是可重建的派生视图。

## 安全与隐私

- 归档与网页全程离线；只有 `sync` / `refresh-counts` 会用到 X 凭据。
- `work/accounts.json`、`work/bridge_token.txt`、`work/extension_key.pem` 是本机敏感文件，已在 `.gitignore` 里；bridge 写入后会尝试 `icacls` 收紧为仅当前用户。
- 本地点赞/收藏只写 `viewer/state.db`，不会回写 X。
- `txv.py hide` 只是隐藏属性，不是加密；真正保密请用 BitLocker/VeraCrypt 等卷级加密。

## 已知限制

1. 链接没有复检（`--revalidate` 未实现）；已落盘资产不会回访源 URL。
2. 互动数据是抓取时快照；要更新请手动 `refresh-counts`（有速率限制）。
3. 单写者：同一个 `store/` 不要多进程并发写。
4. 一个归档只对应一个 X 账号；多账号请用多个 `accounts/<id>` 目录。
5. 扩展 ID 与 `ext/manifest.json` 的 `key` 绑定；`--force` 轮换后需重新加载扩展并更新 `work/extension_id.txt`。

## 开发

```bash
python tools/store_v3.py --self-test     # 离线自测（假 HTTP 服务，覆盖失败/重试/幂等/只读）
```

## 许可证

MIT（见 `LICENSE`）。发布前请把版权人改成你自己的名字。

上游 [tweetxvault](https://github.com/lhl/tweetxvault) 为 Apache-2.0；`patches/` 下的补丁遵循其许可证。
