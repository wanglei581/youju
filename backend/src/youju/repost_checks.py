"""转发组自动合并的质检命令（D-129 ②），都在本机跑，只读写 JSON 文件和 repost_* 表。

- repost-auto-sample：抽组。gate 从现在满足自动合并条件的组里抽（开开关前 Claude 带证据判 100 组）；
  weekly 先抽已经自动合并的组、不够的用别的已确认组补（每周 Codex 独立抽查 10 组；窗口外还在自动
  折叠的组也抽）。抽到的名单连同判的版本的证据指纹当场记进库（repost_auto_checks，结论空着）。只给证据：各版本的标题、机关、日期、网址、引用的文号、
  自己的发文字号（部里原件按它对上）、正文里共同文号前后的原文，结论和备注留空给判的人填。判的版本
  （judge 为真）每个都给摘录：gate 是全部在组版本；weekly 是已经确认（对外折叠）的版本，同组还没
  确认的版本（正文没抓到等，见 repost.rebuild）一并列出、不给摘录，只供对照。
- repost-auto-record：记回判定。交上来的组必须和这一轮抽样名单一模一样（不能删、不能添），每组都要有
  结论；格式不对整份拒收。组在抽样后变了（修订号、判的版本，或这些版本的标题、类型、正文、文号等
  证据不一样）的由程序记 changed、不算进准确率；
  判错的组当场整组撤回（以后不再自动合并）。
- repost-auto-stats：按轮统计（抽了几组、判了几组、对几组、组变了几组、还有几组没判），给出开关那道
  门的结果（只认整份判完、判了对错满 100 组的轮）。
- repost-withdraw：命令行整组撤回（收到投诉等抽查以外的场合），要带看到的修订号。
撤回都记一行审计日志（youju.audit），和管理台的撤回一样留痕。
"""

import hashlib
import json
import logging
import random
import uuid
from collections import defaultdict
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import or_, select
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
    AUTO_GATE_SIZE,
    AUTO_REVIEWER,
    AUTO_RULE_VERSION,
    BODY_HEAD_CHARS,
    WINDOW_DAYS,
    Candidate,
    Cited,
    _window_groups,
    auto_gate,
    auto_reason,
    auto_upstream,
    duplicate_members,
    load_candidates,
    member_facts,
    signals,
    withdraw_group,
)

audit_log = logging.getLogger("youju.audit")


def _now() -> datetime:
    """现在的时间（窗口按它的北京日子算）；测试里固定成测试世界的时间。"""
    return datetime.now(UTC)


def _audit(action: str, **fields: object) -> None:
    """命令行操作留痕：和管理台的审计日志同一个记录器（youju.audit），标明来自命令行。"""
    record = {"event": "cli", "action": action, **fields}
    audit_log.info(json.dumps(record, ensure_ascii=False, default=str))


# gate 默认多抽几组：抽样到记回之间组变了的要跳过，免得记回来不满 AUTO_GATE_SIZE 组、门过不了
GATE_SAMPLE_DEFAULT = AUTO_GATE_SIZE + 10
WEEKLY_SAMPLE_DEFAULT = 10


def _versions(rows, duplicates):
    return [r for r in rows if r.active and r.document_id not in duplicates]


def _confirmed(rows, duplicates):
    """已经确认（对外折叠）的在组版本。"""
    return [r for r in _versions(rows, duplicates) if r.confirmed_at is not None]


def _all_auto(rows, duplicates) -> bool:
    """确认过的版本都是自动合并确认的（没有人工确认的）。"""
    confirmed = _confirmed(rows, duplicates)
    return bool(confirmed) and all(r.confirmed_by == AUTO_REVIEWER for r in confirmed)


def auto_candidates(
    session: Session, today: date
) -> tuple[list[tuple[RepostGroup, list[RepostGroupMember]]], dict[int, Candidate]]:
    """现在满足自动合并条件的组（只看窗口里的组），和判定用的候选 {文件编号: Candidate}。"""
    cutoff = today - timedelta(days=WINDOW_DAYS)
    groups = list(
        session.scalars(
            select(RepostGroup)
            .where(RepostGroup.id.in_(_window_groups(cutoff)))
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
    cands = {c.id: c for c in load_candidates(session, today, cutoff)}
    duplicates = set(duplicate_members(session, [r.document_id for r in members]))
    eligible = [
        (g, by_group[g.id]) for g in groups if not auto_reason(g, by_group[g.id], cands, duplicates)
    ]
    return eligible, cands


def _excerpt(body, number):
    """共同文号前后各 120 字的原文：只在正文前 BODY_HEAD_CHARS 字里找（和判定用的同一段）；找不到给
    正文前 300 字。"""
    if number:
        for match in _CITED.finditer(body[:BODY_HEAD_CHARS]):
            _, prefix, year, serial = match.groups()
            if f"{prefix}〔{year}〕{int(serial)}号" == number:
                return body[max(0, match.start() - 120) : match.end() + 120]
    return body[:300]


def _member_candidates(session: Session, ids: list[int]) -> dict[int, Candidate]:
    """这几份文件的候选（标题、类型、机关、日期、存好的正文信号），不管是不是副本、下架：抽样给证据、
    算证据指纹都用它。"""
    cands = {}
    for doc, source, signal in session.execute(
        select(Document, Source, RepostSignal)
        .join(Source, Source.id == Document.source_id)
        .outerjoin(RepostSignal, RepostSignal.document_id == Document.id)
        .where(Document.id.in_(ids))
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
            own_number=signal.own_number if signal else None,
        )
    return cands


def _evidence(session: Session, ids: list[int]) -> str:
    """判的这几个版本的证据指纹：确认快照用的那些（日期、机关、标题、发文机关、全文，见
    repost.member_facts）加上类型、自己的发文字号、引用的文号。抽样以后有一样变了，记录时就算组变了
    （changed），旧证据下的结论不算进准确率、也不拿来撤回现在的组。"""
    _, facts = member_facts(session, ids)
    cands = _member_candidates(session, ids)
    values = []
    for doc_id in sorted(ids):
        cand = cands.get(doc_id)
        values.append(
            [
                doc_id,
                facts.get(doc_id),
                cand.kind if cand else None,
                cand.own_number if cand else None,
                sorted(c.number for c in signals(cand).cited) if cand else None,
            ]
        )
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def _judged(rows, duplicates, purpose):
    """这一组要判的版本：gate 是全部在组版本（开关打开以后要整组合并的）；weekly 是已经确认、对外
    折叠的版本。"""
    return _versions(rows, duplicates) if purpose == "gate" else _confirmed(rows, duplicates)


def sample(args):
    if args.size is not None and args.size < 1:
        raise ValueError("抽样数量必须大于零")
    stamp = _now()
    rng = random.Random(args.seed)
    round_id = f"{args.purpose}-{stamp:%Y%m%d}-{uuid.uuid4().hex[:12]}"
    with session_factory()() as session:
        today = stamp.astimezone(_BEIJING).date()
        if args.purpose == "gate":
            groups, cands = auto_candidates(session, today)
            population = len(groups)
            selected = rng.sample(groups, min(args.size or GATE_SAMPLE_DEFAULT, len(groups)))
        else:
            # 每周抽查：窗口里有确认版本的组，加上窗口外还在自动折叠的组（它们每轮照样重判、对外
            # 折叠，也要抽得到）；这一版本已经被每周抽查抽过的不再抽（gate 抽查是 Claude 判的，
            # 不挡 Codex 的独立抽查）
            confirmed = select(RepostGroupMember.group_id).where(
                RepostGroupMember.active.is_(True), RepostGroupMember.confirmed_at.is_not(None)
            )
            folded_auto = select(RepostGroupMember.group_id).where(
                RepostGroupMember.active.is_(True), RepostGroupMember.confirmed_by == AUTO_REVIEWER
            )
            cutoff = today - timedelta(days=WINDOW_DAYS)
            groups = list(
                session.scalars(
                    select(RepostGroup)
                    .where(
                        RepostGroup.id.in_(confirmed),
                        or_(
                            RepostGroup.id.in_(_window_groups(cutoff)),
                            RepostGroup.id.in_(folded_auto),
                        ),
                    )
                    .order_by(RepostGroup.id)
                )
            )
            checked = set(
                session.execute(
                    select(RepostAutoCheck.group_id, RepostAutoCheck.revision).where(
                        RepostAutoCheck.purpose == "weekly",
                        RepostAutoCheck.group_id.in_([g.id for g in groups]),
                    )
                ).all()
            )
            groups = [g for g in groups if (g.id, g.revision) not in checked]
            rows = list(
                session.scalars(
                    select(RepostGroupMember)
                    .where(RepostGroupMember.group_id.in_([g.id for g in groups]))
                    .order_by(RepostGroupMember.document_id)
                )
            )
            duplicates = set(duplicate_members(session, [r.document_id for r in rows]))
            grouped = defaultdict(list)
            for row in rows:
                grouped[row.group_id].append(row)
            eligible = []
            for group in groups:
                # 至少两个版本已经确认（对外折叠）：同组还没确认的新版本不妨碍抽它
                confirmed = _confirmed(grouped[group.id], duplicates)
                if len(confirmed) >= 2 and not group.needs_review:
                    eligible.append((group, grouped[group.id]))
            auto = [g for g in eligible if _all_auto(g[1], duplicates)]
            other = [g for g in eligible if g not in auto]
            rng.shuffle(auto)
            rng.shuffle(other)
            population = len(eligible)
            selected = (auto + other)[: args.size or WEEKLY_SAMPLE_DEFAULT]
            selected_ids = [r.document_id for _, rows in selected for r in rows]
            cands = _member_candidates(session, selected_ids)
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
            judged = _judged(rows, duplicates, args.purpose)
            judged_ids = {r.document_id for r in judged}
            # 共同文号、摘录都只按判的版本（weekly 时新来的未确认版本不影响已折叠版本的证据）
            number = auto_upstream(rows, cands, duplicates, versions=judged)
            members = []
            for row in _versions(rows, duplicates):
                doc, detail = details[row.document_id]
                cand = cands.get(row.document_id)
                if cand is None:
                    raise ValueError(f"文件 {row.document_id} 无候选数据，无法生成完整抽样")
                judge = row.document_id in judged_ids
                members.append(
                    dict(
                        document_id=doc.id,
                        judge=judge,
                        title=doc.title,
                        province=cand.province,
                        city=cand.city,
                        district=cand.district,
                        dept=cand.dept,
                        publish_date=cand.publish_date.isoformat() if cand.publish_date else None,
                        url=doc.url,
                        kind=cand.kind,
                        cited=[c.number for c in signals(cand).cited],
                        own_number=cand.own_number,
                        confirmed=(
                            None
                            if row.confirmed_at is None
                            else ("自动合并" if row.confirmed_by == AUTO_REVIEWER else "人工")
                        ),
                        # 判的版本每个都给摘录（大组也不省），只供对照的不给
                        excerpt=(
                            _excerpt(detail.body_text or "" if detail else "", number)
                            if judge
                            else None
                        ),
                    )
                )
            items.append(
                dict(
                    group_id=group.id,
                    revision=group.revision,
                    topic=group.topic,
                    upstream=number,
                    # weekly：确认过的版本是不是都是自动合并的；gate 时还没合并，不适用
                    auto=_all_auto(rows, duplicates) if args.purpose == "weekly" else None,
                    members=members,
                    verdict="",
                    note="",
                )
            )
            # 抽到的名单当场记进库（结论空着）：记录时交上来的必须和它一模一样
            session.add(
                RepostAutoCheck(
                    round=round_id,
                    purpose=args.purpose,
                    group_id=group.id,
                    revision=group.revision,
                    document_ids=sorted(judged_ids),
                    evidence=_evidence(session, sorted(judged_ids)),
                    rule_version=AUTO_RULE_VERSION,
                    verdict=None,
                    note="",
                    sampled_at=stamp,
                )
            )
        result = dict(
            round=round_id,
            purpose=args.purpose,
            rule_version=AUTO_RULE_VERSION,
            sampled_at=stamp.isoformat(),
            seed=args.seed,
            population=population,
            items=items,
        )
        # 文件写成功了再把名单记进库：写不出来就不记，不让这批组白白算成"抽过"
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        session.commit()
    print(f"从 {population} 组里抽了 {len(items)} 组：{args.out}")
    if args.purpose == "gate" and len(items) < AUTO_GATE_SIZE:
        print(f"只抽到 {len(items)} 组，不满 {AUTO_GATE_SIZE} 组：这一轮判完也过不了门")
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
    if data.get("purpose") not in ("gate", "weekly"):
        raise ValueError("用途错误")
    if data.get("rule_version") != AUTO_RULE_VERSION:
        # 旧规则下抽的样本不再算数（门也只认当前版本）：按当前规则重新抽
        raise ValueError(f"抽样的规则版本不是当前的 {AUTO_RULE_VERSION}，按当前规则重新抽样")
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
    stamp = _now()
    counts = defaultdict(int)
    withdrawn: list[dict] = []
    reviewer = args.by.strip()
    with session_factory()() as session:
        sampled = {
            row.group_id: row
            for row in session.scalars(
                select(RepostAutoCheck)
                .where(RepostAutoCheck.round == data["round"])
                .with_for_update()
            )
        }
        if not sampled:
            raise ValueError("这一轮没有抽样名单：先用 repost-auto-sample 抽")
        if any(
            r.purpose != data["purpose"] or r.rule_version != data["rule_version"]
            for r in sampled.values()
        ):
            raise ValueError("用途或规则版本和这一轮抽样名单对不上")
        given = {i["group_id"] for i in data["items"]}
        if given != set(sampled):
            missing, extra = sorted(set(sampled) - given), sorted(given - set(sampled))
            raise ValueError(
                f"交上来的组和抽样名单不一样（少了 {missing[:10]}、多了 {extra[:10]}）："
                "每组都要判，不能删、不能添"
            )
        ids = sorted(given)
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
        for item in sorted(data["items"], key=lambda i: i["group_id"]):
            check = sampled[item["group_id"]]
            group = groups.get(item["group_id"])
            if check.verdict is not None:
                print(f"组 {item['group_id']}：已记过，跳过")
                counts["skip"] += 1
                continue
            check.checked_by, check.checked_at = reviewer, stamp
            judged_now = (
                sorted(r.document_id for r in _judged(grouped[group.id], duplicates, check.purpose))
                if group is not None
                else []
            )
            if (
                group is None
                or group.revision != check.revision
                or judged_now != check.document_ids
                or _evidence(session, judged_now) != check.evidence
            ):
                # 抽样以后组变了（版本或证据）：程序记 changed、不算进准确率（判的人的结论不用，
                # 也不拿旧证据下的结论撤回现在的组）
                check.verdict, check.note = "changed", "抽样以后组变了"
                print(f"组 {item['group_id']}：抽样以后组变了，记 changed、不算")
                counts["changed"] += 1
                continue
            check.verdict, check.note = item["verdict"], item["note"]
            counts[item["verdict"]] += 1
            if item["verdict"] == "wrong":
                # 判错的组当场整组撤回（以后不再自动合并）；审计日志等提交成功以后再写
                before = {
                    r.document_id: r.confirmed_by
                    for r in grouped[group.id]
                    if r.active and r.confirmed_at is not None
                }
                withdrawn_ids = withdraw_group(group, grouped[group.id], stamp)
                counts["withdraw"] += 1
                withdrawn.append(
                    dict(
                        group_id=group.id,
                        document_ids=withdrawn_ids,
                        confirmed_by=before,
                        round=data["round"],
                        reason=f"抽查判错：{item['note']}",
                    )
                )
        judged = sum(1 for r in sampled.values() if r.verdict in ("correct", "wrong"))
        session.commit()
    for event in withdrawn:
        _audit("repost_withdraw", reviewer=reviewer, **event)
    print(
        f"记了 {counts['correct'] + counts['wrong']} 组、对 {counts['correct']} 组、"
        f"错 {counts['wrong']} 组、组变了 {counts['changed']} 组、跳过 {counts['skip']} 组、"
        f"撤回 {counts['withdraw']} 组"
    )
    if data["purpose"] == "gate" and judged < AUTO_GATE_SIZE:
        print(
            f"这一轮判了对错的一共 {judged} 组，不满 {AUTO_GATE_SIZE} 组，开关那道门过不了："
            "再抽一轮"
        )
    return 0


def stats(args):
    with session_factory()() as session:
        rows = list(
            session.scalars(
                select(RepostAutoCheck).order_by(RepostAutoCheck.sampled_at, RepostAutoCheck.id)
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
                sampled=0,
                judged=0,
                correct=0,
                changed=0,
                pending=0,
                sampled_at=row.sampled_at.isoformat(),
            ),
        )
        entry["sampled"] += 1
        entry["judged"] += row.verdict in ("correct", "wrong")
        entry["correct"] += row.verdict == "correct"
        entry["changed"] += row.verdict == "changed"
        entry["pending"] += row.verdict is None
    results = list(rounds.values())
    for entry in results:
        entry["accuracy"] = entry["correct"] / entry["judged"] if entry["judged"] else None
    output = {"rounds": results, "latest_gate": asdict(gate)}
    if args.json:
        print(json.dumps(output, ensure_ascii=False))
    else:
        for r in results:
            accuracy = "暂无" if r["accuracy"] is None else f"{r['accuracy']:.1%}"
            print(
                f"{r['round']} {r['purpose']} {r['rule_version']}：抽 {r['sampled']} 组，"
                f"判 {r['judged']} 组、对 {r['correct']} 组，准确率 {accuracy}；"
                f"组变了 {r['changed']} 组，还没判 {r['pending']} 组"
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
        before = {r.document_id: r.confirmed_by for r in rows if r.active and r.confirmed_at}
        withdrawn_ids = withdraw_group(group, rows, _now())
        session.commit()
        _audit(
            "repost_withdraw",
            group_id=group.id,
            document_ids=withdrawn_ids,
            confirmed_by=before,
            reviewer=args.by.strip(),
        )
        print(f"撤回 {len(withdrawn_ids)} 个版本，修订号 {group.revision}")
    return 0
