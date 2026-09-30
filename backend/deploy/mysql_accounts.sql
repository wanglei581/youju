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
-- 访问日志（网络日志留存）：只能新增，不能改、删（归档和删除由 admin 服务做）
GRANT INSERT ON youju.access_logs TO 'youju_api'@'%';
-- 访问日志用户编号的密钥：库里还没有时第一次用到的服务生成；只能新增，改不了已有的
GRANT INSERT ON youju.app_keys TO 'youju_api'@'%';
