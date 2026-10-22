"""定时任务都要在任务名单里。

job.sh 先用 `youju job-start <任务名>` 记一条运行记录，名单（jobs.JOB_NAMES）里没有的任务名
命令行直接报 invalid choice，记录写不进去，任务本身照跑。10/20 夜里 02:25 的
ai-eligibility-rebuild 就是这样：重算做完了，运行记录没有。
"""

import re
import shlex
from pathlib import Path

from youju.jobs import JOB_NAMES

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
# job.sh 开头那段 if 里的任务：每分钟或每 10 分钟一次，不记运行记录
UNRECORDED = {"advisor-run", "pay-run", "ai-heartbeat", "ai-eligibility"}
# 一行里分开前后两条命令的符号
SEPARATORS = {";", "&&", "||", "|", "&"}
# 命令前面可能带的 shell 关键字
KEYWORDS = {"if", "then", "else", "elif", "do", "!", "{"}
# 任务名的样子：字母、数字开头，后面是字母、数字、连字符、下划线
TASK_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


def _name_of(command: list[str]) -> set[str]:
    while command and command[0] in KEYWORDS:
        command = command[1:]
    for i, token in enumerate(command[:-1]):
        # `…/job.sh <任务名>`（crontab、round.sh），或 round.sh 里的 `run <任务名>`
        if token.endswith("/job.sh") or token == "job.sh" or (i == 0 and token == "run"):
            name = command[i + 1]
            # "$@" 这类变量、round.sh 定义函数那行 `run() {` 的 "()" 都不是任务名
            return {name} if TASK_NAME.fullmatch(name) else set()
    return set()


def _job_names_in(text: str) -> set[str]:
    """经 job.sh 跑的任务名。逐行按 shell 的规则拆参数：注释不算，引号去掉，; && || 前后是两条命令。"""
    names = set()
    for line in text.splitlines():
        lex = shlex.shlex(line, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        lex.commenters = "#"
        command: list[str] = []
        for token in [*lex, ";"]:
            if token in SEPARATORS:
                names |= _name_of(command)
                command = []
            else:
                command.append(token)
    return names


def _scheduled() -> set[str]:
    names = set()
    for name in ("crontab", "round.sh"):
        names |= _job_names_in((DEPLOY / name).read_text(encoding="utf-8"))
    return names


def _unrecorded_in_job_sh() -> set[str]:
    text = (DEPLOY / "job.sh").read_text(encoding="utf-8")
    block = re.search(r'^if (.*?); then\n\s*run_id=""', text, re.S | re.M)
    assert block, "job.sh 里找不到跳过记账的那段 if"
    return set(re.findall(r'"\$name" = "([^"]+)"', block.group(1)))


def test_job_name_reader_reads_commands_not_comments():
    sample = (
        "# 30 2 * * * /app/backend/deploy/job.sh phantom-job（整行注释不算）\n"
        '* * * * *  /app/backend/deploy/job.sh   "quoted-job2" --x; /app/x/job.sh second-job\n'
        "run first && run check-sources --streaks  # run commented-job（行尾注释也不算）\n"
        '[ "$1" = "--notify" ] && run notify\n'
        "if ! flock -n 9; then run inside-if; fi\n"
        '"$D/job.sh" "$@"\n'
        'run_id=$(timeout 20s youju job-start round 2>> /proc/1/fd/1) || run_id=""\n'
        "run() {\n"
    )
    assert _job_names_in(sample) == {
        "quoted-job2",
        "second-job",
        "first",
        "check-sources",
        "notify",
        "inside-if",
    }


def test_job_sh_skips_recording_only_for_the_frequent_jobs():
    assert _unrecorded_in_job_sh() == UNRECORDED


def test_every_scheduled_job_is_in_the_job_list():
    scheduled = _scheduled()
    # 读得到三种写法：crontab 的普通行和一行两条命令、round.sh 的 run 行、round.sh 的 job.sh 行
    expected = {
        "backup-verify",
        "weekly-digest",
        "ai-eligibility-rebuild",
        "fetch-details",
        "alert",
    }
    assert expected <= scheduled
    assert sorted(scheduled - UNRECORDED - set(JOB_NAMES)) == []
