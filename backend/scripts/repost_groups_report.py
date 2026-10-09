"""转发归组规则（youju.repost）在一份只读数据快照上的统计和清单（D-055，转载去重方案 v1 附录二）。

只读：会话设为只读事务，只执行 SELECT；不连正式库 youju。只用最早几版就有的列，旧快照
（如本机 youju_perf，迁移停在 0013）也能跑。同一文件层只按现有 document_merges；
同网址的小重复留作后续，不在统计里模拟归并。

用法（在 backend/ 下）：
    uv run python scripts/repost_groups_report.py \
        --url 'mysql+pymysql://root@127.0.0.1:33306/youju_perf?charset=utf8mb4' \
        --since 2026-08-07 --until 2026-10-06
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date

from sqlalchemy import create_engine, make_url, text

from youju.classify import effective_kind
from youju.crawler.urls import url_key
from youju.repost import BODY_HEAD_CHARS, Candidate, kind_bucket, propose_groups


def _load(conn):
    docs = conn.execute(
        text(
            "SELECT d.id, d.url, d.title, d.kind_override, d.publish_date, s.province, s.dept, "
            f"LEFT(dd.body_text, {BODY_HEAD_CHARS}) AS body, d.taken_down_at "
            "FROM documents d JOIN sources s ON s.id = d.source_id "
            "LEFT JOIN document_details dd ON dd.document_id = d.id ORDER BY d.id"
        )
    ).all()
    merges = dict(
        conn.execute(
            text("SELECT duplicate_id, canonical_id FROM document_merges WHERE active = 1")
        ).all()
    )
    deadlines = conn.execute(
        text(
            "SELECT document_id, kind, candidate_date, candidate_time, status "
            "FROM document_deadlines"
        )
    ).all()
    return docs, merges, deadlines


def _canonical(docs, merges) -> dict[int, int]:
    """同一文件层只认已有的 document_merges，不跨机关模拟同网址合并。"""
    return {row.id: merges.get(row.id, row.id) for row in docs}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True, help="只读连接的数据库（不能是正式库 youju）")
    parser.add_argument("--since", type=date.fromisoformat, required=True)
    parser.add_argument("--until", type=date.fromisoformat, required=True)
    parser.add_argument("--list", action="store_true", help="逐组列出各版本")
    args = parser.parse_args()
    url = make_url(args.url)
    if url.database in (None, "", "youju"):
        parser.error("不连正式库 youju：请指定快照库（如 youju_perf）或测试库")
    engine = create_engine(url)
    with engine.connect() as conn:
        conn.execute(text("SET SESSION TRANSACTION READ ONLY"))
        conn.execute(text("START TRANSACTION READ ONLY"))
        docs, merges, deadlines = _load(conn)
        conn.rollback()
    engine.dispose()

    master = _canonical(docs, merges)
    rows = {row.id: row for row in docs}
    candidates = [
        Candidate(
            id=row.id,
            title=row.title,
            kind=effective_kind(row.title, row.kind_override),
            province=row.province,
            dept=row.dept,
            publish_date=row.publish_date,
            body_head=row.body or "",
        )
        for row in docs
        if master[row.id] == row.id and row.publish_date is not None and row.taken_down_at is None
    ]
    by_id = {c.id: c for c in candidates}
    proposals = propose_groups(candidates)

    def in_window(doc_id: int) -> bool:
        published = by_id[doc_id].publish_date
        return args.since <= published <= args.until

    window = [c for c in candidates if args.since <= c.publish_date <= args.until]
    same_urls: dict[str, set[int]] = defaultdict(set)
    for row in docs:
        same_urls[url_key(row.url)].add(master[row.id])
    pending = sum(len(ids) - 1 for ids in same_urls.values())
    print(f"同一文件层：按已有 document_merges 统计；同网址未合并的 {pending} 对留作后续")
    print(
        f"窗口 {args.since}～{args.until}：条目 {len(window)} 份，"
        + "、".join(f"{k} {n}" for k, n in Counter(kind_bucket(c.kind) for c in window).items())
    )
    for strength, label in (("strong", "强证据"), ("weak", "疑似")):
        chosen = [p for p in proposals if p.strength == strength and any(map(in_window, p.ids))]
        members = sum(len(p.ids) for p in chosen)
        inside = sum(1 for p in chosen for d in p.ids if in_window(d))
        folded = sum(max(0, sum(1 for d in p.ids if in_window(d)) - 1) for p in chosen)
        kinds = Counter(kind_bucket(by_id[p.ids[0]].kind) for p in chosen)
        sizes = Counter(len(p.ids) for p in chosen)
        print(
            f"{label}：{len(chosen)} 组，组内 {members} 份（窗口内 {inside}），"
            f"按组显示后窗口内少 {folded} 条；类型 {dict(kinds)}；"
            f"规模 {dict(sorted(sizes.items()))}"
        )
    # 截止候选：同组各来源的日期是否相同（各来源的候选日期集合）
    by_doc: dict[int, list] = defaultdict(list)
    for row in deadlines:
        by_doc[master.get(row.document_id, row.document_id)].append(row)
    multi = differ = disjoint = 0
    for p in proposals:
        if p.strength != "strong" or not any(map(in_window, p.ids)):
            continue
        dates = {}
        for doc_id in p.ids:
            got = {row.candidate_date for row in by_doc.get(doc_id, [])}
            if got:
                dates[doc_id] = got
        if len(dates) >= 2:
            multi += 1
            sets = list(dates.values())
            if any(s != sets[0] for s in sets[1:]):
                differ += 1
            if not set.intersection(*sets):
                disjoint += 1
    print(
        f"强证据组里 2 个以上来源各有截止候选的 {multi} 组；各来源日期不完全相同 {differ} 组；"
        f"没有一个共同日期 {disjoint} 组"
    )
    if args.list:
        for p in proposals:
            if not any(map(in_window, p.ids)):
                continue
            label = "强证据" if p.strength == "strong" else "疑似"
            print(f"\n[{label}] {p.topic}（{len(p.ids)} 个版本）上游：{p.upstream_number or '—'}")
            for doc_id in p.ids:
                c, row = by_id[doc_id], rows[doc_id]
                print(
                    f"  #{doc_id} {c.province}{c.dept} {c.publish_date} {c.kind}"
                    f"｜{p.evidence.get(doc_id) or '无上游证据'}｜{row.title}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
