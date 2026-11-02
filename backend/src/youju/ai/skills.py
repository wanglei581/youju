"""通知摘录技能注册表：解读、条款对照、问这份通知（ask，问一问开发设计 #328 第五节）。

AI_KINDS 是这几种 AI 任务：全站上限、调用次数、清理、能力接口的剩余次数、排队位置都按它算。
问这份通知只从对话接口建、看、删（api/chat.py），任务接口不列、不收（api/advisor.py）；
admin 处理时交给 ai/ask.py（advisor_tasks.process_task）。
"""

from youju.ai.ask import ASK
from youju.ai.clause import CLAUSE
from youju.ai.interpret import INTERPRET, Skill

SKILLS = {skill.kind: skill for skill in (INTERPRET, CLAUSE, ASK)}
AI_KINDS = tuple(SKILLS)


def get_skill(kind: str) -> Skill:
    return SKILLS[kind]
