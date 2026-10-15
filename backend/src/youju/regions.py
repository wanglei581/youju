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


MUNICIPALITIES = {"北京", "天津", "上海", "重庆"}


def checked_area_name(value: str | None, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{label}须填写文字")
    value = value.strip()
    if len(value) > 32 or any(c in value for c in "/\\,，;；、|\t\n\r"):
        raise ValueError(f"{label}须为单个名称，最多32字")
    return value


def district_parent_city(province: str, city: str) -> str:
    if province in MUNICIPALITIES and (city_key(city) == province or city in {"市辖区", "县"}):
        return province
    return city


def covered_district_scopes(session: Session) -> set[tuple[str, str, str]]:
    return set(
        session.execute(
            select(Source.province, Source.city, Source.district).where(
                Source.enabled.is_(True),
                Source.last_success_at.is_not(None),
                Source.level == "区县级",
                Source.city.is_not(None),
                Source.city != "",
                Source.district.is_not(None),
                Source.district != "",
            )
        ).all()
    )


def resolve_district(
    province: str, city: str, district: str, covered: Collection[tuple[str, str, str]]
) -> tuple[str, str] | None:
    city = district_parent_city(province, city)
    parents = {(p, c) for p, c, d in covered if d == district}
    resolved = resolve_city(province, city, parents)
    return (resolved, district) if resolved is not None else None
