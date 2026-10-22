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
# 分开前后两条命令的符号；括号是子 shell 或 $( ) 的边界
SEPARATORS = {";", "&&", "||", "|", "&", "(", ")"}
# 命令前面可能带的 shell 关键字
KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{"}
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def _name_of(command: list[str]) -> set[str]:
    """命令位置上是 `…/job.sh` 或 round.sh 的 `run` 时，取后面那个参数，原样不改。"""
    # 去掉前面的关键字和 X=1 这样的环境变量，剩下第一个就是实际执行的命令
    while command and (command[0] in KEYWORDS or ASSIGNMENT.match(command[0])):
        command = command[1:]
    if len(command) < 2:
        return set()
    program, name = command[0], command[1]
    if not (program == "job.sh" or program.endswith("/job.sh") or program == "run"):
        return set()
    # round.sh 定义函数那行 `run() {` 不是调用；"$@" 这类变量不是任务名
    if name == "()" or name.startswith("$"):
        return set()
    return {name}


def _job_names_in(text: str) -> set[str]:
    """经 job.sh 跑的任务名。

    先把行尾 \\ 的续行接起来，再逐行按 shell 的规则拆参数：注释不算，引号去掉，
    ; && || 和括号前后是不同的命令；只认命令位置上的 job.sh 和 run（见 _name_of）。
    """
    names = set()
    for line in text.replace("\\\n", " ").splitlines():
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


def _cron_commands(text: str) -> str:
    """crontab 每行去掉前 5 个时间字段，剩下的才是 shell 命令；注释、空行、环境变量行不算。"""
    commands = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split(None, 5)
        if len(fields) == 6:
            commands.append(fields[5])
    return "\n".join(commands)


def _scheduled() -> set[str]:
    cron = _cron_commands((DEPLOY / "crontab").read_text(encoding="utf-8"))
    round_sh = (DEPLOY / "round.sh").read_text(encoding="utf-8")
    return _job_names_in(cron) | _job_names_in(round_sh)


def _unrecorded_in_job_sh() -> set[str]:
    text = (DEPLOY / "job.sh").read_text(encoding="utf-8")
    block = re.search(r'^if (.*?); then\n\s*run_id=""', text, re.S | re.M)
    assert block, "job.sh 里找不到跳过记账的那段 if"
    return set(re.findall(r'"\$name" = "([^"]+)"', block.group(1)))


def test_cron_reader_skips_schedule_fields_and_comments():
    crontab = (
        "SHELL=/bin/sh\n"
        "# 30 2 * * * /app/backend/deploy/job.sh phantom-job（整行注释不算）\n"
        '* * * * *  /app/backend/deploy/job.sh   "quoted-job2" --x; /app/x/job.sh second-job\n'
        "*/5 * * * * date +\\%s > /tmp/youju-cron-heartbeat\n"
    )
    assert _job_names_in(_cron_commands(crontab)) == {"quoted-job2", "second-job"}


def test_job_name_reader_reads_commands_not_comments():
    sample = (
        "# /app/backend/deploy/job.sh phantom-job（整行注释不算）\n"
        '/app/backend/deploy/job.sh   "quoted-job2" --x; /app/x/job.sh second-job\n'
        "run first && run check-sources --streaks  # run commented-job（行尾注释也不算）\n"
        '[ "$1" = "--notify" ] && run notify\n'
        "if ! flock -n 9; then run inside-if; fi\n"
        "LIMIT=1 run with-env\n"
        "(run in-subshell)\n"
        "run continued \\\n  --limit 600\n"
        "job.sh typo.name\n"
        "echo /app/backend/deploy/job.sh not-a-job\n"
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
        "with-env",
        "in-subshell",
        "continued",
        "typo.name",
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
