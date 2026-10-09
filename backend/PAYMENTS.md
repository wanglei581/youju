# 名单比对按次支付后台（2026-10-04）

仅修改 backend，未提交，未开通收费。依据 D-047 和创始人提供的接口规则。

## 配置和部署

`YOUJU_PAY_ENABLED=false` 默认关闭；必须同时开启 `YOUJU_ADVISOR_ENABLED`。
开关只停新下单：关闭任一开关时下单、商品、订单查询接口返回 404“没有找到接口”，包括无效 JSON。
已有订单不冻结：`youju pay-run` 先查本地库，只要还有未完结订单（待支付、处理中、退款中、
未告警的人工订单、未通知发货的已完成订单）就照常处理退款、查单和告警；没有这些订单、
也没有“当天有本地订单”的待完成对账时直接返回，不创建 HTTP 客户端、不访问微信。
`youju pay-reconcile` 关闭时只在前一天有本地已付款订单时下载账单。
推送对本地已有订单照常处理（晚到的付款、退款、投诉）；本地没有的订单只记摘要、应答成功。
关闭支付会暂停领取付费任务，免费 compare/track 和 AI 任务照原有授权执行；
处理中订单的付费任务排队超过 10 分钟未执行（或运行中断超过 10 分钟无人回收），
订单转人工并告警，不自动退款。排队时间按任务写入的 expires_at 减 30 天推算。

商品默认为 `compare_100` / `compare_200`，行数上限 100 / 200。
`YOUJU_XPAY_PRICE_100` / `YOUJU_XPAY_PRICE_200` 是整数分，默认空，不配价格不出售。
商品编号可用 `YOUJU_XPAY_PRODUCT_100` / `YOUJU_XPAY_PRODUCT_200` 对应微信后台实际商品。
配置 `YOUJU_XPAY_OFFER_ID`、`YOUJU_XPAY_ENV`（默认 1 沙箱）；
对应的 `YOUJU_XPAY_APPKEY_SANDBOX` / `YOUJU_XPAY_APPKEY_PROD` 是 SecretStr。
三个支付密钥（包括 `YOUJU_WX_APPSECRET`）不从 dotenv 或 secret 文件来源取值。
初始化参数仅保留供假客户端测试；部署密钥由创始人自己填环境变量。

迁移新增 0026（0025 advisor_agreement 的后继），不修改旧迁移。部署由 Claude 审阅后执行；
API 表权限见 deploy/mysql_accounts.sql 最后的两条 GRANT，不在本次执行。
消息推送默认 `/api/wx/push`；用 `YOUJU_WX_PUSH_PATH` 在启动前配置，
只在 API 服务开放，核对台服务拒绝这个路径。云托管开放接口服务需配置白名单。

## 接口和状态

- `POST /api/v1/pay/orders`：`{product_id, rows, code}`。使用说明同意、预检输入、
  商品上限和用户行锁下的十分钟三张待付订单限制；保存价格/环境/输入快照。
  返回 `orderId/signData/paySig/signature/mode`，不保存登录 code 或 session_key。
- `GET /api/v1/pay/orders/{id}`：只返回当前用户订单的中文状态和任务编号。
  查不到和属于别人均为 404。没有前端“确认付款”接口。
- 发货推送和查单兜底：按订单号锁行，query_order 确认 openid 查询上下文、
  业务单号、环境、状态 2/3/4 和两个金额；推送还有商品、数量、价格和支付信息核验。
  事务中创建 compare 任务、写 task_id，不调用免费额度账本。
  订单号唯一约束、任务幂等键和行锁共同防止重推执行多次。
  查单已确认付款（2/3/4）但金额不符或 paid_time 无效：订单转人工、error 写明原因、
  alerted_at 置空由每分钟任务告警，不建任务；推送仍应答失败，重推不重复告警。
- 推送按 JSON/XML 原格式应答；失败应答 ErrCode 非零，让微信重试。
  退款和投诉推送使用官方的 `MchOrderId/WxOrderId`，不使用发货推送字段代替。
  投诉只存交易摘要，转人工待办；不存投诉详情。
- 每分钟 `pay-run`：补两分钟后的待付订单（三十分钟内每分钟查一次，之后每十分钟）；
  创建超过 48 小时仍查单失败的订单关闭，error 为“超过 48 小时无法确认支付，已关闭”
  （真付了款的由推送或账单对账转人工）。每轮最多 100 条：退款中、任务已失败或缺失的
  处理中订单、待告警的人工订单排最前，其次是其他已付款订单，待支付查单放最后。
  先持久化任务，再通知发货；通知发货失败计入 `provide_attempts`，满 20 次不再重试，转人工告警。
  原队列最多初次加两次重试，最终失败后普通支付全额退款。
  退款单号在外部请求前持久化，同号重试并回查；只有查单 5/8 且 left_fee=0 才确认完成。
  退款部分成功、金额/余额异常、注销中的未完成订单进入人工待办。
- iOS：官方 query_order 的 order_type=7/8 判为 iOS，0/1 判为普通支付；
  可用平台字段及值映射替换，未知值转人工，绝不主动退款。
  首次最终失败给同一名单一次重跑；再次失败进人工待办并发管理提醒。
  问询只读本地任务，不调用微信、不锁订单；本地核验超过两秒或出错时以 0 放过，
  独立后台会话可继续安全结束。返回 `result_code/result_info/evidence`。
  未成功为 0；成功为 1，凭据包含任务编号和 UTC 完成时间。
  问询摘要不挂订单外键，避免发货事务持锁时阻塞三秒内应答。

## 对账、管理台和隐私

9:30 `pay-reconcile` 触发前一天日账单；生成中由每分钟任务继续轮询。
每天唯一记录，逐订单号及金额比较，笔数/金额相同而订单不同也报差异。
CSV 解析列名和金额单位需要显式配置：`YOUJU_XPAY_BILL_ORDER_COLUMN`、
`YOUJU_XPAY_BILL_FEE_COLUMN`、`YOUJU_XPAY_BILL_FEE_UNIT`（fen/yuan）。
没配两个列名时不下载账单，当天直接标为 failed，differences 为“未配置账单字段”。
下载或解析失败计入 `attempts`，满 5 次转 failed 并告警，不再占用每轮 3 天的轮询名额；
账单生成中（268490011）或还没有下载地址不计次，下一分钟继续轮询。
下载域名用 `YOUJU_XPAY_BILL_HOSTS` JSON 数组允许列表，只接受 HTTPS、不跟随重定向，
内容最大 10 MiB，不存完整账单。格式未知、生成未完成、下载失败都留待核对，不能显示对账成功。
管理台总览显示今日订单数、实付金额、人工订单数及对账待完成、差异、失败天数；
差异、未完成和失败的账单进入待办，未新增管理页面。
`pay-reconcile` 沿用 job.sh/job_runs 记账；`pay-run` 每分钟一次，和 advisor-run 一样
不写运行记录（失败仍由 job.sh 提醒），也不在总览的定时任务列表里。

订单和任务输入都按原下单时间七天清理，重跑不延长。
事件摘要使用交易字段允许列表，保留 180 天；不存 attach 原文、投诉正文、
个人姓名手机号及支付签名。httpx 请求日志过滤 xpay/code2Session URL，
避免 query 中的 pay_sig/AppSecret 被 INFO 日志记录；异常统一稳定中文。
注销先用普通查询确认有没有订单，有才加锁处理（youju_api 在支付授权前只有 SELECT 权限，
没有订单时不能发出 SELECT ... FOR UPDATE）；删除输入、断开 user_id、把关联事件 openid 去掉，
再删现有任务和账号；用户删除已完成的付费任务时先锁订单、再锁任务，和退款确认的加锁顺序一致；
注销后的晚到付款推送临时使用推送身份查单，核验后转人工，事件不重新保存 openid。
金额、订单编号保留并改为程序 HMAC 假名。完成时间单独保留供退款审计。
终态财务订单按 `YOUJU_RETAIN_PAY_ORDERS_DAYS` 清理，暂设 3650 天，待财务/合规确认。

## 拿不准的地方及上线验收

1. A 方式：云托管开放接口服务能否不带 secret 调通 code2Session；未线上验证，
   走 `http://api.weixin.qq.com/sns/jscode2session`。
   B 方式：配置 `YOUJU_WX_APPID/YOUJU_WX_APPSECRET`，只由创始人填；带 secret 时改用
   `https://api.weixin.qq.com/sns/jscode2session`，secret 不经明文 http。两个方式都校验
   返回 openid 等于登录用户，session_key 仅用于签名。xpay 的免 access_token 服务代理也待实测。
2. `X-WX-SOURCE` 基本校验参照微信官方示例；已配 appid 时还比对 `X-WX-APPID`。
   头本身不是公网签名：必须确认平台注入/剥离规则及公网入口限制，不能在允许伪造头的入口开启。
   还要实测推送 envelope、JSON/XML、iOS 非微信渠道的 WeChatPayInfo 缺省情况。
3. iOS 的 pay_order_id 属于业务单号还是微信单号，文档表格没有明确说明；目前都匹配。
   需使用真实沙箱/设备推送确认问询包装、应答字段位置和高负载下三秒响应。
   order_type 枚举已核实，但仍需线上订单样本确认，不识别则人工处理。
4. 日账单接口只给 begin_ds/end_ds 和 URL，未给文件列、币种单位、压缩形式及日汇总口径。
   目前提供可配置 CSV 适配器；上线需取真实文件确认是否为交易明细、支付日或结算日，
   是否含退款净额与 iOS 交易。当前本地比较支付日的实付总额，所有平台均保留，
   不会把 iOS 的苹果月结算账单当日账单。口径不一致应保持待核对，需按样本调整适配器。
   沙箱日账单是否可下载、URL 域名允许列表、账单晚到的重跑流程均待线上确认。
5. 财务留存 3650 天是可调占位期限，需财务/agy 确认后更新，不能作为已核实的法定期限。
   商品价格、发布生效、退费规则及收费门槛仍须创始人批准；本次不打开功能。
6. 独立成员复核尚待 Claude/agy；本工作区没有提交、合并或生产操作。

核对日期：2026-10-04。官方来源：

- [虚拟支付，签名公开示例、推送字段与 iOS 问询](https://developers.weixin.qq.com/miniprogram/dev/platform-capabilities/business-capabilities/virtual-payment.html)
- [query_order，order_type 枚举](https://developers.weixin.qq.com/miniprogram/dev/server/API/VirtualPayment/api_query_order)
- [download_bill](https://developers.weixin.qq.com/miniprogram/dev/server/API/VirtualPayment/api_download_bill)
- [notify_provide_goods](https://developers.weixin.qq.com/miniprogram/dev/server/API/VirtualPayment/api_notify_provide_goods)
- [微信官方云托管消息推送示例](https://github.com/WeixinCloud/message-push-nodejs/blob/main/index.js)

测试用 MockTransport，不调用微信；指定 youju_test_codex3 库，全量迁移对照和 pytest，
ruff format/check。旧管理台统计断言补支付待办；0015 测试限定比较 0015 自身列变化，
仍验证回滚清理和升级 head 的完整列恢复。


改动文件索引：

- `src/youju/config.py`：支付开关、密钥环境来源、商品价格与账单适配配置。
- `src/youju/models.py`、`alembic/versions/0026_payments.py`：订单、事件、对账和付费任务外键。
- `src/youju/xpay.py`、`src/youju/payments.py`、`src/youju/api/pay.py`：签名/传输、支付生命周期、接口和推送。
- `src/youju/advisor_tasks.py`：付费来源隔离，不扣/退免费额度，关闭时不领取付费任务。
- `src/youju/api/me.py`、`src/youju/notify.py`：注销假名化和留存清理。
- `src/youju/cli.py`、`src/youju/jobs.py`、`deploy/crontab`、`deploy/job.sh`：补单退款/对账
  （关闭后只处理未完结订单）及任务记账（pay-run 不记）。
- `src/youju/api/advisor.py`：删除付费任务先锁订单再锁任务。
- `deploy/mysql_accounts.sql`：支付 API 最小写权限的部署草稿。
- `src/youju/api/app.py`、`src/youju/admin/portal.html`：路由隐藏、API 推送入口、支付总览/待办。
- `tests/test_payments.py`：假 HTTP、并发发货、200 行真实引擎、退款重跑、隐私与对账；
  第二轮审阅 9 个问题各有 `test_fix_N_*` 测试（先确认失败再修复）。
- `tests/test_admin_portal.py`、`tests/test_me.py`、`tests/test_ai_clause.py`：适配新增统计、清理计数及迁移列。
- `PAYMENTS.md`：交接和未核实点，留待 Claude/agy 复核。


最终验证（2026-10-04）：

- 在 backend 执行 `uv run ruff format .`、`uv run ruff check .`：通过。
- `YOUJU_TEST_DATABASE_URL="mysql+pymysql://root@127.0.0.1:33306/youju_test_codex3?charset=utf8mb4" YOUJU_REQUIRE_DB=1 uv run pytest -q`：1544 passed，1 条已有 Starlette/httpx 弃用提示，未跳过数据库测试。
- 支付专项 118 项（含参数化）：包含 15 次并发事务发货只建一个任务、200 行真实比对引擎执行、慢数据库下 iOS 两秒超时应答，
  以及第二轮审阅的 9 个问题。微信支付 HTTP 全部为假客户端。
- `git diff --check`：通过；分支 feat/payment，仅 backend 工作区改动，无提交。
