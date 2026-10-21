"""定时任务都要在任务名单里。

job.sh 先用 `youju job-start <任务名>` 记一条运行记录，名单（jobs.JOB_NAMES）里没有的任务名
命令行直接报 invalid choice，记录写不进去，任务本身照跑。10/20 夜里 02:25 的
ai-eligibility-rebuild 就是这样：重算做完了，运行记录没有。
"""

import re
from pathlib import Path

from youju.jobs import JOB_NAMES

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
# job.sh 开头那段 if 里的任务：每分钟或每 10 分钟一次，不记运行记录
UNRECORDED = {"advisor-run", "pay-run", "ai-heartbeat", "ai-eligibility"}


def _unrecorded_in_job_sh() -> set[str]:
    text = (DEPLOY / "job.sh").read_text(encoding="utf-8")
    block = re.search(r'^if (.*?); then\n\s*run_id=""', text, re.S | re.M)
    assert block, "job.sh 里找不到跳过记账的那段 if"
    return set(re.findall(r'"\$name" = "([a-z][a-z-]*)"', block.group(1)))


def _scheduled() -> set[str]:
    """crontab 里 job.sh 后面的任务名，加上 round.sh 里 run 和 job.sh 后面的任务名。"""
    names = set()
    for name in ("crontab", "round.sh"):
        text = (DEPLOY / name).read_text(encoding="utf-8")
        names |= set(re.findall(r'(?:job\.sh"?|\brun) ([a-z][a-z-]*)', text))
    return names


def test_job_sh_skips_recording_only_for_the_frequent_jobs():
    assert _unrecorded_in_job_sh() == UNRECORDED


def test_every_scheduled_job_is_in_the_job_list():
    scheduled = _scheduled()
    # 读法本身要读得到：这几个分别来自 crontab 的普通行、round.sh 的 run 行和 round.sh 的 job.sh 行
    assert {"backup-verify", "ai-eligibility-rebuild", "fetch-details", "alert"} <= scheduled
    assert sorted(scheduled - UNRECORDED - set(JOB_NAMES)) == []
