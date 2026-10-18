"""来源统计（推广与获客方案第二节第 3 条、第九节；创始人 10/18 同意）：用户从哪个入口打开小程序。

小程序每次冷启动只在手机上读出打开方式，首屏出来以后发一次过来（apps/miniapp/utils/entry-stats.js），
这里只按天累加次数，给管理台的"来源统计"看每周汇总。不给用户看，不是新功能。

- 存什么：日期（北京时间）、场景值（微信给的打开方式）、来源编号（链接或小程序码里的 from）、
  第一个打开的页面路径（不含参数）、次数。不存 openid 或任何用户编号，不存检索词等查询参数，
  也不存每次打开的时间和 IP。只能说"群聊卡片进来 N 次"，说不出"来了几个人"。
- 谁来收：只有常驻的 admin 服务收（api 服务上这个接口是 404）。admin 不信任身份头（配置不允许它打开
  trust_wx_headers），所以这次请求的访问日志里没有用户编号；也不会为了统计去叫醒按需启动的接口服务。
- 来源编号只收渠道或活动级的短编号：小写字母、数字、"-"、"_"，最长 24 位（如 gzh1103、park-qd）。
  不给每位顾问单独编号，免得从编号反推出是谁。不合格的当作没带编号。
- 保存期限：400 天（13 个月多一点，够拿今年申报季和明年同期比），每天 3:30 的清理任务删除更早的
  （notify.run_cleanup）。访问日志 190 天就删了，过了那之后这些计数和任何请求记录都对不上。
- 每天最多 MAX_ROWS_PER_DAY 种不同组合：超出后新出现的组合都记进当天的"超出上限"一行
  （场景值 0、来源编号 OVERFLOW_CODE），乱填的编号撑不大这张表。
"""

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from youju.models import EntryStat

BEIJING = ZoneInfo("Asia/Shanghai")
KEEP_DAYS = 400
MAX_ROWS_PER_DAY = 500
# 超出当天组合上限时记成这个编号：正常编号里不会有"*"
OVERFLOW_CODE = "*"
CODE = re.compile(r"[a-z0-9_-]{1,24}")
PAGE = re.compile(r"pages/[a-z0-9-]{1,40}/index")
CRAWLER = 1129

# 微信官方场景值列表（https://developers.weixin.qq.com/miniprogram/dev/reference/scene-list.html，
# 2026-10-18 本机取回核对）里和推广有关的，括号里的基础库版本说明省略；没列的在汇总里写"场景值 N"
SCENE_NAMES = {
    1001: "发现页小程序「最近使用」列表",
    1005: "微信首页顶部搜索框的搜索结果页",
    1006: "发现栏小程序主入口搜索框的搜索结果页",
    1007: "单人聊天会话中的小程序消息卡片",
    1008: "群聊会话中的小程序消息卡片",
    1011: "扫描二维码",
    1012: "长按图片识别二维码",
    1013: "扫描手机相册中选取的二维码",
    1014: "小程序订阅消息",
    1017: "前往小程序体验版的入口页",
    1023: "安卓系统桌面图标",
    1027: "微信首页顶部搜索框搜索结果页「使用过的小程序」列表",
    1035: "公众号自定义菜单",
    1036: "App 分享消息卡片",
    1037: "小程序打开小程序",
    1038: "从另一个小程序返回",
    1042: "添加好友搜索框的搜索结果页",
    1043: "公众号模板消息",
    1044: "带 shareTicket 的小程序消息卡片",
    1047: "扫描小程序码",
    1048: "长按图片识别小程序码",
    1049: "扫描手机相册中选取的小程序码",
    1053: "搜一搜的结果页",
    1058: "公众号文章",
    1065: "URL scheme",
    1074: "公众号会话下发的小程序消息卡片",
    1082: "公众号会话下发的文字链",
    1089: "微信聊天主界面下拉，「最近使用」栏",
    1091: "公众号文章商品卡片",
    1096: "聊天记录，打开小程序",
    1102: "公众号 profile 页服务预览",
    1103: "发现页小程序「我的小程序」列表",
    1104: "微信聊天主界面下拉，「我的小程序」栏",
    1106: "聊天主界面下拉，从顶部搜索结果页，打开小程序",
    1107: "订阅消息，打开小程序",
    1129: "微信爬虫访问",
    1131: "浮窗",
    1152: "订阅号视频打开小程序",
    1157: "服务号会话页打开小程序",
    1184: "视频号链接打开小程序",
    1187: "浮窗或星标",
    1193: "视频号主页服务菜单打开小程序",
    1194: "URL Link",
    1256: "pc端小程序面板「最近使用」列表",
    1257: "pc端小程序面板「我的小程序」列表",
}

# 汇总时按渠道归并（显示用，库里存原始场景值）；不在这里的都算"其他"
CHANNELS = (
    ("好友转发的卡片", (1007,)),
    ("群聊卡片", (1008, 1044)),
    ("扫小程序码", (1047, 1048, 1049)),
    ("扫二维码", (1011, 1012, 1013)),
    ("微信搜索", (1005, 1006, 1027, 1042, 1053, 1106)),
    ("公众号", (1035, 1043, 1058, 1074, 1082, 1091, 1102, 1152, 1157)),
    ("视频号", (1184, 1193)),
    ("订阅消息（提醒）", (1014, 1107)),
    ("最近使用、我的小程序、桌面", (1001, 1023, 1089, 1103, 1104, 1131, 1187, 1256, 1257)),
)
OTHER = "其他"
_CHANNEL_OF = {scene: name for name, scenes in CHANNELS for scene in scenes}

PAGE_NAMES = {
    "pages/home/index": "首页",
    "pages/detail/index": "通知详情",
    "pages/search/index": "搜索",
    "pages/report/index": "截止",
    "pages/lists/index": "查名单",
    "pages/digest/index": "本期清单",
    "pages/follow/index": "关注",
    "pages/files/index": "文件",
    "pages/me/index": "我的",
    "pages/clients/index": "我的客户",
    "pages/advisor/index": "批量工具",
}


def clean_scene(value: int) -> int:
    """场景值都是四位数；别的数（含没带）记成 0。"""
    return value if 1000 <= value <= 9999 else 0


def clean_code(value: str) -> str:
    """来源编号：大写字母当小写（小程序也已经转过）；不合格的当作没带（空）。"""
    value = value.strip().lower()
    return value if CODE.fullmatch(value) else ""


def clean_page(value: str) -> str:
    """页面路径：只认本小程序 pages/xxx/index 的写法，不带参数；别的记成空。"""
    value = value.strip().removeprefix("/")
    return value if PAGE.fullmatch(value) else ""


def record(session: Session, *, day: date, scene: int, code: str, page: str) -> None:
    """这一组合当天的次数加 1（已经清洗过的值）。先改已有的行（多数情况）；没有再新增，
    当天组合已满时记进"超出上限"那一行。两个请求同时新增同一组合时，靠唯一键合并成一行。"""
    key = {"day": day, "scene": scene, "code": code, "page": page}
    same = (
        (EntryStat.day == day)
        & (EntryStat.scene == scene)
        & (EntryStat.code == code)
        & (EntryStat.page == page)
    )
    if session.execute(update(EntryStat).where(same).values(opens=EntryStat.opens + 1)).rowcount:
        session.commit()
        return
    rows = session.scalar(select(func.count()).select_from(EntryStat).where(EntryStat.day == day))
    if rows >= MAX_ROWS_PER_DAY:
        key.update(scene=0, code=OVERFLOW_CODE, page="")
    session.execute(
        mysql_insert(EntryStat)
        .values(**key, opens=1)
        .on_duplicate_key_update(opens=EntryStat.opens + 1)
    )
    session.commit()


def prune(session: Session, now: datetime) -> dict[str, int]:
    """删掉 KEEP_DAYS 天以前（按北京时间的日期）的计数。由每天的清理任务调用，和它一起提交。"""
    cutoff = now.astimezone(BEIJING).date() - timedelta(days=KEEP_DAYS)
    removed = session.execute(delete(EntryStat).where(EntryStat.day < cutoff)).rowcount
    return {"entry_stats": removed}


def _channel(scene: int) -> str:
    return _CHANNEL_OF.get(scene, OTHER)


def weekly(session: Session, *, today: date, weeks: int) -> dict:
    """最近 weeks 周（周一到周日，北京时间；最后一周是本周到今天）的次数，按渠道、来源编号、
    第一个页面、场景值分别汇总。各列从早到晚。微信爬虫（场景值 1129）单列，不算进合计和其他各表。"""
    this_week = today - timedelta(days=today.weekday())
    starts = [this_week - timedelta(weeks=n) for n in range(weeks - 1, -1, -1)]
    rows = session.execute(
        select(
            EntryStat.day, EntryStat.scene, EntryStat.code, EntryStat.page, EntryStat.opens
        ).where(EntryStat.day >= starts[0], EntryStat.day <= today)
    ).all()

    def empty() -> list[int]:
        return [0] * weeks

    total, crawler = empty(), empty()
    channels = {name: empty() for name, _ in CHANNELS} | {OTHER: empty()}
    codes: dict[str, list[int]] = {}
    pages: dict[str, list[int]] = {}
    scenes: dict[int, list[int]] = {}
    for day, scene, code, page, opens in rows:
        week = (day - starts[0]).days // 7
        if scene == CRAWLER:
            crawler[week] += opens
            continue
        total[week] += opens
        channels[_channel(scene)][week] += opens
        codes.setdefault(code, empty())[week] += opens
        pages.setdefault(page, empty())[week] += opens
        scenes.setdefault(scene, empty())[week] += opens

    def ranked(counts: dict) -> list:
        return sorted(counts.items(), key=lambda item: (-sum(item[1]), str(item[0])))

    return {
        "keep_days": KEEP_DAYS,
        "weeks": [
            {
                "start": start,
                "end": min(start + timedelta(days=6), today),
                "current": start == this_week,
            }
            for start in starts
        ],
        "total": total,
        "crawler": crawler,
        "channels": [
            {"name": name, "scenes": list(scene_list), "counts": channels[name]}
            for name, scene_list in CHANNELS
        ]
        + [{"name": OTHER, "scenes": [], "counts": channels[OTHER]}],
        "codes": [
            {"code": code, "overflow": code == OVERFLOW_CODE, "counts": counts}
            for code, counts in ranked(codes)
        ],
        "pages": [
            {"page": page, "name": PAGE_NAMES.get(page, ""), "counts": counts}
            for page, counts in ranked(pages)
        ],
        "scenes": [
            {
                "scene": scene,
                "name": SCENE_NAMES.get(scene, ""),
                "channel": _channel(scene),
                "counts": counts,
            }
            for scene, counts in ranked(scenes)
        ],
    }
