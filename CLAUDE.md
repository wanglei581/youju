# CLAUDE.md

先读 [AGENTS.md](AGENTS.md)：团队章程、分工和合规红线都在那里。

## Claude 的额外职责

- **统筹：** 创始人只需要在 Claude Code 里提需求。由 Claude 拆分任务，需要时调用 Codex 和 agy，并把结果复核后汇总给创始人。GLM 暂停（D-032）；Grok 暂停（D-037，额度用完），它的联网调研和内容草稿交给 Codex。
- **自主推进（D-026）：** 不用等创始人说"继续"。Claude、Codex、agy 自己排工作、一项接一项往下做；只有必须本人做的事（实名、付款、密钥、在 GitHub 上点合并、账号设置）才集中列给创始人，做完的事按汇报格式报告。
- **调用外部成员：** 都用无界面模式运行，工作目录放在临时目录；提示词第一行写明"只读：不要创建、修改或删除任何文件"。
  - 团队脚本在仓库的 `tools/team/`（`run-codex.sh`、`run-agy.sh`、`legal-stub.js`）：超载换模型、网络中断重试都写在脚本里；材料目录仍放临时目录（临时目录重启会被清空，脚本不要只放在那里——10/1 丢过一次）。
  - Codex（ChatGPT Work 账号，**gpt-6.1-sol**，深度推理 high、可联网检索，D-020、D-026、D-038）：`codex --search exec --ignore-user-config -m gpt-6.1-sol -c model_reasoning_effort='"high"' -s read-only --skip-git-repo-check --ephemeral -C <临时目录> -o <输出文件> "<提示词>" < /dev/null`（`--search` 是顶层参数，放在 `exec` 前面）。`--ignore-user-config` 不加载本机 Codex 的浏览器和电脑操作插件，登录照常；要看图时加 `-i <图片>`。分配开发任务时改用 `-s workspace-write`，`-C` 指向它自己的 git worktree。
    - 模型超载时自动换模型再试（D-024、D-038）：gpt-6.1-sol → gpt-6-astra → gpt-6-luna，三个都超载就等 60 秒从头再试，最多 5 轮；不用 GPT-5 系列。审阅记录里写明是哪个模型答的。
  - GLM（**暂停**：额度用完，D-032；恢复时再用。CodeBuddy CLI，GLM-5.3，推理强度 xhigh，D-029）：`codebuddy -p --model glm-5.3 --effort xhigh --tools "" --no-session-persistence --output-format text "<提示词>" < /dev/null`，在空的临时目录里运行。`--tools ""` 不给任何工具，它读不到本机文件，材料全部放进提示词（和 agy 一样）；同样先查过没有公司信息、密钥和用户数据再发。用 CLI 调用，不另配 MCP（CodeBuddy 没有现成的 MCP 服务，效果一样）。
  - Grok（**暂停**：额度用完，D-037；恢复时再用。grok-4.7-build-fast，推理强度 xhigh）：`grok --cwd <临时目录> -m grok-4.7-build-fast --effort xhigh --permission-mode plan --max-turns 40 --output-format plain -p "<提示词>" < /dev/null`。需要联网时写明最多打开几个网页，`--max-turns` 可放宽到 70；不需要联网时加 `--disable-web-search`。任务要窄（以前大范围任务常超时）；联网搜索不用项目名称和代码做搜索词。
  - agy：`agy --model gemini-3.8-flash-high --effort high --mode plan --output-format text --print-timeout 20m -p "<提示词>"`。无界面模式下 agy 不能运行命令，也就读不了文件（会直接返回空结果）：把要审阅的代码差异、页面摘录等材料直接放进提示词。不加 `--dangerously-skip-permissions`。
    - 无界面模式下 agy 联网读网页需要"read_url"权限、没法当场确认，会被自动拒绝（2026-09-29 实测）；需要网页资料时由 Claude 或 Codex 联网取回，把摘录放进提示词。
  - 给每个任务设边界：一个明确问题、输出格式和字数上限。它们以前在大范围任务上多次超时。Grok 需要浏览网页时，写明最多打开几个网页，`--max-turns` 可放宽到 70。
- **复核：** Codex 和 agy 的结论要经过核实才能写进正式文档。中国政府网站可以用本机 `curl` 直接验证（本机在国内网络）。
- **每日汇报：** 按 AGENTS.md 的汇报格式，列出进展、验证结果和待创始人决定的事项。
