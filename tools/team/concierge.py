#!/usr/bin/env python3
"""人工代做一次（D-058 验证愿不愿意付费用）：按顾问给的客户清单，用线上数据出一张 Excel。

做什么：
  1. 逐家查客户有没有上已收录的公示、认定名单（企业全称精确相同才算，和小程序"查名单"同一个接口）；
  2. 按客户所在省份（加国家部委）和产业方向，列出近 90 天发布的申报通知，只去掉已人工核对且已截止的
     （和小程序"按客户筛通知"同一个口径）。
不判断"能不能报"，不打分，不推荐。产业方向只认小程序里那 8 项的原文，写别的一律按"不限产业方向"列，
不替客户猜行业。

怎么取数：api 服务不对公网开放，只能从小程序里调用。这里借微信开发者工具的自动化接口，在模拟器里
用 wx.cloud.callContainer 调线上接口（只读的查询接口）。所以运行前：开发者工具要开着"有据"项目、
模拟器已经加载出首页。查名单接口每个来源每分钟最多 30 次，这里每 2.2 秒查一家，200 家约 8 分钟。

用法（用后端虚拟环境里的 Python，里面有 openpyxl）：
  /Users/wanglei/youju-worktrees/ai/backend/.venv/bin/python tools/team/concierge.py <客户清单.xlsx|.csv> [输出.xlsx]

客户清单：第一行是表头，列名里带"企业""公司"或"名称"的是企业全称（必填）；带"地区"或"省"的是所在省份
（可空）；带"产业"或"方向"的是产业方向（可空，多个用顿号或逗号隔开）。只有一列、没有表头也可以。
**清单里不要有联系人、电话**；清单不上传到服务器，出完表后由创始人删除本机文件（见
docs/商业/首批试用计划_v1.md"四之二"）。
"""

from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

WECHATIDE = "/Applications/wechatwebdevtools.app/Contents/MacOS/wechatide"
PROJECT = "/Users/wanglei/youju-worktrees/miniapp-release/apps/miniapp"
CLOUD_ENV = "prod-d5getqftefb61fc92"
MAX_CLIENTS = 500
LOOKUP_GAP_MS = 2200  # 查名单限流每分钟 30 次
SEARCH_GAP_MS = 400
NOTICE_LIMIT = 50  # 每家客户最多列多少条通知
INDUSTRIES = (
    "数字经济",
    "先进制造",
    "新材料与化工",
    "生物医药与健康",
    "新能源与绿色低碳",
    "电子信息与集成电路",
    "现代农业",
    "文旅与现代服务业",
)
PROVINCES = ["北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江", "上海", "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南", "广东", "广西", "海南", "重庆", "四川", "贵州", "云南", "西藏", "陕西", "甘肃", "青海", "宁夏", "新疆"]
_SPLIT = re.compile(r"[、,，;；/|\s]+")

# 装进模拟器的小工具：排队逐个调接口（间隔由任务自己带），结果留在 getApp().__cg.done 里等 Python 来取。
_INSTALL_JS = (
    "function(){ const app=getApp(); app.__cg={queue:[],done:{},running:false};"
    " app.__cgRun=function(){ const s=app.__cg; if(s.running) return; s.running=true;"
    " const next=function(){ const job=s.queue.shift(); if(!job){ s.running=false; return; }"
    " const finish=function(v){ s.done[job.id]=v; setTimeout(next, job.gap); };"
    ' wx.cloud.callContainer({config:{env:"%s"},path:job.path,method:"GET",header:{"X-WX-SERVICE":"api"},'
    " success:function(r){ finish({status:r.statusCode,data:r.data}); },"
    " fail:function(e){ finish({status:0,error:String(e&&e.errMsg)}); }}); };"
    " next(); };"
    " app.__cgSlim=function(v){ if(!v) return null; const d=v.data||{};"
    " const item=function(x){ return {title:x.title,issuer:x.issuer,date:x.date,url:x.url,status:x.status,"
    "region:x.region,category:x.category,deadline:x.deadline,deadline_state:x.deadline_state,matched:x.matched}; };"
    " return {status:v.status,error:v.error||(typeof d.detail==='string'?d.detail:''),total:d.total,"
    "items:(d.items||[]).map(item),candidates:(d.candidates||[]).slice(0,5),ambiguous:d.ambiguous,"
    "truncated:d.truncated,coverage:d.coverage}; };"
    ' return "ok"; }'
) % CLOUD_ENV


def evaluate(js: str, timeout: int = 60):
    """在模拟器里执行一段 JS，返回它的返回值。"""
    proc = subprocess.run(
        [
            WECHATIDE,
            "-c",
            "claude-code",
            "automation_evaluate",
            "--project",
            PROJECT,
            "--fn-source",
            js,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    found = re.search(r"\{.*", proc.stdout, re.DOTALL)
    if not found:
        raise RuntimeError(
            f"开发者工具没有返回结果：{(proc.stdout or proc.stderr)[-200:]}"
        )
    result = json.loads(found.group(0))
    result = result.get("result", result)
    while isinstance(result, dict) and "result" in result and len(result) <= 3:
        result = result["result"]
    if isinstance(result, dict) and result.get("ok") is False:
        raise RuntimeError(f"开发者工具执行出错：{result.get('message')}")
    return result


def run_jobs(jobs: list[dict], label: str) -> dict[str, dict]:
    """把任务排进模拟器里的队列，等它逐个调完，取回精简后的结果。失败的最多重试 3 次。"""
    results: dict[str, dict] = {}
    pending = {job["id"]: job for job in jobs}
    for attempt in range(4):
        if not pending:
            break
        if attempt:
            print(f"  {label}：{len(pending)} 个没成功，第 {attempt} 次重试")
            time.sleep(20)
        batch = list(pending.values())
        for start in range(0, len(batch), 30):
            chunk = json.dumps(batch[start : start + 30], ensure_ascii=False)
            evaluate(
                "function(){ const app=getApp(); const jobs=%s; jobs.forEach(function(j){ app.__cg.queue.push(j); });"
                " app.__cgRun(); return app.__cg.queue.length; }" % chunk
            )
        fetched: set[str] = set()
        while len(fetched) < len(batch):
            time.sleep(4)
            done = json.loads(
                evaluate(
                    "function(){ return JSON.stringify(Object.keys(getApp().__cg.done)); }"
                )
            )
            ready = [i for i in done if i in pending and i not in fetched]
            for start in range(0, len(ready), 5):
                ids = ready[start : start + 5]
                raw = evaluate(
                    "function(){ const app=getApp(); const ids=%s; const out={};"
                    " ids.forEach(function(i){ out[i]=app.__cgSlim(app.__cg.done[i]); delete app.__cg.done[i]; });"
                    " return JSON.stringify(out); }" % json.dumps(ids)
                )
                for job_id, value in json.loads(raw).items():
                    results[job_id] = value
                    fetched.add(job_id)
            print(f"  {label}：{len(results)}/{len(jobs)}", end="\r", flush=True)
        # 限流、网关错误、调用失败的再试；422（像个人姓名或太短）不重试
        pending = {
            i: job
            for i, job in pending.items()
            if results.get(i, {}).get("status") in (0, 429, 502, 503, 504)
        }
    print()
    return results


def read_clients(path: Path) -> list[dict]:
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = [[cell.strip() for cell in row] for row in csv.reader(handle)]
    else:
        sheet = load_workbook(path, read_only=True, data_only=True).active
        rows = [
            ["" if cell is None else str(cell).strip() for cell in row]
            for row in sheet.iter_rows(values_only=True)
        ]
    rows = [row for row in rows if any(row)]
    if not rows:
        raise SystemExit("清单是空的")
    header = rows[0]

    def column(*words: str) -> int | None:
        return next(
            (i for i, name in enumerate(header) if any(w in name for w in words)), None
        )

    name_col, region_col, industry_col = (
        column("企业", "公司", "名称"),
        column("地区", "省"),
        column("产业", "方向"),
    )
    if name_col is None:  # 没有表头：第一列就是企业名称
        name_col, region_col, industry_col, body = 0, None, None, rows
    else:
        body = rows[1:]
    clients, seen = [], set()
    for row in body:
        name = row[name_col] if name_col < len(row) else ""
        if not name or name in seen:
            continue
        seen.add(name)
        region_text = (
            row[region_col] if region_col is not None and region_col < len(row) else ""
        )
        industry_text = (
            row[industry_col]
            if industry_col is not None and industry_col < len(row)
            else ""
        )
        provinces = [p for p in PROVINCES if p in region_text]
        wanted = [v for v in _SPLIT.split(industry_text) if v]
        clients.append(
            {
                "name": name,
                "provinces": provinces,
                "industries": [v for v in wanted if v in INDUSTRIES],
                "ignored": [v for v in wanted if v not in INDUSTRIES],
                "region_text": region_text,
            }
        )
    if len(clients) > MAX_CLIENTS:
        raise SystemExit(f"清单有 {len(clients)} 家，一次最多 {MAX_CLIENTS} 家")
    return clients


def notice_key(client: dict) -> str:
    return "|".join([",".join(client["provinces"]), ",".join(client["industries"])])


def notice_path(client: dict) -> str:
    params = {
        "kind": "申报通知",
        "time": "近 90 天",
        "exclude_closed": "true",
        "group": "1",
    }
    params["limit"] = str(NOTICE_LIMIT)
    # 和小程序"按客户筛通知"一样：有省份时查"国家部委 + 这些省"，没有时全国
    params["regions"] = (
        ",".join(["国家部委", *client["provinces"]])
        if client["provinces"]
        else "不限地区"
    )
    if client["industries"]:
        params["industries"] = ",".join(client["industries"])
    return "/api/v1/search?" + "&".join(f"{k}={quote(v)}" for k, v in params.items())


def deadline_text(item: dict) -> str:
    deadline = item.get("deadline")
    if deadline and deadline.get("date"):
        return f"{deadline['date']}（已人工核对）"
    return "截止未核对，请以原文为准"


def write_sheet(
    book: Workbook, title: str, header: list[str], rows: list[list], widths: list[int]
):
    sheet = book.create_sheet(title)
    sheet.append(header)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for row in rows:
        sheet.append(row)
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            if isinstance(cell.value, str) and cell.value.startswith("http"):
                cell.hyperlink = cell.value
                cell.font = Font(color="2A5CE0", underline="single")
    sheet.freeze_panes = "A2"
    return sheet


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    source = Path(sys.argv[1]).expanduser()
    target = (
        Path(sys.argv[2]).expanduser()
        if len(sys.argv) > 2
        else source.with_name(source.stem + "_结果.xlsx")
    )
    clients = read_clients(source)
    print(
        f"读到 {len(clients)} 家客户；查名单约需 {len(clients) * LOOKUP_GAP_MS // 60000 + 1} 分钟"
    )
    evaluate(_INSTALL_JS)

    lookups = run_jobs(
        [
            {
                "id": f"l{i}",
                "path": "/api/v1/lists/lookup?q=" + quote(c["name"]),
                "gap": LOOKUP_GAP_MS,
            }
            for i, c in enumerate(clients)
        ],
        "查名单",
    )
    combos = {notice_key(c): c for c in clients}
    searches = run_jobs(
        [
            {"id": f"s{n}", "path": notice_path(c), "gap": SEARCH_GAP_MS}
            for n, c in enumerate(combos.values())
        ],
        "查通知",
    )
    notices = {key: searches.get(f"s{n}") or {} for n, key in enumerate(combos)}

    coverage = (
        next((r["coverage"] for r in lookups.values() if r and r.get("coverage")), None)
        or {}
    )
    list_rows, notice_rows, by_notice, failed = [], [], {}, 0
    for i, client in enumerate(clients):
        found = lookups.get(f"l{i}") or {}
        status = found.get("status")
        if status == 200 and found.get("items"):
            for item in found["items"]:
                matched = item.get("matched") or {}
                note = []
                if matched.get("via") == "creditCode":
                    note.append("按统一社会信用代码相同找到（名单里的名称不同）")
                if found.get("ambiguous"):
                    note.append("名单里有同名但信用代码不同的企业，请核对")
                list_rows.append(
                    [
                        client["name"],
                        f"上榜 {len(found['items'])} 份",
                        item.get("title"),
                        item.get("issuer"),
                        item.get("date"),
                        item.get("status") or "",
                        matched.get("name") or "",
                        " ".join(
                            str(v)
                            for v in (matched.get("batch"), matched.get("sequence"))
                            if v
                        ),
                        item.get("url"),
                        "；".join(note),
                    ]
                )
        elif status == 200:
            hint = (
                "名称里带这几个字的有：" + "、".join(found["candidates"])
                if found.get("candidates")
                else ""
            )
            list_rows.append(
                [
                    client["name"],
                    "已收录的名单里没查到",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    hint,
                ]
            )
        elif status == 422:
            list_rows.append(
                [
                    client["name"],
                    "没查：名称太短或像个人姓名，请给企业全称",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                ]
            )
        else:
            failed += 1
            list_rows.append(
                [
                    client["name"],
                    f"没查成（{found.get('error') or status}），请重跑",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                ]
            )

        result = notices.get(notice_key(client)) or {}
        region = (
            "、".join(client["provinces"]) + "（含国家部委）"
            if client["provinces"]
            else "全国"
        )
        industry = "、".join(client["industries"]) or "不限产业方向"
        if client["ignored"]:
            ignored = "、".join(client["ignored"])
            industry += f"（清单里写的“{ignored}”不在可筛选的 8 项里，没有用）"
        items = result.get("items") or []
        if result.get("status") != 200:
            failed += 1
            notice_rows.append(
                [client["name"], region, industry, "没查成，请重跑", "", "", "", "", ""]
            )
        elif not items:
            notice_rows.append(
                [
                    client["name"],
                    region,
                    industry,
                    "近 90 天没有列出的通知",
                    "",
                    "",
                    "",
                    "",
                    "",
                ]
            )
        for item in items:
            notice_rows.append(
                [
                    client["name"],
                    region,
                    industry,
                    item.get("title"),
                    item.get("issuer"),
                    item.get("date"),
                    item.get("category") or "",
                    deadline_text(item),
                    item.get("url"),
                ]
            )
            entry = by_notice.setdefault(item.get("url"), {"item": item, "clients": []})
            entry["clients"].append(client["name"])
        if items and (result.get("total") or 0) > len(items):
            notice_rows.append(
                [
                    client["name"],
                    region,
                    industry,
                    f"（共 {result['total']} 条，这里只列最新的 {len(items)} 条）",
                    "",
                    "",
                    "",
                    "",
                    "",
                ]
            )

    book = Workbook()
    intro = book.active
    intro.title = "说明"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    hit = len({row[0] for row in list_rows if str(row[1]).startswith("上榜")})
    for line in [
        "客户名单与申报通知对照表",
        f"生成时间：{stamp}；客户 {len(clients)} 家，其中 {hit} 家在已收录的名单里查到。",
        "",
        "这张表是第三方按政府网站公开发布的信息整理的，不是政府平台出具的，也不代办申报。",
        "一切以发文机关原文为准，每一条都附了原文链接。",
        "",
        "【上榜情况】企业全称和名单原文完全相同才算查到；没查到不等于没上榜："
        + (
            f"目前只收录了 {coverage.get('lists')} 份名单（{coverage.get('since')} 以来各省科技、工信部门发布、能读出企业名称的）。"
            if coverage
            else "收录范围有限。"
        ),
        "【相关通知】按客户所在省份（加国家部委）和产业方向，列出近 90 天发布的申报通知，"
        "只去掉截止日期已人工核对、确认已截止的。产业方向按通知标题归类，仅供检索参考，不是官方分类。",
        "列出来不代表客户符合申报条件，这里不作任何资格判断。",
        "【截止日期】只写人工对照原文核对过的；写“截止未核对”的，请打开原文看。",
        "【按通知看】同一条通知和哪些客户有关，方便一次通知多家。",
    ]:
        intro.append([line])
    intro.column_dimensions["A"].width = 110
    intro["A1"].font = Font(bold=True, size=14)
    for row in intro.iter_rows():
        row[0].alignment = Alignment(wrap_text=True, vertical="top")

    write_sheet(
        book,
        "上榜情况",
        [
            "客户",
            "结果",
            "名单标题",
            "发文机关",
            "发布日期",
            "公示/公布",
            "名单里的写法",
            "批次/序号",
            "原文链接",
            "备注",
        ],
        list_rows,
        [30, 22, 50, 22, 12, 10, 30, 12, 40, 40],
    )
    write_sheet(
        book,
        "相关通知",
        [
            "客户",
            "地区",
            "产业方向",
            "通知标题",
            "发文机关",
            "发布日期",
            "项目类别",
            "截止日期",
            "原文链接",
        ],
        notice_rows,
        [30, 20, 22, 56, 22, 12, 16, 26, 40],
    )
    write_sheet(
        book,
        "按通知看",
        [
            "通知标题",
            "发文机关",
            "发布日期",
            "截止日期",
            "有关的客户数",
            "有关的客户",
            "原文链接",
        ],
        [
            [
                e["item"].get("title"),
                e["item"].get("issuer"),
                e["item"].get("date"),
                deadline_text(e["item"]),
                len(e["clients"]),
                "、".join(e["clients"]),
                url,
            ]
            for url, e in sorted(
                by_notice.items(),
                key=lambda kv: kv[1]["item"].get("date") or "",
                reverse=True,
            )
        ],
        [56, 22, 12, 26, 12, 60, 40],
    )
    book.save(target)
    print(f"已生成：{target}")
    print(
        f"上榜 {hit} 家；通知 {len(by_notice)} 条；没查成 {failed} 项"
        + ("（请重跑）" if failed else "")
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
