"""已收录城市范围的共同解析；简称匹配不能扩大为全省或跨省查询。"""

from collections.abc import Collection

from sqlalchemy import select
from sqlalchemy.orm import Session

from youju.models import Source


def city_key(value: str) -> str:
    """仅末尾“市”参与别名匹配，自治州、地区及地名内部的“市”保持原样。"""
    return value.removesuffix("市")


def covered_city_scopes(session: Session) -> set[tuple[str, str]]:
    """只使用已成功采集且仍启用的市级来源，不把选择器选项当成数据覆盖。"""
    return set(
        session.execute(
            select(Source.province, Source.city).where(
                Source.enabled.is_(True),
                Source.last_success_at.is_not(None),
                Source.level == "市级",
                Source.city.is_not(None),
                Source.city != "",
            )
        ).all()
    )


def resolve_city(province: str, city: str, covered: Collection[tuple[str, str]]) -> str | None:
    """精确名称优先；否则仅接受同省唯一的“市”后缀别名，无法确定时返回 None。"""
    if (province, city) in covered:
        return city
    aliases = {
        name for area, name in covered if area == province and city_key(name) == city_key(city)
    }
    return next(iter(aliases)) if len(aliases) == 1 else None
