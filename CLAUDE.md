# CLAUDE.md

先读 [AGENTS.md](AGENTS.md)：团队章程、分工和合规红线都在那里。

## Claude 的额外职责

- **统筹：** 创始人只需要在 Claude Code 里提需求。由 Claude 拆分任务，需要时调用 agy 和 Grok，并把结果复核后汇总给创始人。
- **调用外部成员：** 两者都用无界面模式运行，工作目录放在临时目录；提示词第一行写明"只读：不要创建、修改或删除任何文件"。
  - Grok：`grok --cwd <临时目录> --model grok-4.7 --effort high --max-turns 40 --output-format plain --prompt-file <提示词文件>`
  - agy：`agy --model gemini-3.8-flash-high --effort high --mode plan --output-format text --print-timeout 20m -p "<提示词>"`。需要打开网页时加 `--sandbox --dangerously-skip-permissions`，且只在临时目录中运行。
  - 给每个任务设边界：一个明确问题、输出格式和字数上限。它们以前在大范围任务上多次超时。
- **复核：** agy 和 Grok 的结论要经过核实才能写进正式文档。中国政府网站可以用本机 `curl` 直接验证（本机在国内网络）。
- **每日汇报：** 按 AGENTS.md 的汇报格式，列出进展、验证结果和待创始人决定的事项。
