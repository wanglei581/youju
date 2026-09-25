# 有据 · 后端

数据底座与 API：监测全国政府网站栏目，发现新文件，提供只读接口。设计见 [docs/adr/](../docs/adr/)。

## 环境准备

需要 [uv](https://docs.astral.sh/uv/) 和 PostgreSQL 16（macOS：`brew install postgresql@16`）。

```sh
cd backend
uv sync                      # 安装依赖（按 uv.lock）
./scripts/dev_db.sh start    # 在 backend/.pgdata 启动项目内数据库（端口 55432），不改系统设置
uv run youju db-upgrade      # 建表 / 迁移到最新结构
```

`./scripts/dev_db.sh stop` 停止数据库；`reset` 清空重建。

## 常用命令

```sh
uv run youju import-sources          # 从 docs/数据来源/全国来源清单_P0_v1.csv 导入来源
uv run youju crawl                   # 检查全部已启用来源
uv run youju crawl --province 山东    # 只检查某个省（国家级来源填"国家"）
uv run youju crawl --source-id 12    # 只检查指定来源
uv run youju report                  # 来源健康和条目统计
uv run youju serve                   # 启动 API：http://127.0.0.1:8000/docs
```

## API

| 接口 | 说明 |
|---|---|
| `GET /health` | 健康检查 |
| `GET /api/sources?health=&province=&enabled=` | 来源列表及健康状态 |
| `GET /api/documents?q=&province=&dept=&since=&new_only=&limit=&offset=` | 文件题录，按发布日期倒序 |
| `GET /api/documents/{id}` | 单个文件 |
| `GET /api/stats` | 来源健康分布、文件总数、近 7 天新增 |

## 配置

全部通过环境变量（前缀 `YOUJU_`）或 `backend/.env` 设置，见 `src/youju/config.py`：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `YOUJU_DATABASE_URL` | `postgresql+psycopg://localhost:55432/youju` | 数据库 |
| `YOUJU_PER_HOST_INTERVAL` | `3.0` | 同一网站两次请求的最小间隔（秒） |
| `YOUJU_MAX_WORKERS` | `8` | 同时抓取的网站数 |
| `YOUJU_FAILURE_ALERT_THRESHOLD` | `3` | 连续失败几次告警 |

## 测试与检查

```sh
uv run ruff format .   # 格式化
uv run ruff check .    # 代码检查
uv run pytest          # 全部测试（需要数据库的测试连 youju_test 库；数据库没启动时这些测试会跳过）
```

CI（`.github/workflows/ci.yml`）在每个 PR 上运行同样的检查，并要求数据库测试必须执行。

## 目录

```text
src/youju/
  config.py            配置
  db.py / models.py    数据库连接与数据模型
  migrations.py        迁移入口（alembic/ 下是迁移脚本）
  cli.py               命令行
  api/app.py           只读 API
  crawler/
    fetcher.py         礼貌抓取：robots、限速、编码、兼容老旧加密设置
    listing.py         列表页识别：通用规则 + JCMS datastore + 脚本内 JSON
    dates.py / urls.py 日期解析、网址规范化
    service.py         检查来源、入库、健康状态
  sources/importer.py  来源清单导入
tests/                 测试；fixtures/html/ 是真实政府网站列表页快照
```
