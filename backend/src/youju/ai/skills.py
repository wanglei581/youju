"""通知摘录技能注册表；两种技能共用任务流程。"""

from youju.ai.clause import CLAUSE
from youju.ai.interpret import INTERPRET, Skill

SKILLS = {skill.kind: skill for skill in (INTERPRET, CLAUSE)}
AI_KINDS = tuple(SKILLS)


def get_skill(kind: str) -> Skill:
    return SKILLS[kind]
