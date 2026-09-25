# 本地资料 API 与管理台

这是受控开发服务，非生产部署方案。启动前安装 `requirements.txt`，设置至少 16 位的 `LIBRARY_ADMIN_TOKEN`，运行 `python3 services/api/server.py --host 127.0.0.1 --port 8766`。

管理台：`/admin`；公开查询页：`/`；健康检查：`/api/health`。数据库默认在 `services/api/var/library.sqlite3`，已被 Git 忽略。服务默认只监听本机地址，不应直接暴露到公网。

测试：从仓库根目录运行 `python3 -m unittest discover -s services/api/tests -v`。

资料先登记来源与使用范围，再预览导入、提交待审、人工发布。管理台可把来源降为仅题录或暂停；恢复更高使用权须重新核验，当前本地管理台不提供一键恢复。公开接口读取已发布版本，并按当前来源权限限制摘要、答案和证据。
