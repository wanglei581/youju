"""转发组自动合并的质检命令（D-129 ②），都在本机跑，只读写 JSON 文件和 repost_* 表。

- repost-auto-sample：抽组。gate 从现在满足自动合并条件的组里抽（开开关前 Claude 带证据判 100 组）；
  weekly 先抽已经自动合并的组、不够的用别的已确认组补（每周 Codex 独立抽查 10 组）。只给证据：
  各版本的标题、机关、日期、网址、引用的文号、正文里共同文号前后的原文，结论和备注留空给判的人填。
- repost-auto-record：记回判定。格式不对整份拒收；组在抽样后变了（修订号或在组版本不一样）的跳过；
  判错的组当场整组撤回（以后不再自动合并）。
- repost-auto-stats：按轮统计准确率，给出开关那道门的结果。
- repost-withdraw：命令行整组撤回（收到投诉等抽查以外的场合），要带看到的修订号。
撤回都记一行审计日志（youju.audit），和管理台的撤回一样留痕。
"""

import json
import logging
import random
import uuid
from collections import defaultdict
from dataclasses import asdict
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from youju.classify import effective_kind
from youju.db import session_factory
from youju.models import (
    Document,
    DocumentDetail,
    RepostAutoCheck,
    RepostGroup,
    RepostGroupMember,
    RepostSignal,
    Source,
)
from youju.repost import (
    _BEIJING,
    _CITED,
    AUTO_REVIEWER,
    AUTO_RULE_VERSION,
    Candidate,
    Cited,
    _window_groups,
    auto_gate,
    auto_reason,
    auto_upstream,
    duplicate_members,
    load_candidates,
    signals,
    withdraw_group,
)

audit_log = logging.getLogger("youju.audit")


def _audit(action: str, **fields: object) -> None:
    """命令行操作留痕：和管理台的审计日志同一个记录器（youju.audit），标明来自命令行。"""
    record = {"event": "cli", "action": action, **fields}
    audit_log.info(json.dumps(record, ensure_ascii=False, default=str))


def _versions(rows, duplicates):
    return [r for r in rows if r.active and r.document_id not in duplicates]


def auto_candidates(
    session: Session, today: date
) -> tuple[list[tuple[RepostGroup, list[RepostGroupMember]]], dict[int, Candidate]]:
    """现在满足自动合并条件的组（只看窗口里的组），和判定用的候选 {文件编号: Candidate}。"""
    groups = list(
        session.scalars(
            select(RepostGroup)
            .where(RepostGroup.id.in_(_window_groups(today)))
            .order_by(RepostGroup.id)
        )
    )
    members = list(
        session.scalars(
            select(RepostGroupMember)
            .where(RepostGroupMember.group_id.in_([g.id for g in groups]))
            .order_by(RepostGroupMember.document_id)
        )
    )
    by_group = defaultdict(list)
    for row in members:
        by_group[row.group_id].append(row)
    cands = {c.id: c for c in load_candidates(session, today)}
    duplicates = set(duplicate_members(session, [r.document_id for r in members]))
    eligible = [
        (g, by_group[g.id]) for g in groups if not auto_reason(g, by_group[g.id], cands, duplicates)
    ]
    return eligible, cands


def _excerpt(body, number):
    if number:
        for match in _CITED.finditer(body):
            _, prefix, year, serial = match.groups()
            if f"{prefix}〔{year}〕{int(serial)}号" == number:
                return body[max(0, match.start() - 120) : match.end() + 120]
    return body[:300]


def sample(args):
    if args.size is not None and args.size < 1:
        raise ValueError("抽样数量必须大于零")
    stamp = datetime.now(UTC)
    rng = random.Random(args.seed)
    with session_factory()() as session:
        if args.purpose == "gate":
            groups, cands = auto_candidates(session, stamp.astimezone(_BEIJING).date())
            selected = rng.sample(groups, min(args.size or 100, len(groups)))
        else:
            groups = list(session.scalars(select(RepostGroup).order_by(RepostGroup.id)))
            rows = list(
                session.scalars(select(RepostGroupMember).order_by(RepostGroupMember.document_id))
            )
            duplicates = set(duplicate_members(session, [r.document_id for r in rows]))
            grouped = defaultdict(list)
            for row in rows:
                grouped[row.group_id].append(row)
            eligible = []
            for group in groups:
                versions = _versions(grouped[group.id], duplicates)
                if (
                    len(versions) >= 2
                    and not group.needs_review
                    and all(r.confirmed_at for r in versions)
                ):
                    eligible.append((group, grouped[group.id]))
            auto = [
                g
                for g in eligible
                if all(r.confirmed_by == AUTO_REVIEWER for r in _versions(g[1], duplicates))
            ]
            other = [g for g in eligible if g not in auto]
            rng.shuffle(auto)
            rng.shuffle(other)
            selected = (auto + other)[: args.size or 10]
            selected_ids = [r.document_id for _, rows in selected for r in rows]
            cands = {}
            for doc, source, signal in session.execute(
                select(Document, Source, RepostSignal)
                .join(Source, Source.id == Document.source_id)
                .outerjoin(RepostSignal, RepostSignal.document_id == Document.id)
                .where(Document.id.in_(selected_ids))
            ):
                cands[doc.id] = Candidate(
                    id=doc.id,
                    title=doc.title,
                    kind=effective_kind(doc.title, doc.kind_override),
                    province=source.province,
                    city=source.city,
                    district=source.district,
                    dept=source.dept,
                    publish_date=doc.publish_date,
                    cited=tuple(Cited(**c) for c in signal.cited) if signal else (),
                    has_body=signal.has_body if signal else False,
                )
        all_rows = [r for _, rows in selected for r in rows]
        duplicates = set(duplicate_members(session, [r.document_id for r in all_rows]))
        ids = [r.document_id for r in all_rows]
        details = {
            doc.id: (doc, detail)
            for doc, detail in session.execute(
                select(Document, DocumentDetail)
                .outerjoin(DocumentDetail, DocumentDetail.document_id == Document.id)
                .where(Document.id.in_(ids))
            )
        }
        items = []
        for group, rows in selected:
            number = auto_upstream(rows, cands, duplicates)
            members = []
            for row in _versions(rows, duplicates):
                doc, detail = details[row.document_id]
                cand = cands.get(row.document_id)
                if cand is None:
                    raise ValueError(f"文件 {row.document_id} 无候选数据，无法生成完整抽样")
                members.append(
                    dict(
                        document_id=doc.id,
                        title=doc.title,
                        province=cand.province,
                        city=cand.city,
                        district=cand.district,
                        dept=cand.dept,
                        publish_date=cand.publish_date.isoformat() if cand.publish_date else None,
                        url=doc.url,
                        kind=cand.kind,
                        cited=[c.number for c in signals(cand).cited],
                        excerpt=_excerpt(detail.body_text or "" if detail else "", number),
                    )
                )
            items.append(
                dict(
                    group_id=group.id,
                    revision=group.revision,
                    topic=group.topic,
                    upstream=number,
                    members=members,
                    verdict="",
                    note="",
                )
            )
    result = dict(
        round=f"{args.purpose}-{stamp:%Y%m%d}-{uuid.uuid4().hex[:12]}",
        purpose=args.purpose,
        rule_version=AUTO_RULE_VERSION,
        sampled_at=stamp.isoformat(),
        items=items,
    )
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"抽取 {len(items)} 组：{args.out}")
    return 0


def _validate(data, reviewer):
    if not isinstance(reviewer, str) or not reviewer.strip() or len(reviewer.strip()) > 40:
        raise ValueError("核对人须为 1 至 40 字")
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("round"), str)
        or not 1 <= len(data["round"]) <= 40
    ):
        raise ValueError("轮次格式错误")
    if (
        data.get("purpose") not in ("gate", "weekly")
        or not isinstance(data.get("rule_version"), str)
        or not 1 <= len(data["rule_version"]) <= 16
    ):
        raise ValueError("用途或规则版本错误")
    if not isinstance(data.get("items"), list):
        raise ValueError("抽样项目格式错误")
    seen = set()
    for item in data["items"]:
        if (
            not isinstance(item, dict)
            or type(item.get("group_id")) is not int
            or item["group_id"] < 1
            or type(item.get("revision")) is not int
            or item["revision"] < 0
        ):
            raise ValueError("组编号或修订号错误")
        if item["group_id"] in seen:
            raise ValueError("同一组重复")
        seen.add(item["group_id"])
        if (
            item.get("verdict") not in ("correct", "wrong")
            or not isinstance(item.get("note"), str)
            or len(item["note"]) > 300
            or (item["verdict"] == "wrong" and not item["note"].strip())
        ):
            raise ValueError("结论或备注错误")
        members = item.get("members")
        if (
            not isinstance(members, list)
            or len(members) < 2
            or any(
                not isinstance(m, dict)
                or type(m.get("document_id")) is not int
                or m["document_id"] < 1
                for m in members
            )
        ):
            raise ValueError("成员清单错误")
        if len({m["document_id"] for m in members}) != len(members):
            raise ValueError("成员重复")


def record(args):
    data = json.loads(args.file.read_text(encoding="utf-8-sig"))
    _validate(data, args.by)
    stamp = datetime.now(UTC)
    counts = defaultdict(int)
    with session_factory()() as session:
        ids = sorted(i["group_id"] for i in data["items"])
        groups = {
            g.id: g
            for g in session.scalars(
                select(RepostGroup)
                .where(RepostGroup.id.in_(ids))
                .order_by(RepostGroup.id)
                .with_for_update()
            )
        }
        rows = list(
            session.scalars(
                select(RepostGroupMember)
                .where(RepostGroupMember.group_id.in_(ids))
                .order_by(RepostGroupMember.document_id)
                .with_for_update()
            )
        )
        grouped = defaultdict(list)
        for row in rows:
            grouped[row.group_id].append(row)
        duplicates = set(duplicate_members(session, [r.document_id for r in rows]))
        existing = set(
            session.scalars(
                select(RepostAutoCheck.group_id).where(
                    RepostAutoCheck.round == data["round"], RepostAutoCheck.group_id.in_(ids)
                )
            )
        )
        for item in data["items"]:
            group = groups.get(item["group_id"])
            if item["group_id"] in existing:
                print(f"组 {item['group_id']}：已记过，跳过")
                counts["skip"] += 1
                continue
            doc_ids = sorted(m["document_id"] for m in item["members"])
            if (
                group is None
                or group.revision != item["revision"]
                or sorted(r.document_id for r in _versions(grouped[group.id], duplicates))
                != doc_ids
            ):
                print(f"组 {item['group_id']}：组变了，没记")
                counts["skip"] += 1
                continue
            session.add(
                RepostAutoCheck(
                    round=data["round"],
                    purpose=data["purpose"],
                    group_id=group.id,
                    revision=item["revision"],
                    document_ids=doc_ids,
                    rule_version=data["rule_version"],
                    verdict=item["verdict"],
                    note=item["note"],
                    checked_by=args.by.strip(),
                    checked_at=stamp,
                )
            )
            counts[item["verdict"]] += 1
            if item["verdict"] == "wrong":
                ids = withdraw_group(group, grouped[group.id], args.by.strip(), stamp)
                counts["withdraw"] += 1
                _audit(
                    "repost_withdraw",
                    group_id=group.id,
                    document_ids=ids,
                    reviewer=args.by.strip(),
                    reason=f"抽查判错（{data['round']}）：{item['note']}",
                )
        session.commit()
    print(
        f"记了 {counts['correct'] + counts['wrong']} 组、对 {counts['correct']} 组、"
        f"错 {counts['wrong']} 组、跳过 {counts['skip']} 组、撤回 {counts['withdraw']} 组"
    )
    return 0


def stats(args):
    with session_factory()() as session:
        rows = list(
            session.scalars(
                select(RepostAutoCheck).order_by(RepostAutoCheck.checked_at, RepostAutoCheck.id)
            )
        )
        gate = auto_gate(session)
    rounds = {}
    for row in rows:
        key = (row.round, row.purpose, row.rule_version)
        entry = rounds.setdefault(
            key,
            dict(
                round=row.round,
                purpose=row.purpose,
                rule_version=row.rule_version,
                checked=0,
                correct=0,
                checked_at=None,
            ),
        )
        entry["checked"] += 1
        entry["correct"] += row.verdict == "correct"
        entry["checked_at"] = row.checked_at.isoformat()
    results = list(rounds.values())
    for entry in results:
        entry["accuracy"] = entry["correct"] / entry["checked"]
    output = {"rounds": results, "latest_gate": asdict(gate)}
    if args.json:
        print(json.dumps(output, ensure_ascii=False))
    else:
        for r in results:
            print(
                f"{r['round']} {r['purpose']} {r['rule_version']}：判 {r['checked']} 组，"
                f"对 {r['correct']} 组，准确率 {r['accuracy']:.1%}"
            )
        print("当前规则最近 gate：" + json.dumps(output["latest_gate"], ensure_ascii=False))
    return 0


def withdraw(args):
    if not args.by.strip() or len(args.by.strip()) > 80:
        raise ValueError("核对人须为 1 至 80 字")
    with session_factory()() as session:
        group = session.scalar(
            select(RepostGroup).where(RepostGroup.id == args.group_id).with_for_update()
        )
        if group is None or group.revision != args.revision:
            print("组不存在或修订号不符，没有撤回")
            return 1
        rows = list(
            session.scalars(
                select(RepostGroupMember)
                .where(RepostGroupMember.group_id == group.id)
                .order_by(RepostGroupMember.document_id)
                .with_for_update()
            )
        )
        if not any(r.active and r.confirmed_at is not None for r in rows):
            print("这一组没有确认过，不用撤回")
            return 1
        ids = withdraw_group(group, rows, args.by.strip(), datetime.now(UTC))
        session.commit()
        _audit("repost_withdraw", group_id=group.id, document_ids=ids, reviewer=args.by.strip())
        print(f"撤回 {len(ids)} 个版本，修订号 {group.revision}")
    return 0
