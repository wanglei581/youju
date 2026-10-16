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
- `YOUJU_PAY_TERMS_VERSION`：现行《付费服务协议》的版本号（和小程序 content/pay-terms.js 的 version 一致）。
  下单必须带 `terms_version`（用户勾选同意的版本），随订单保存；配了这个变量时，版本不一致返回 409，
  不配只记录不比对（Codex 复审后加，10/10）。
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
“未收回段”按**付款时间**取（结束时间晚于付款时间的段），不按确认时间：付款时旧段还没结束、
确认却晚到时，新段仍接在旧段后面，不会重叠少给天数（Codex 复审后改，10/10）。
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

## 奖励天数（2026-10-10）

奖励是客户工作台使用天数，权益与购买相同：compare / track 每次最多 500 行，次数不限，
仍先使用当月免费量。奖励接在发放时该用户所有未收回段的最晚结束时间后面；
没有未来有效段时从发放时间开始。不能转让、不能折现，注销账号时作废。

`YOUJU_REWARDS_ENABLED=false` 默认关闭，必须同时开启 `YOUJU_ADVISOR_ENABLED`。
关闭时不自动发奖，`rewards-reconcile` 不写奖励，个人奖励和引荐接口 404（包括未登录请求），
注册忽略引荐码，管理台不显示发奖按钮、发奖接口 404。已发出的使用期仍有效。
只有服务端开关，没有审核隐藏开关。支付开关不控制奖励。
`YOUJU_REFERRAL_ENABLED=false` 单独控制引荐，须奖励与工具开关也开启。关闭时引荐接口 404，
注册忽略 invite_code，自动发奖及 rewards-reconcile 均跳过 referral，其他奖励照常。

每个北京时间自然月，所有奖励实际发放天数合计最多 30 天；不足时只给剩余天数。
零天也写流水、备注说明达到上限，但不创建使用期段。同一对象不会重复发奖，
超出的天数或名额不会顺延到下月，月统计按实际发放时间，不按使用期开始时间。

负责人在管理服务运行 `youju rewards-revoke --reward-id N --reason "核实为虚假账号领奖"`
收回一笔奖励，原因必填且为 6–100 字；先加 `--dry-run` 可查看种类、天数、发放时间、
用户内部编号、使用期起止及待收回时长，不写入。奖励开关关闭后仍可运行。
收回撤销对应使用期，仅将未用时长从后续付费及奖励段中前移；重复执行不再前移。
用户奖励记录保留原天数，note 显示“已收回：原因”（原备注附在后面，总长最多 200 字），
并返回 `revoked: true`。零天及已注销用户的奖励只记备注。收回不退还当月额度。

| kind | 用户看到的名称 | 申请天数 | 限制 |
|---|---|---|---|
| first_batch | 第一次用批量功能 | 3 | 第一次 compare / track 成功完成，每个用户一次 |
| correction | 纠错被采纳 | 1 | 管理员采纳，一条反馈一次 |
| source | 提的栏目已接入 | 3 | 管理员确认接入，一条反馈一次，每人每月最多 2 次实际到账 |
| referral | 请来一位同行 | 7 | 新用户第一次成功完成批量任务时，仅给引荐人，每月最多计入 4 位 |
| deadline_clue | 补截止日期被采纳 | 1 | 人工通过后只奖日期相同且最早提交者；每人每月最多 10 天，同一通知同一性质一次 |

只计算直接引荐的一层，新用户不会因引荐码多得天数，只按普通规则拿 first_batch。
引荐码由安全随机数生成，固定 12 字符，不编码用户编号或 openid；老用户首次请求时生成，
不回填。只在第一次建立用户记录时写引荐关系；已有记录更新协议不写关系，
无效码（含超长或类型不符）忽略，自用码不记录。接口不公开被引荐人身份。
`reward_claims` 保留 openid 的用途分离 HMAC-SHA256 假名、类型、记录时间；复用 app_keys 程序密钥，
不存 openid 或用户外键，注销不删除。first_batch、referred（曾使用过账号）、referral（已触发引荐奖）
按假名和类型查重；引荐开启时所有用户注销均追加 referred，首次奖励/引荐奖励有历史时也追加对应凭据，
时间统一为注销时刻，避免旧凭据已满 180 天时立即丢失资格限制。引荐关闭时不追加 referred。
180 天内重新注册不重复领取首次奖励或建立引荐关系。`youju cleanup` 清除满 180 天记录。
待办：启用前在隐私保护指引增加防重复领奖假名的用途、注销后保留及 180 天期限；本次不改 docs/。

新迁移 **0027**，0026 不变：

- `pass_rewards`：`id,user_id,kind,requested_days,granted_days,ref_type,ref_id,granted_at,note`。
  `(ref_type,ref_id)` 唯一；用户外键允许置空，天数和类型有 CHECK 约束。
  `ref_type` 分别使用 `user_first_batch`、`user_referral`、`feedback`、`document`；
  first_batch 和 referral 的 ref_id 是首次完成者编号，其余是反馈或通知编号。
  截止线索使用 `deadline_` 加性质 SHA256 前 20 位作为 ref_type、主通知编号作为 ref_id，
  唯一键保证同一通知同一性质只奖励一次，补发同时检查归并组内旧编号的流水，撤回核对不清除奖励流水。
  两种管理台操作共用反馈唯一键，不能对同一反馈各发一遍。
- `reward_claims`：`pseudonym,kind,claimed_at`；三列为联合主键（直接修改未上线的 0027），类型为 first_batch/referred/referral。
  无身份外键，凭据只追加；app_keys 的 reward_claim_queue 行锁串行化查重和追加，
  API 不锁凭据表、不需要 UPDATE/DELETE。零天奖励也消耗首次资格，关系/发奖与凭据同事务提交。
- `deadline_clues`：`id,user_id,document_id,kind,date,time,quote,status,submitted_at,processed_at,deadline_id`。
  kind 沿用 DocumentDeadline，time 可空，quote 脱敏后最多 200 字，status 为 pending/accepted/rejected；
  用户、候选外键允许置空，文件删除级联删除。公开接口不输出线索或提交人。
- `document_deadlines.origin` 新增 user；evidence 扩至 200 字，完整保留线索句子；user 指纹有独立前缀，规则重跑不覆盖或碰撞。
- `workbench_passes.order_id` 改为可空，新增唯一可空外键 `reward_id`。
  CHECK 保证订单和奖励来源有且只有一个。奖励流水通过 reward_id 反查对应使用期，
  零天没有对应段，不增加循环外键。
- `users` 增加 `invite_code`（唯一可空）、`invited_by_user_id`（自关联外键，注销时置空）、
  `invited_at`、`batch_completed_at`（最少首次完成凭据，任务删除后仍能补发、去重）。
- 正式启用前运行迁移，并执行 `deploy/mysql_accounts.sql` 中新增的授权：deadline_clues 为 INSERT/UPDATE/DELETE，
  reward_claims 仅 INSERT，pass_rewards 仅 UPDATE（API 注销时断开流水关联，发奖 INSERT 由 admin 执行）。
  workbench_passes 的 INSERT/UPDATE 仍为支付 API 所需。没有奖励、线索、支付历史且开关关闭时，
  注销对这些新表只做普通 SELECT，兼容迁移已完成、追加授权尚未执行的阶段。
  本次只验证指定测试库，不执行正式库操作。

| 接口 | 最终字段 / 行为 |
|---|---|
| `GET /api/v1/me/rewards` | `monthDays,monthLimit,items[]`；最近 50 条，`kind,name,requestedDays,days,grantedAt,note,revoked`，不返回关联人或对象编号 |
| `POST /api/v1/me/deadline-clues` | `{document_id,kind,date,time?,quote}`；201 返回下述线索字段；关闭奖励时 404 |
| `GET /api/v1/me/deadline-clues` | 本人最近 50 条 `items[]`，按提交时间、编号倒序；开关关闭仍可查看 |
| `DELETE /api/v1/me/deadline-clues/{id}` | 仅本人待核对线索，204；已处理 409；开关关闭、协议更新后仍可删除 |
| `GET /api/v1/me/data` | 加 `deadlineClues,deadlineCluesTotal`，最多 500 条线索元数据及总数 |
| `GET /api/v1/documents/{id}` | 加 `can_submit_deadline_clue` 布尔值，不含他人线索 |
| `GET /api/admin/deadlines` | 候选加 `deadline_clues[]`，只含待核对线索，不含提交人；关闭奖励时为空 |
| `GET /api/admin/deadline-clues` | `enabled,items[]`；线索字段加 `title,url,can_build_candidate`；关闭时 false/空数组 |
| `POST /api/admin/deadline-clues/{id}/candidate` | 无业务请求体，返回 `deadline_id,status=pending`；只在整份通知无任何候选时建候选 |
| `POST /api/admin/deadline-clues/{id}/reject` | 无业务请求体，返回 `status=未采纳`；两项写操作沿用管理口令和操作留痕 |
| `GET /api/v1/me/invite` | `inviteCode,monthCount,monthLimit,rules[]`；只显示实际计入人数，每月上限 4 |
| `POST /api/v1/me/agreement` | 原字段加可选 `invite_code`；仅 general 的新记录写入关系 |
| `GET /api/admin/feedback` | 原列表加 `rewardsEnabled,rewardKind,rewardDays`；未发奖的后两项为 null |
| `POST /api/admin/feedback/{id}/reward/{kind}` | kind 为 correction 或 source；沿用 X-Admin-Token 和操作留痕，校验反馈类型，成功同时标为已处理；返回 `alreadyGranted,days,message,note`，重复返回“已经发过”（无 note） |

线索返回字段：`id,document_id,kind,date,time,quote,status,submitted_at,processed_at,deadline_id`，
status 给中文“待核对 / 已采纳 / 未采纳”，时间 UTC ISO 8601，日期 YYYY-MM-DD，time 为 HH:MM 或 null。
提交须登录并同意当前协议，文件须公开可见且属于申报通知；缺发布日期不接收。
日期含发布当天至三周年当天（闰日三周年取二月末）；原句去首尾空白后 6–200 字，
复用纠错的微信内容安全检查并脱敏，所有环境均检查，检查不通过或服务出错均不写库。
同人同通知同性质只留一条待核对；每人最多 20 条，全站最多 50 条，重复/满额/已有同性质
核对通过日期均 409。全站满额说明“线索暂时收满了，过几天再来”；已有日期说明
“这份通知的截止日期已经核对过了”。用 app_keys 的 deadline_clue_queue 名称原值更新占用队列行锁，串行计数防并发超额；
部署脚本仅补充该表 name 列 UPDATE 权限，程序密钥 value 不可由 API 修改。
详情布尔值按奖励及工具开关、申报通知、无核对通过截止、全站未满计算；登录、日期和个人限制
仍由提交接口检查。归并的多个网址按主通知处理，转载组各版本仍独立。

API 提交不锁 documents/sources，依靠 app_keys 队列锁与管理端建候选、核对、导入串行化；
原句脱敏后再次校验 6–200 字，超长返回中文 422。
核对台列出全部待核对线索，只显示日期、性质、原句及提交时间，不显示提交人；
只有同一性质的待核对候选阻止建 origin=user 候选，已驳回或其他性质候选不阻止；每条线索均可不采纳。
之后仍须负责人对照原文点通过/驳回。通过按实际核定日期采纳同性质同日期待核对线索，
其他日期标未采纳（时间不参与匹配）；采纳/未采纳状态和核对在同一事务写入。
仅最早提交者拿 1 天；发奖独立提交、失败记固定日志，由 rewards-reconcile 重试。
单条和批量导入核对均调用线索结算；导入预览回滚、正式通过后单独发奖。
驳回候选保留线索待核对，可重新建候选或不采纳；撤回核对不追回奖励、不改变线索状态；不采纳按钮可处理无效线索。
关闭奖励时隐藏相关内容，核对通过不处理线索。注销删除待核对线索，已处理保留且断开用户关联，
最早提交者已注销时不把奖励转给后来者。

任务完成和首次完成标记在同一事务持久化，之后 first_batch、referral 分别独立事务发放。
发奖失败只写固定日志，不回滚成功任务、不暴露异常正文；后续完成不自动补发。
admin 服务的定时任务每小时运行一次 `youju rewards-reconcile`（`deploy/crontab`；开关关闭时直接返回）。
`youju rewards-reconcile` 补发仍在库中的用户：读取首次完成标记，也兼容升级前保留的成功任务，
跳过已有流水（包括零天），两种奖分别补发，另补发已采纳线索中最早提交者尚未收到的奖励。补发按补发当天所属月份计算限制，使用期从补发时起。
升级前成功任务已删除且没有首次完成标记的，无法凭空还原，不发奖。
所有发奖先锁用户再写流水和使用期；同时涉及引荐双方时按用户编号排序锁行。
备份恢复的自关联外键核验与表锁已适配引荐关系，备份包含奖励流水和使用期。

退款资格只检查订单自己的段；奖励段上的任务不计入该订单的 paid_usage。
订单接口只查订单，不把奖励段伪装成订单。退款收回订单段时，后续奖励段也按未用时长往前挪。
注销保留奖励流水并置空用户关联，奖励段收回并断开用户，清除引荐关系两边的关联与时间，
清除流水中指向已注销用户的 ref_id；另一方已经拿到的天数不追回。
批量任务收尾用 no_autoflush 先锁用户，再刷新任务，避免引擎已修改结果引用时提前写任务造成与注销互锁。
受限账号测试按部署 SQL 的 GRANT 自动授权到测试库；可跳过“支付权限”之后的追加授权，
仅创建随机密码的 youju_api_test@localhost，结束 DROP USER；root 无 CREATE USER 权限则自动跳过并报告。

本次修复验证（2026-10-11）：指定虚拟环境、指定 youju_test_codex2 测试库，
串行全量 **2055 passed，2 failed，无跳过，732.17 秒**；新增 21 个回归用例全部通过，
其中 3 个受限账号场景实际执行，账号在每例结束后删除。
两条失败均是固定日期数据与当日时钟不一致的旧测试，本次未改其代码：

- tests/test_conditions.py::test_queue_shows_items_with_highlight_context：10 月 10 日截止已过，卡片不再是“30 天内截止”。
- tests/test_digest.py::test_daily_report_cli_sends_once_and_records_ai_usage：CLI 按 10 月 11 日取候选，假 AI 返回的旧通知截止已过，无法生成“AI 建议先核对”。

两条已有警告（Starlette/httpx 弃用、ZIP 重复名称）。
ruff format --check、ruff check、核对台脚本语法、git diff --check 通过。
仍在 feat/rewards，只改 backend，未 commit/push/部署。正式复核、隐私指引更新、
迁移与授权、真实微信内容安全及浏览器验收待办；180 天清理后不再跨注销维持资格限制。

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

## 小程序侧防重复扣款（Codex 复审后加，10/10）

付款后 60 秒没等到服务端确认时，订单号记在本机（`pay.unsettled`，48 小时内有效）。
这时再点“支付”不下新单，接着查这张订单：确认了显示已开通；仍没确认提示“上一笔付款还在确认，先不要再付”；
订单已关闭（或查不到，按已关闭处理）才放开重新支付。用户主动取消支付的不记，约 6 秒后即可重新支付。
服务端另有“十分钟内最多三张待付订单”的限制。

苹果退款问询在本地核验出错或超过两秒时仍答“未使用”：核验不了时宁可让用户能退，
不把没用过的人答成“已使用”。平台对这种情况有没有规定的应答，上线前按真实问询确认。

## 收费第一期补充（2026-10-16，#266，开关仍默认关闭）

- **测试名单**：`YOUJU_PAY_AUDIENCE=whitelist`（默认）时，即使 `YOUJU_PAY_ENABLED=true`，也只有
  `YOUJU_PAY_OPENIDS`（逗号分隔 openid）里的人能看到商品、下单；对全体收费要改成 `all`（创始人另行批准）。
  补单、退款、对账等定时任务只看总开关，已有订单照常处理。判断函数：`payments.selling_to`。
- **沙箱标记**：`GET /api/v1/pay/products` 返回 `sandbox`（`YOUJU_XPAY_ENV=1` 时为 true）。小程序在沙箱下
  对测试名单放开付款按钮（页面写明不扣真钱），现网仍须《付费服务协议》定稿（`content/pay-terms.js` final）。
  服务端同样把关：现网（`YOUJU_XPAY_ENV=0`）没配 `YOUJU_PAY_TERMS_VERSION`（律师定稿版本）时不展示商品、
  不下单、不算在售（`payments.orderable`，商品列表、下单、购买入口共用），不出现看得到价格却付不了款的入口。
- **支付入口**：`GET /api/v1/advisor/capabilities` 和 `GET /api/v1/me` 带 `pay: {enabled, hasOrders}`。
  enabled = 对这个用户在售；hasOrders = 有过订单（停卖后仍能进订单页退款、开票）。都为 false 时小程序不出现任何购买字样。
- **开票申请**（迁移 0030，表 `invoice_requests`）：`PUT /api/v1/pay/orders/{id}/invoice`
  `{title_type: personal|company, title, tax_id?, email}`；每笔已完成、没退款的订单一份，待开时可改；
  单位税号 15/18/20 位数字或大写字母。订单结构新增 `canInvoice`、`invoice`（status 待开/已开/不开、note）。
  已开发票的订单不能自助退款（先冲红）；自助退款或平台退款完成时作废未开的申请。管理台 `/admin/invoices`
  标记已开、不开（不开要写原因）。注销只断开用户、保留记录；订单按保存期清理时一并删除。
  上线前执行 `deploy/mysql_accounts.sql` 里 `invoice_requests` 的授权。
- **下新单前先查上一笔**：`POST /api/v1/pay/orders` 先向微信查这个用户 10 分钟内的待支付单（`query_order`）。
  已付款就按推送同样的核验开通、返回 409，不下新单；核验不过转人工、也返回 409；微信说已关闭就关单、照常下新单；
  查不了返回 503。用来防小程序取消回调不准、付款中途被关掉（本机没记下这一笔）造成的重复扣款。
- **锁顺序**：用户 → 订单 → 使用期 / 发票。自助退款读发票加锁，管理台开票先锁订单再锁发票，
  不会出现「退款查完发票、作废之前被标成已开」。
