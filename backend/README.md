# 有据 · 后端

数据底座与 API：监测全国政府网站栏目，发现新文件，提供只读接口。设计见 [docs/adr/](../docs/adr/)。

## 环境准备

需要 [uv](https://docs.astral.sh/uv/) 和 MySQL 8.0（macOS：`brew install mysql@8.0`；线上用微信云托管自带的 MySQL 8.0）。

```sh
cd backend
uv sync                      # 安装依赖（按 uv.lock）
./scripts/dev_mysql.sh start # 在 backend/.mysql 启动项目内数据库（端口 33306），不改系统设置
uv run youju db-upgrade      # 建表 / 迁移到最新结构
```

`./scripts/dev_mysql.sh stop` 停止数据库；`reset` 清空重建。旧的 `dev_db.sh`（PostgreSQL）只用于把以前的本机数据搬到 MySQL。

## 常用命令

```sh
uv run youju import-sources          # 从 docs/数据来源/全国来源清单_P0_v1.csv 导入来源
uv run youju crawl                   # 检查全部已启用来源
uv run youju crawl --province 山东    # 只检查某个省（国家级来源填"国家"）
uv run youju crawl --source-id 12    # 只检查指定来源
uv run youju fetch-details --limit 50  # 抓取尚无详情或已到重试时间的公开政府文件
uv run youju fetch-details --source-id 12 --limit 20  # 只处理指定来源
uv run youju fetch-details --workers 8  # 跨网站并行，同一网站仍遵守间隔
uv run youju fetch-details --since 2026-07-01 --notices-only --limit 300  # 申报类通知回填
uv run youju backfill-titles          # 列表页截断的历史标题用详情页完整标题补全：默认只预览，加 --apply 才写入
uv run youju repost-groups           # 同一份部里通知的各地版本归成转发组，送管理台确认（D-055）
uv run youju extract-deadlines --since 2026-07-01  # 生成待人工核对候选，重复运行不会覆盖核对结果
uv run youju parse-lists --since 2026-07-01  # 补名单详情并解析政府表格附件，单次最多下载 200 个附件
uv run youju rescan-list-details     # 还没读出企业名的名单详情按附件文件服务器新规则重抓补附件：默认只预览，加 --apply 才写入
uv run youju report                  # 来源健康和条目统计
uv run youju serve                   # 启动 API：http://127.0.0.1:8000/docs
```

## API

| 接口 | 说明 |
|---|---|
| `GET /health` | 健康检查 |
| `GET /api/sources?health=&province=&enabled=` | 来源列表及健康状态 |
| `GET /api/documents?q=&province=&dept=&since=&new_only=&limit=&offset=` | 文件题录，按发布日期倒序 |
| `GET /api/documents/{id}` | 单个文件及详情；截止字段只含人工通过项，未通过时 `deadline_text` 为“截止时间见原文” |
| `GET /api/lists/search?q=企业全称或信用代码` | 精确名单结果和单独的近似名称候选；仅查企业和机构 |
| `GET /api/admin/deadlines` | 待核对候选、证据句和原文链接 |
| `POST /api/admin/deadlines/{id}/approve` | 通过候选；请求体含 `reviewed_by`，可选 `date`（YYYY-MM-DD）和 `time`（HH:MM）修改后通过 |
| `POST /api/admin/deadlines/{id}/reject` | 驳回候选；请求体含 `reviewed_by` |
| `GET /api/stats` | 来源健康分布、文件总数、近 7 天新增 |

## 管理台

浏览器打开 `/admin`，输入一次管理口令后使用总览、六个工作页和定时任务记录。
口令只在页面内存中保存，刷新需重新输入。无口令或口令错误时 `/admin` 返回 401；
首次导航的 401 正文提供登录表单，正常打开和刷新不会计入口令输错次数。
旧地址仍可单独使用；`?embed=1` 仅在同源管理台中隐藏原顶栏并向父页面取口令，
核对人、筛选和加载控件仍可用。外部平台链接在新标签页打开，需要单独登录。

`GET /api/admin/overview` 和 `GET /api/admin/job-runs` 要求 `X-Admin-Token`，读取留痕。
待办复用原核对队列；来源异常与日报一致，包含今天启用栏目的失败和漏检。
文件及名单份数按归并后的文件计；名单条数为入库条数；已核对截止为去重后的已通过候选；
待发提醒为未使用的订阅授权，实际发送仍取决于原触发条件。

迁移 `0011` 增加 `job_runs`。`deploy/job.sh` 记录每个步骤，`round.sh` 汇总整轮结果；
任一步失败会标整轮失败，但仍继续后续步骤。每次开始和结束写同一条记录，记账有独立超时，
失败不阻断任务。摘要仅用固定结果和退出码，不复制参数、异常或任务输出。
`cleanup` 删除开始时间早于 90 天的记录；任务页按任务筛选最近 7 天记录并支持继续加载。
容器突然退出或结束记账失败时可能留下“运行中”，不将其误报为成功。

## AI 顾问后台（内部白名单）

迁移 `0014` 扩展任务类型 `interpret`。`YOUJU_AI_ADVISOR_ENABLED=true` 且认证用户
openid 在 `YOUJU_AI_ADVISOR_OPENIDS` 逗号分隔白名单内才可访问；默认关闭、白名单为空。
这两个配置与数据工具开关独立；后台 `advisor-run` 也只领取获准的任务。

`POST /api/v1/advisor/tasks` 携 `Idempotency-Key`，输入
`{"kind":"interpret","doc_id":123}`；同键异输入返回 409。
`GET /api/v1/advisor/capabilities` 分别返回 `data_tools`、`ai_advisor` 的功能和剩余额度。
解读每天每人 5 次，按北京时间计；缓存命中仍计一次，失败和拒答退回额度。

结果从 `GET /api/v1/advisor/tasks/{id}` 的 `result` 获取，含 `items`、`missing`、`verified`、`dropped`、
`refusal`、`generated_at`、`model`、`prompt_version`、`notice` 和校验提示 `warning`。
拒答使用 `status=done`、非空中文 `refusal` 和空条目；调用或解析终败使用 `status=failed`。
服务端忽略模型的 `text`，只按引用定位；仅合并空白、全角 ASCII 转半角，不做模糊匹配。
引用至少 6 字，优先在指定段内查找；段内找不到时只接受全文唯一命中并纠正段号。
命中扩展为完整正文上的最小连续完整句（超长句可跨段），展示和引用都回填原文原样；
含脱敏标记的句子丢弃，同类型同原文范围去重。
`dropped_reasons` 只保存在数据库和评测结果中，不通过用户接口或“我的数据”导出返回。
任务列表仅返回解读元数据和 `hasResult`，不逐行复核；数据工具接口保持原行为。
解读另含 `attachments`（归并组内 `document_attachments` 的已知记录数）和
`attachment_notice`；大于 0 时固定提醒申请表、材料清单可能在附件中，不推断附件内容。
`disclosure` 保存生成时的 `modelLabel`、`filingNo`，capabilities 同名字段返回当前配置。
快照段落 `start/end` 是完整脱敏原文的字符偏移（左闭右开）；快照不通过用户接口返回。
人工核对值由后端单独写入 `verified`，标“已核对”，不交给模型改写。

缓存按正文、核对版本（含附件个数）、提示词版本和模型共享，原始生成时间超过 30 天不复用；
查询和复用都复核正文与下架状态。输入 7 天清空，结果和快照 30 天清空；
删除任务和注销删除整条记录。网络、429、5xx 最多重排两次，解析失败直接终败。
管理台总览显示开关、白名单人数、近 7 天任务和失败数（拒答不计调用失败）。
本轮只有后台，无小程序页面、导出或条款对照。

## 解读评测

在 `backend/` 下运行 `uv run python scripts/eval_interpret.py --limit 30 --out /tmp/interpret-eval`。
可加 `--doc-ids 1,2,3` 限定候选，`--dry-run` 只选文、不调用模型。数据库沿用现有配置；
没有 AI 密钥时仅允许 dry-run。脚本在数据库只读事务内读取，不创建任务、不扣额度，
不写任何表（包括 AI 用量表），每篇最多一次请求、失败不中断。
只选 `interpret.prepare` 通过的文件；按来源 ID 轮取，短（≤1500 字）、中（≤6000 字）、
长文轮流补位，候选按文件 ID 排序；库内容相同则选文相同。
输出 `results.jsonl`（文件 ID、保留条目、六类条数、missing、丢弃原因、耗时与用量）、
`summary.json` 和中文表格 `summary.md`；汇总含选中文件 ID、成功篇数、总条数、保留率、
各原因合计、各类覆盖篇数和平均耗时。成功表示模型调用与校验完成，零保留也算成功；
平均耗时包含调用失败、不含 dry-run；错误仅记录类别，不输出原始响应、请求头或密钥。

## 配置

后台 AI 的模型方可配置，使用 OpenAI 兼容接口；连接地址和模型默认值保持兼容。
全部通过环境变量（前缀 `YOUJU_`）或 `backend/.env` 设置，见 `src/youju/config.py`：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `YOUJU_DATABASE_URL` | `mysql+pymysql://root@127.0.0.1:33306/youju?charset=utf8mb4` | 数据库 |
| `YOUJU_PER_HOST_INTERVAL` | `3.0` | 同一网站两次请求的最小间隔（秒） |
| `YOUJU_MAX_WORKERS` | `8` | 同时抓取的网站数 |
| `YOUJU_FAILURE_ALERT_THRESHOLD` | `3` | 连续失败几次告警 |
| `YOUJU_ADMIN_TOKEN` | 未配置 | 管理接口请求头 `X-Admin-Token`；未配置时一律拒绝 |
| `YOUJU_ADVISOR_ENABLED` | `false` | R1.5 数据工具总开关；正式上线前保持关闭 |
| `YOUJU_BOOST_TRIAL_ENABLED` | `false` | 加量体验开关（D-070 ③，见 `youju/boost.py`）；还要数据工具开关开着。打开前先执行 `deploy/mysql_accounts.sql` 的"加量体验权限"，隐私保护指引写明体验记录；关掉后已开始的体验照常用满 72 小时 |
| `YOUJU_SEASON_BOOST_ENABLED`、`YOUJU_SEASON_BOOST_START`、`YOUJU_SEASON_BOOST_END` | `false`、空、空 | 申报季加量（D-070 ⑤）：起止日期（北京时间，含首尾，如 `2026-11-01`）内免费批量次数每月 5 次（每次仍 50 家）；开着就要填两个日期，开始不能晚于结束，配错不启动 |
| `YOUJU_XPAY_PRICE_PASS`、`YOUJU_XPAY_PRICE_PASS_90` | 空、空 | 批量工具 30 天、90 天两档的价格（分，D-070）；没填的那一档不卖。商品编号 `YOUJU_XPAY_PRODUCT_PASS`、`YOUJU_XPAY_PRODUCT_PASS_90` 默认 `workbench_30d`、`workbench_90d`，两档相同不启动。见 `PAYMENTS.md` |
| `YOUJU_PAY_INVOICE_TYPE` | 空 | 能开哪种发票（D-070：付款前要看得到），购买页照这句显示；现网（`YOUJU_XPAY_ENV=0`）没填不卖 |
| `YOUJU_RENEW_BONUS_ENABLED` | `false` | 续买多送（D-070 ⑥）：到期前后 7 天内续买，每次多送 3 天；下单时开着才算数 |
| `YOUJU_CORRECTION_REWARD_ENABLED` | `false` | 纠错被采纳送 1 天（D-070 ⑦，可选），每人每月最多 3 天；还要数据工具开关开着，和旧的 `YOUJU_REWARDS_ENABLED` 分开 |
| `YOUJU_AI_BASE_URL` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | 模型方 OpenAI 兼容接口地址 |
| `YOUJU_AI_MODEL` | `qwen-flash` | 调用的模型标识 |
| `YOUJU_AI_API_KEY` | 未配置 | 模型调用密钥，不记录、不输出 |
| `YOUJU_AI_MODEL_LABEL` | 空字符串 | 对外公示的模型名称 |
| `YOUJU_AI_MODEL_FILING` | 空字符串 | 模型备案编号 |
| `YOUJU_AI_ADVISOR_ENABLED` | `false` | AI 顾问独立开关；内部白名单测试 |
| `YOUJU_AI_ADVISOR_OPENIDS` | 空 | AI 顾问认证 openid 白名单，逗号分隔 |

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
    detail.py           HTML 详情解析与敏感信息脱敏
    detail_service.py   详情抓取、失败记录与入库
    service.py         检查来源、入库、健康状态
  sources/importer.py  来源清单导入
tests/                 测试；fixtures/html/ 是真实政府网站列表页快照
```

转发归组只在人工确认后对外折叠；内容变化由 `youju repost-groups` 重算检测，标为需复核后立即恢复单条显示。0022 在本分支直接修改，已执行旧版 0022 的环境不能靠再次 `upgrade head` 补齐字段，部署前须另行补齐 `needs_review` 和 `confirmed_fingerprint`，旧确认记录无快照时必须重新确认。
