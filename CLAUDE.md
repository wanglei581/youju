# CLAUDE.md

先读 [AGENTS.md](AGENTS.md)：团队章程、分工和合规红线都在那里。

## Claude 的额外职责

- **统筹：** 创始人只需要在 Claude Code 里提需求。由 Claude 拆分任务，需要时调用 agy 和 Grok，并把结果复核后汇总给创始人。
- **调用外部成员：** 都用无界面模式运行，工作目录放在临时目录；提示词第一行写明"只读：不要创建、修改或删除任何文件"。
  - Codex（GPT-6-sol，极高推理）：`codex exec --ignore-user-config -m gpt-6-sol -c model_reasoning_effort='"xhigh"' -s read-only --skip-git-repo-check --ephemeral -C <临时目录> -o <输出文件> "<提示词>" < /dev/null`。`--ignore-user-config` 不加载本机 Codex 的浏览器和电脑操作插件，登录照常；要看图时加 `-i <图片>`。分配开发任务时改用 `-s workspace-write`，`-C` 指向它自己的 git worktree。
  - Grok：`grok --cwd <临时目录> --model grok-4.7 --effort high --max-turns 40 --output-format plain --prompt-file <提示词文件>`
  - agy：`agy --model gemini-3.8-flash-high --effort high --mode plan --output-format text --print-timeout 20m -p "<提示词>"`。无界面模式下 agy 不能运行命令，也就读不了文件（会直接返回空结果）：把要审阅的代码差异、页面摘录等材料直接放进提示词。不加 `--dangerously-skip-permissions`。
  - 给每个任务设边界：一个明确问题、输出格式和字数上限。它们以前在大范围任务上多次超时。Grok 需要浏览网页时，写明最多打开几个网页，`--max-turns` 可放宽到 70。
- **复核：** agy 和 Grok 的结论要经过核实才能写进正式文档。中国政府网站可以用本机 `curl` 直接验证（本机在国内网络）。
- **每日汇报：** 按 AGENTS.md 的汇报格式，列出进展、验证结果和待创始人决定的事项。
