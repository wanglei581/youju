-- 云托管 MySQL：建库和两个应用账号（云托管 MySQL 最多 3 个账号：root 和下面两个）。
-- 在云托管控制台 → MySQL → 数据库管理，用 root 登录后执行。
-- 执行前把 <改成你的密码> 换成各自的随机密码（8–64 位，含大小写字母、数字、符号中的三种）。
-- 密码只填在这里和云托管的环境变量里，不要发给任何人，也不要存进仓库。

-- ① 第一次部署 admin 服务之前执行 ------------------------------------------------

-- 区分大小写、不忽略尾部空格（和测试时一致；迁移建表时每张表也写明了这个排序规则）
CREATE DATABASE IF NOT EXISTS youju CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin;

-- admin 服务：启动时建表迁移，定时任务写入抓取数据，核对台
CREATE USER IF NOT EXISTS 'youju_admin'@'%' IDENTIFIED BY '<改成你的密码>';
GRANT ALL PRIVILEGES ON youju.* TO 'youju_admin'@'%';

-- ② admin 服务第一次启动、迁移建好表之后再执行（按表授权要求表已经存在）----------------

-- api 服务（小程序接口）：能读全部表，只能写用户自己的数据
CREATE USER IF NOT EXISTS 'youju_api'@'%' IDENTIFIED BY '<改成你的密码>';
GRANT SELECT ON youju.* TO 'youju_api'@'%';
GRANT INSERT, UPDATE, DELETE ON youju.users TO 'youju_api'@'%';
GRANT INSERT, UPDATE, DELETE ON youju.follows TO 'youju_api'@'%';
GRANT INSERT, UPDATE, DELETE ON youju.subscriptions TO 'youju_api'@'%';
GRANT INSERT, UPDATE, DELETE ON youju.feedback TO 'youju_api'@'%';
GRANT INSERT, UPDATE, DELETE ON youju.events TO 'youju_api'@'%';
-- 顾问任务权限：迁移 0010 建表后，由创始人在正式库手工执行本行一次。
GRANT INSERT, UPDATE, DELETE ON youju.advisor_tasks TO 'youju_api'@'%';
GRANT INSERT, UPDATE, DELETE ON youju.advisor_usage TO 'youju_api'@'%';
GRANT INSERT ON youju.advisor_file_trash TO 'youju_api'@'%';
-- 访问日志（网络日志留存）：只能新增，不能改、删（归档和删除由 admin 服务做）
GRANT INSERT ON youju.access_logs TO 'youju_api'@'%';
-- 访问日志用户编号的密钥：库里还没有时第一次用到的服务生成；只能新增，改不了已有的
GRANT INSERT ON youju.app_keys TO 'youju_api'@'%';

-- 支付权限：仅迁移 0026 完成后，由创始人执行。金额记录不能由 API 删除。
GRANT INSERT, UPDATE ON youju.pay_orders TO 'youju_api'@'%';
GRANT INSERT, UPDATE ON youju.pay_events TO 'youju_api'@'%';
GRANT INSERT, UPDATE ON youju.workbench_passes TO 'youju_api'@'%';
-- 开票申请：迁移 0030 完成后和上面三行一起执行。用户在订单页申请、改抬头，退款时作废未开的申请；不能删除。
GRANT INSERT, UPDATE ON youju.invoice_requests TO 'youju_api'@'%';

-- 奖励权限：迁移 0027 完成后、打开奖励开关（YOUJU_REWARDS_ENABLED）之前，由创始人执行。
-- 要和上面 workbench_passes 那一行一起执行：有奖励使用期的用户提交任务、注销时要改这张表。
-- 线索队列靠"把名称更新成原值"占用行锁来排队；密钥的值仍然不能改。
GRANT UPDATE (name) ON youju.app_keys TO 'youju_api'@'%';
-- API 只在注销时断开流水关联；发奖 INSERT 由 admin 定时任务/核对台执行。
GRANT UPDATE ON youju.pass_rewards TO 'youju_api'@'%';
-- 以前这里还有防重复领取凭据表的 INSERT 授权，迁移 0035 已把表删掉。MySQL 删表不收回按表的授权，
-- 执行过旧版的，迁移后可以收回（没有这项授权时报 1147，不用管）：
-- REVOKE INSERT ON youju.reward_claims FROM 'youju_api'@'%';
GRANT INSERT, UPDATE, DELETE ON youju.deadline_clues TO 'youju_api'@'%';

-- 加量体验权限（D-070 ③）：迁移 0031 完成后、打开体验开关（YOUJU_BOOST_TRIAL_ENABLED）之前，由创始人执行。
-- 用户点"开始体验"时新增一行；注销时随用户行级联删除（MySQL 外键级联不另查子表权限），API 不改也不删。
GRANT INSERT ON youju.boost_trials TO 'youju_api'@'%';
-- 注销时体验记录随用户删除，不另留凭据（10/18 改为注销即删）。
-- 体验期每天的次数记在 advisor_usage（上面顾问任务权限已有），申报季加量不需要新权限。

-- 批量工具两档开卖权限（D-070）：迁移 0032 完成后、打开收费（YOUJU_PAY_ENABLED）之前，由创始人执行；
-- 上面"支付权限"四行要先执行（或一起执行）。
-- 付款确认（微信推送、下新单前查上一笔）时随订单记一笔赠送（续买多送、加量体验折算）：只新增，不删除。
GRANT INSERT ON youju.pass_rewards TO 'youju_api'@'%';
-- 退款时收回随订单送的续买天数（流水备注写"已收回"）；上面奖励段也有这一行，没开奖励也要执行。
GRANT UPDATE ON youju.pass_rewards TO 'youju_api'@'%';
-- 体验期内买了批量工具：体验到付款确认时结束，没用完的折成天数。只能改结束时间。
GRANT UPDATE (ends_at) ON youju.boost_trials TO 'youju_api'@'%';

-- AI 设置心跳权限（D-075）：迁移 0039 完成后由创始人执行。api 最多每 5 分钟写一次自己的 AI 设置指纹
-- （ai_heartbeat 里 service=api 那一行），admin 拿来和自己的比，两个服务设置不一致时往运维群提醒。
-- 不执行也不影响 AI 能不能用（api 只读 admin 那一行就能判断），只是收不到"两个服务设置不一致"
-- "api 开着 AI、admin 却处理不了"这两条提醒；admin 能处理 AI 任务时每天会提醒一次"api 心跳没来"。
GRANT INSERT, UPDATE ON youju.ai_heartbeat TO 'youju_api'@'%';
