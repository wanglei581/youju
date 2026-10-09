# 客户工作台 30 天支付后台（D-058，2026-10-09）

买一次用 30 天，到期不扣钱。只卖一个商品，默认编号 `workbench_30d`。
付款仅开通使用期，用户自行提交 compare / track 任务。任务失败、取消和钱无关。
本次仅改 backend；未开通收费、未提交、未部署，小程序页面另做。

## 配置与迁移

- `YOUJU_PAY_ENABLED=false` 默认关闭；须同时开启 `YOUJU_ADVISOR_ENABLED` 才能展示商品、下单，
  关闭时这两个接口统一 404（含无效 JSON）。**开关只停卖**：订单列表、详情、自助退款不受开关控制，
  关掉收费以后已经付款的用户照样能看订单、退款（Claude 复核时改，10/9）。没有审核隐藏开关。
  工具按原授权开关执行，支付关闭不会冻结已有使用期或对应任务。
- `YOUJU_XPAY_PRODUCT_PASS=workbench_30d`：微信商品编号。
  `YOUJU_XPAY_PRICE_PASS`：正整数分，未配或空字符串时不出售；有效天数为代码常量 30。
  原 100/200 行商品和环境配置已移除。
- 保留 `YOUJU_XPAY_OFFER_ID`、`YOUJU_XPAY_ENV`（默认 1 沙箱）、对应环境的
  `YOUJU_XPAY_APPKEY_SANDBOX/PROD`，以及可选 `YOUJU_WX_APPID/YOUJU_WX_APPSECRET`。
  密钥只由创始人填环境变量，部署不使用 dotenv / secret 文件来源，不打印密钥。
  初始化参数只用于假客户端测试。
- 直接改未上线的 0026（0025 后继），没有新迁移号。新表 `workbench_passes`：用户、
  唯一订单、开始/结束/收回时间，以及最少使用凭据。任务新增 `pass_id`、`usage_source`、
  `usage_released`，去掉订单任务绑定。正式部署前执行迁移与 `deploy/mysql_accounts.sql`
  中新增的使用期 INSERT/UPDATE 授权；本次没有执行正式库操作。
- 推送默认 `/api/wx/push`，可在部署启动前配置 `YOUJU_WX_PUSH_PATH`；仅可信 API 服务开放。

## 接口约定

时间统一返回 UTC ISO 8601；金额为整数分，使用期的右端点不包含在有效时间内。

| 接口 | 请求 / 返回 |
|---|---|
| `GET /api/v1/pay/products` | `items[]: productId,name,price,days`；未配价格或 offerId 返回空列表 |
| `POST /api/v1/pay/orders` | `{product_id,code}`；返回 `orderId,signData,paySig,signature,mode`，201 |
| `GET /api/v1/pay/orders` | 本人最近 20 张，按创建时间及编号倒序，`items[]` |
| `GET /api/v1/pay/orders/{id}` | 本人订单；陌生用户/他人订单统一 404 |
| `POST /api/v1/pay/orders/{id}/refund` | 无业务请求体；返回同一订单结构，不满足条件 422 中文原因 |
| `GET /api/v1/advisor/status` | `enabled,quota,pass`；`quota.compare/track: used,limit,maxRows`，`pass: active,expiresAt` |
| `POST /api/v1/advisor/tasks` | 原 compare/track 入参和幂等请求头；任务返回 `usageSource: free/pass` |

订单结构：`orderId,status`（中文）、`amount,paidAt,passStartsAt,passEndsAt,passRevokedAt,
canRefund,refundUnavailableReason`。不返回名单或任务输入。
`expiresAt` 显示当前有效段加上后面连续且未收回段的最终到期时间；有空档时仅显示当前连续段。
未到开始时间的未来段不会让 `active` 变为 true。
下单仍需同意工具使用说明；用户行锁下十分钟最多三张待付订单；只保存价格、商品、环境快照，
不接受名单和预检快照，不保存 code/session_key，没有前端“确认付款”接口。

## 付款确认与使用期

推送和 query_order 查单兜底仍核对 openid、业务单号、环境、状态、两个金额、有效付款时间。
发货推送另核商品、数量、原价/实价、Attach、微信支付信息。JSON/XML 按原格式应答；
核验失败 ErrCode 非零，未知本地订单只记允许字段摘要并应答成功。
核验通过后同一事务写订单和使用期，提交后调用 notify_provide_goods；通知失败会补发，
20 次失败转人工并告警。重复推送、补单不重复开通，也不创建工具任务。

所有涉及提交任务、开通、退款与注销的写操作统一先锁用户，再锁订单/使用期。
注销在修改使用期前先锁任务，与任务完成/取消的 task → pass 锁序一致。
新段从付款时间和未收回段最晚结束时间中的较晚者开始，加 30 天。
当前有效要求 `starts_at <= now < ends_at` 且未收回。
退款只收回该订单的一段；它后面如果还排着没收回的段（一次买了两张、退掉前面那张），
这些段按被退掉的剩余时长整体往前挪，不留空档（`revoke_pass`，Claude 复核时改）。
晚到的已确认付款也开通；已注销账号的晚到付款转人工，不恢复身份。

免费量：compare、track 分别每北京时间自然月 3 次，每次最多 50 行；底层仍按日记账，按月汇总。
有当前使用期优先记录为通行证，每次最多 500 行，不消耗免费次数。
无使用期先检查行数，再检查月次数；429 的 `detail` 为 `{code,message}`：
`FREE_ROW_LIMIT`（超过 50 行）、`FREE_MONTHLY_LIMIT`（本月免费次数用完）；
有使用期超过 500 行为 `PASS_ROW_LIMIT`。失败/取消免费任务退回原提交日期的次数，
`usage_released` 保证只退一次；解读类原有每日额度与授权保持不变。

有使用期也先用免费的量：50 行以内、当月免费次数没用完的任务记为免费（`usage_source=free`，不挂 `pass_id`）；
超过 50 行，或当月免费次数已用完，才记为通行证。这样和付款页写的自助退款条件一致：
“没有提交过超过 50 家的名单、也没有超出每月免费次数”（Claude 复核时改）。
“用过付费的量”由 `paid_usage` 统一判断：这段使用期下存在记为通行证的任务，任何状态均算；
失败、取消、删除任务都不会清除使用事实。
使用期留最近一次任务编号、提交时间、完成/结束时间；不存企业名称或结果内容。

## 退款与苹果问询

普通支付仅付款后 7 天内（含恰好 7 天）、未用过这张票的付费量且已开通时允许自助全额原路退。
受理时立即收回该段，并在外部请求前持久化退款单号。重复申请返回原退款状态，不重复建单。
外部退款请求返回后重新锁定读取订单，退款推送先到时不会把完成状态写回待确认。
苹果订单一律 422：“苹果手机订单请在 App Store 申请退款”；未知平台不主动退，交人工。
重复扣款、系统故障、投诉等特殊情况保留人工待办和告警。

退款同号重试、指数退避、查单回查；仅状态 5/8 且 left_fee=0 确认为完成。
部分退款、余额/状态不符、超过六次尝试或 24 小时未确认转人工，不恢复使用期。
苹果开发者不主动退款：本地问询未用答 `result_code=0/result_info=未使用`；
用过答 `1/已使用`，附最近一次任务编号、完成时间；未完成附提交时间及“尚未完成”。
问询不调用微信、不锁订单，独立会话两秒超时留出平台三秒应答余量；超时/异常返回 0 和核验原因。
苹果退款成功通知经查单核验后收回对应使用期，任务结果仍按原留存规则保留。
原按次包、订单任务绑定、任务失败退款、苹果专门重跑和排队超时转人工已删除。
工具队列原有暂时失败最多两次重试保留，与付款无关。

## 补单、对账与隐私留存

每分钟 `pay-run`：两分钟后查待付订单；前 30 分钟每分钟查，之后每十分钟。
48 小时仍无法确认关闭；本地已有订单的晚到推送继续处理。
每轮最多 100 条，退款和待告警人工订单优先，随后补发货，待付查单最后。
支付关闭后仍处理未完结订单；无待办且无当地有订单的待完成对账时不创建 HTTP 客户端。
`pay-run` 不写 job_runs，沿用 job.sh 的失败提醒。

9:30 `pay-reconcile` 下载前一天日账单，生成中由每分钟任务轮询；逐订单号/金额比较，
笔数金额相同但订单不同仍报差异；账单不能直接开通使用期。支付关闭仅在当天有本地付款时下载。
CSV 列名与单位显式配置 `YOUJU_XPAY_BILL_ORDER_COLUMN/FEE_COLUMN/FEE_UNIT`（fen/yuan）。
未配字段直接 failed；下载/解析失败五次转 failed 告警；生成中不计失败。
下载域名用 `YOUJU_XPAY_BILL_HOSTS` JSON 允许列表；仅 HTTPS、不跟随重定向、最大 10 MiB，
不存完整账单。管理台仍显示订单、金额、人工与对账待办，没有新增管理页面。

订单不再存输入。工具输入七天清理、结果文件三十天清理；退款不修改结果文件留存。
事件交易字段允许列表保留 180 天，不存 Attach 原文、投诉正文、个人联系方式或支付签名。
httpx 日志过滤 xpay/code2Session URL；异常稳定中文。
注销收回并断开使用期、订单用户关联，订单用 HMAC 假名，事件去 openid，再删除工具任务和用户。
没有本地订单的注销不锁支付表，兼容正式库尚未授予支付写权限的阶段。
财务订单按 `YOUJU_RETAIN_PAY_ORDERS_DAYS` 清理，暂设 3650 天，使用期审计随订单级联清理。

## track 取数

近 90 天（包含第 90 天、不含未来）发布的公开申报通知；复用搜索的 `_application_ids({closed})`
排除人工核对且已截止的通知。未核对、已变更、无日期仍保留，区域/关键词/支持方式条件照常。
核对过的截止显示日期，未核对的 JSON/Excel 写“截止未核对，请以原文为准”；
未知剩余天数为空，不计入七/十四/三十天截止数。

## 拿不准的地方与上线验收

1. 真实微信沙箱/设备仍须验证 code2Session 云托管免 secret 代理、xpay 免 access_token 代理、
   JSON/XML envelope、iOS 非微信渠道信息缺省、通知发货和原路退款。测试只用 MockTransport。
2. X-WX-SOURCE 是平台注入身份头，不是公网签名；须确认平台剥离/注入和公网入口限制。
3. 苹果问询 pay_order_id 是业务还是微信单号，当前兼容两者；order_type 官方枚举及问询字段位置
   仍需真实样本和高负载三秒测试。正在执行的任务不能提供未来完成时间，凭据明确写“尚未完成”。
4. 账单真实列、金额单位、压缩形式、支付/结算日、退款净额、iOS 日交易口径、沙箱下载和允许域名
   均待真实账单验收；不明确时不能标对账成功。
5. 3650 天财务留存是占位配置，待财务/agy 确认；价格、许可门槛和生产开启由创始人决定。
6. 收回前面的段时后面的段会往前挪（见上）；订单页显示的是每张订单自己的起止，挪动后以接口返回为准。
7. Claude/agy 的正式复核、迁移/授权部署、小程序页面和上线批准仍待后续任务。

官方接口来源沿用原草稿；2026-10-09 本环境重新访问微信文档未成功，未声明本次重新核实平台规则。
业务退款规则按创始人本次 D-058 指令实现。

- [虚拟支付与苹果问询](https://developers.weixin.qq.com/miniprogram/dev/platform-capabilities/business-capabilities/virtual-payment.html)
- [query_order](https://developers.weixin.qq.com/miniprogram/dev/server/API/VirtualPayment/api_query_order)
- [notify_provide_goods](https://developers.weixin.qq.com/miniprogram/dev/server/API/VirtualPayment/api_notify_provide_goods)
- [download_bill](https://developers.weixin.qq.com/miniprogram/dev/server/API/VirtualPayment/api_download_bill)
- [云托管推送官方示例](https://github.com/WeixinCloud/message-push-nodejs/blob/main/index.js)

## 验证

使用指定虚拟环境，`YOUJU_TEST_DATABASE_URL` 指向 `youju_test_codex2`，pytest 串行执行，
所有微信 HTTP 使用假客户端；最终检查数字见本次交接报告。

最终验证（2026-10-09）：

- 在 backend 使用指定虚拟环境执行 `ruff format .`、`ruff check .`，通过。
- 串行执行：`YOUJU_TEST_DATABASE_URL='mysql+pymysql://root@127.0.0.1:33306/youju_test_codex2?charset=utf8mb4' YOUJU_REQUIRE_DB=1 PYTHONPATH=$PWD/src /Users/wanglei/youju-worktrees/ai/backend/.venv/bin/python -m pytest -q`。
- **1893 passed，未跳过数据库测试，304.93 秒**。两条警告分别为已有 Starlette/httpx
  弃用提示与附件图片夹具 ZIP 重复名称提示，无测试失败。
- `git diff --check` 通过；分支仍为 `feat/pass-30d`，改动仅 backend，未 commit/push/部署。
- 独立只读审阅已确认并发修复，记录见
  [D-058 后台审阅](reports/D058_通行证后台审阅_2026-10-09.md)。
