"""覆盖抽查：从覆盖看板"已收录"的格里按固定种子抽 N 格，每格取一份文件，打开原网页核对
链接、标题、发布日期和归属（本市、本部门），留下证据给人逐条看过后签字。

D-126：每省抽查由 Claude 带证据做，创始人复看约 5%；覆盖率按抽查合格的算。

只读公开页面：打开原文走 PoliteFetcher（遵守 robots、同一网站两次请求间隔 3 秒、透明标识，
不绕过任何防护），结果存页面缓存，重跑不重复请求。

用法（backend 目录，数据库读 YOUJU_DATABASE_URL，也就是试抓用的本机库）：
  uv run python -m youju.govsites.spotcheck --board <试点覆盖/省.csv> --out <目录>
"""

import argparse
import csv
import hashlib
import html as htmllib
import re
import unicodedata
from datetime import date
from pathlib import Path

from youju.govsites.divisions import name_variants
from youju.govsites.links import is_gov_url, parse_html
from youju.govsites.names import dept_categories
from youju.govsites.platform import fingerprint

DEFAULT_SEED = "2026-11"
OK = "合格"
REVIEW = "待人工看"
FIELDS = (
    "序号",
    "城市",
    "部门",
    "部门全称",
    "栏目网址",
    "文件标题",
    "文件网址",
    "发布日期",
    "来源",
    "打开",
    "标题核对",
    "标题说明",
    "日期核对",
    "日期说明",
    "归属核对",
    "归属说明",
    "程序结论",
    "人工结论",
    "备注",
)


def pick_cells(
    rows: list[dict], level: str = "市级", n: int = 20, seed: str = DEFAULT_SEED
) -> list[dict]:
    """只取这一级"已收录"的格，按种子散列排序取前 n 个：重跑抽到的一样，换种子就换一批。"""
    pool = [r for r in rows if r.get("层级") == level and r.get("状态") == "已收录"]

    def key(r: dict) -> str:
        raw = f"{seed}|{r.get('区划代码', '')}|{r.get('部门', '')}|{r.get('栏目网址', '')}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    return sorted(pool, key=key)[:n]


_TRUNCATED = re.compile(r"(?:\.{3,}|…+|．{3,}|。{3,})\s*$")


def norm_title(text: str) -> str:
    """比标题用：去掉列表页截断的省略号、空白和全半角标点符号，全角字母数字转半角。"""
    t = _TRUNCATED.sub("", (text or "").strip())
    t = unicodedata.normalize("NFKC", t)
    return "".join(ch for ch in t if unicodedata.category(ch)[0] not in "PSZC")


_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")


def page_text(page_html: str) -> str:
    """网页的文字（去掉脚本、样式和标签，实体转成字符）。"""
    t = _SCRIPT_STYLE.sub(" ", page_html or "")
    t = _TAG.sub(" ", t)
    return htmllib.unescape(t)


def _page_title(page_html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", page_html or "", re.S | re.I)
    return htmllib.unescape(re.sub(r"\s+", " ", m.group(1))).strip() if m else ""


def _longest_run(needle: str, hay: str) -> int:
    """needle 里在 hay 中原样出现的最长一段有多长（标题一般几十字，逐段试够快）。"""
    for k in range(len(needle), 0, -1):
        for i in range(len(needle) - k + 1):
            if needle[i : i + k] in hay:
                return k
    return 0


def title_check(stored: str, page_html: str) -> tuple[str, str]:
    t = norm_title(stored)
    if not t:
        return "不一致", "库里没有标题"
    hay = norm_title(page_text(page_html)) + norm_title(_page_title(page_html))
    if t in hay:
        return "一致", ""
    ratio = _longest_run(t, hay) / len(t)
    if ratio >= 0.8:
        return "基本一致", f"原文里最长的相同部分占库里标题的 {ratio:.0%}"
    return "不一致", f"原文里最长的相同部分只占库里标题的 {ratio:.0%}"


_DATE = r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})"
_LABELED_DATE = re.compile(
    r"(?:发布日期|发布时间|发文日期|成文日期|印发日期|日期|时间)\s*[:：]?\s*" + _DATE
)


def _labeled_dates(text: str) -> list[date]:
    out: list[date] = []
    for m in _LABELED_DATE.finditer(text):
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        if d not in out:
            out.append(d)
    return out


def date_forms(d: date) -> list[str]:
    y, m, dd = d.year, d.month, d.day
    return [
        f"{y}-{m:02d}-{dd:02d}",
        f"{y}-{m}-{dd}",
        f"{y}/{m:02d}/{dd:02d}",
        f"{y}/{m}/{dd}",
        f"{y}.{m:02d}.{dd:02d}",
        f"{y}.{m}.{dd}",
        f"{y}年{m}月{dd}日",
        f"{y}年{m:02d}月{dd:02d}日",
    ]


def date_check(stored: date | None, page_html: str) -> tuple[str, str]:
    if stored is None:
        return "页面上找不到日期", "库里没有发布日期"
    text = page_text(page_html)
    labeled = _labeled_dates(text)
    if stored in labeled:
        return "一致", ""
    if labeled:
        shown = "、".join(d.isoformat() for d in labeled[:4])
        return "页面写的是别的日期", f"页面上标的日期：{shown}"
    for form in date_forms(stored):
        if re.search(rf"(?<!\d){re.escape(form)}(?!\d)", text):
            return "一致", "页面上没有标明是发布日期，但有这个日期"
    return "页面上找不到日期", "页面上没有标明的日期，也找不到库里的日期"


def city_names(city: str) -> set[str]:
    """看板里城市写简称（南京）或自治州全称；网页上多写全称"南京市"或"恩施州"。"""
    names = {v for v in name_variants(city) if len(v) >= 2}
    if not city.endswith(("州", "盟", "地区", "市")):
        names.add(city + "市")
    return names


def _site_names(page_html: str) -> list[str]:
    doc = parse_html(page_html)
    out = []
    if doc is not None:
        site = fingerprint(page_html, "", doc, []).site_name
        if site:
            out.append(site)
    title = _page_title(page_html)
    if title:
        out.append(title)
    return out


def attribution_check(cell: dict, column_html: str, doc_html: str) -> tuple[str, str]:
    """栏目页和文件页的网站名、标题里有没有本市的写法，栏目页网站名能不能归到这个部门。"""
    city, dept = cell.get("城市", ""), cell.get("部门", "")
    col_names = _site_names(column_html)
    doc_names = _site_names(doc_html)
    names = city_names(city) if city else set()
    where = " / ".join(col_names + doc_names)[:120]
    if not names or not any(n in s for n in names for s in col_names + doc_names):
        return "待看", f"网站名和标题里没有找到“{city}”：{where or '（都没有）'}"
    if dept == "政府办":
        return "本市本部门", where
    for s in col_names + doc_names:
        for part in re.split(r"[-_|·—–>＞:：\s]+", s):
            if dept in dept_categories(part):
                return "本市本部门", where
    return "本市", f"认不出是“{dept}”的网站（门户频道或网站名不写部门）：{where}"


# —— 取证（要数据库和页面缓存）——


def _first_list_item(cache, url: str, today: date):
    """库里这一格没有文件时（合并部门共用栏目，文件挂在先抓到的来源下）：重读列表页取第一条带日期的。"""
    from youju.crawler.adapters import api_html, find_list_api
    from youju.crawler.listing import extract_items

    r = cache.get(url)
    if r.status != "ok":
        return None
    final = r.final_url or url
    items = extract_items(r.text, final, today=today)
    if len(items) < 3:
        api = find_list_api(r.text, final)
        if api is not None:
            ar = cache.get(api.url)
            body = api_html(api, ar.text) if ar.status == "ok" else ""
            via = extract_items(body, api.base_url, today=today) if body else []
            if len(via) > len(items):
                items = via
    return next((i for i in items if i.publish_date), None)


def evidence_for(cell: dict, session, cache, today: date) -> dict:
    from sqlalchemy import select

    from youju.models import Document, Source

    urls = (cell.get("栏目网址") or "").split()
    ev = {f: "" for f in FIELDS}
    ev.update(
        城市=cell.get("城市", ""),
        部门=cell.get("部门", ""),
        部门全称=cell.get("部门全称", ""),
        栏目网址=" ".join(urls),
    )
    if not urls:
        ev.update(程序结论=REVIEW, 打开="看板里这一格没有栏目网址")
        return ev
    title = doc_url = ""
    published = None
    ids = list(session.scalars(select(Source.id).where(Source.url.in_(urls))))
    row = None
    if ids:
        row = session.execute(
            select(Document.title, Document.url, Document.publish_date)
            .where(Document.source_id.in_(ids), Document.publish_date.is_not(None))
            .order_by(Document.publish_date.desc(), Document.id.desc())
            .limit(1)
        ).first()
    if row is not None:
        title, doc_url, published = row
        ev["来源"] = "库里"
    else:
        item = None
        for url in urls:
            item = _first_list_item(cache, url, today)
            if item is not None:
                break
        if item is None:
            ev.update(
                来源="列表页重读", 程序结论=REVIEW, 打开="库里没有文件，列表页也没读出带日期的条目"
            )
            return ev
        title, doc_url, published = item.title, item.url, item.publish_date
        ev["来源"] = "列表页重读"
    ev.update(文件标题=title, 文件网址=doc_url, 发布日期=published.isoformat() if published else "")
    if not is_gov_url(doc_url):
        ev.update(打开="非政府网站，未打开", 程序结论=REVIEW)
        return ev
    r = cache.get(doc_url)
    if r.status != "ok":
        ev.update(打开=f"打不开：{(r.error or r.status)[:80]}", 程序结论=REVIEW)
        return ev
    ev["打开"] = "ok" if (r.final_url or doc_url) == doc_url else f"ok（跳到 {r.final_url}）"
    col = cache.get(urls[0])
    ev["标题核对"], ev["标题说明"] = title_check(title, r.text)
    ev["日期核对"], ev["日期说明"] = date_check(published, r.text)
    ev["归属核对"], ev["归属说明"] = attribution_check(
        cell, col.text if col.status == "ok" else "", r.text
    )
    passed = (
        ev["标题核对"] in ("一致", "基本一致")
        and ev["日期核对"] == "一致"
        and ev["归属核对"] == "本市本部门"
    )
    ev["程序结论"] = OK if passed else REVIEW
    return ev


def _md_cell(value: str) -> str:
    return (value or "").replace("|", "\\|").replace("\n", " ")


def _md_link(text: str, url: str) -> str:
    if not url:
        return _md_cell(text)
    return f"[{_md_cell(text) or '链接'}]({url.replace(' ', '%20').replace(')', '%29')})"


def render_markdown(
    province: str, level: str, population: int, seed: str, rows: list[dict], today: date
) -> str:
    ok = sum(r["程序结论"] == OK for r in rows)
    lines = [
        f"# {province}{level}抽查（程序取证，{today.isoformat()}）",
        "",
        f"抽样方法：总体是覆盖看板里{province}{level}“已收录”的 {population} 格；按"
        f"“种子 {seed} + 区划代码 + 部门 + 栏目网址”的 SHA-1 从小到大取前 {len(rows)} 格"
        "（重跑抽到的一样）。每格取库里这格栏目下发布日期最新的一份文件（库里没有时重读列表页取"
        "第一条带日期的），打开原网页核对标题、发布日期，并看栏目页和文件页的网站名、标题是不是"
        "本市、本部门。",
    ]
    if population < 20:
        lines.append("")
        lines.append(f"已收录的格不足 20 个，全部 {population} 格都抽了。")
    lines += [
        "",
        "| 序号 | 城市 | 部门 | 栏目 | 文件标题 | 发布日期 | 标题 | 日期 | 归属 | 程序结论 |",
        "|---:|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        first_col = (r["栏目网址"] or "").split(" ")[0]
        lines.append(
            "| "
            + " | ".join(
                [
                    str(r["序号"]),
                    _md_cell(r["城市"]),
                    _md_cell(r["部门"]),
                    _md_link("栏目页", first_col),
                    _md_link(r["文件标题"][:40], r["文件网址"]),
                    _md_cell(r["发布日期"]),
                    _md_cell(r["标题核对"] or r["打开"]),
                    _md_cell(r["日期核对"]),
                    _md_cell(r["归属核对"]),
                    _md_cell(r["程序结论"]),
                ]
            )
            + " |"
        )
    lines += [
        "",
        f"合计：程序判合格 {ok}/{len(rows)}，待人工看 {len(rows) - ok}。"
        "“待人工看”的逐条原因见同名 CSV 的“标题说明”“日期说明”“归属说明”。",
        "",
        "抽查人签字：（待 Claude 逐条看过后填写）",
        "",
        "创始人复看：第 1 条（约 5%）",
        "",
    ]
    return "\n".join(lines)


def write_outputs(
    out: Path,
    province: str,
    level: str,
    population: int,
    seed: str,
    rows: list[dict],
    today: date,
) -> tuple[Path, Path]:
    folder = out / "抽查"
    folder.mkdir(parents=True, exist_ok=True)
    csv_path = folder / f"{province}.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, FIELDS)
        w.writeheader()
        w.writerows(rows)
    md_path = folder / f"{province}.md"
    md_path.write_text(
        render_markdown(province, level, population, seed, rows, today), encoding="utf-8"
    )
    return csv_path, md_path


def main(argv: list[str] | None = None) -> int:
    from youju.db import session_factory
    from youju.govsites.cli import _fetcher
    from youju.govsites.pagecache import PageCache

    p = argparse.ArgumentParser(description="覆盖抽查：按省抽已收录的格，核对原网页")
    p.add_argument("--board", type=Path, action="append", required=True, help="试点覆盖/<省>.csv")
    p.add_argument("--level", default="市级", choices=("国家级", "省级", "市级", "区县级"))
    p.add_argument("--n", type=int, default=20)
    p.add_argument("--seed", default=DEFAULT_SEED)
    p.add_argument("--cache", type=Path, default=Path.home() / ".cache" / "youju-coverage-city1")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--offline", action="store_true", help="只用页面缓存，不联网")
    args = p.parse_args(argv)
    today = date.today()
    with _fetcher() as fetcher, session_factory()() as session:
        cache = PageCache(args.cache, None if args.offline else fetcher, offline=args.offline)
        for board in args.board:
            with board.open(encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.DictReader(fh))
            province = rows[0]["省份"] if rows else board.stem
            population = sum(
                r.get("层级") == args.level and r.get("状态") == "已收录" for r in rows
            )
            picked = pick_cells(rows, args.level, args.n, args.seed)
            evidence = []
            for i, cell in enumerate(picked, start=1):
                ev = evidence_for(cell, session, cache, today)
                ev["序号"] = i
                evidence.append(ev)
                print(f"{province} {i}/{len(picked)} {ev['城市']} {ev['部门']}：{ev['程序结论']}")
            csv_path, md_path = write_outputs(
                args.out, province, args.level, population, args.seed, evidence, today
            )
            ok = sum(e["程序结论"] == OK for e in evidence)
            print(f"{province}：程序判合格 {ok}/{len(evidence)}；写入 {md_path}、{csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
