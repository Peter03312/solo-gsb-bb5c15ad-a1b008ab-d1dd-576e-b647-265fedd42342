"""亚秒（sub-second）测温 / 交接时刻的端到端回归。

背景：责任区间接受亚秒级测温与交接时刻，但快照标识、表态片段坐标与封存
证据时间曾被截为整秒——同一整秒内的边界变化（如闭合时刻先有
10:00:01.900 的正常读数、后补录 10:00:01.100 的正常读数使其提前闭合）
不会改变快照，旧表态被误认有效、封存对照漏报（unchanged）。

本轮修复后：
* 快照标识、表态片段坐标、封存内容指纹按**实际时刻**（含亚秒）规范化，
  整秒时刻格式与历史逐字节一致（``…:01Z``），亚秒时刻带六位微秒
  （``…:01.100000Z``）；
* 亚秒边界一变，快照即变：旧快照表态 / 封存被拒（stale_snapshot），
  旧表态 is_current=false，封存对照为 changed / reopened / disappeared；
* 新封存包内容与摘要带亚秒；历史表态、已封存原包及摘要绝不改写；
* 旧记录中已丢失精度（坐标截为整秒）、当前片段含亚秒时，不得认定旧
  表态有效或对照 unchanged（无法证实片段仍相同）；
* HTTP 路径 / 字段 / 错误语义不变，整秒时刻的响应仍是秒级 Z 格式。
"""
from app.database import Base, SessionLocal, engine
from app.models import EvidenceSeal


def setup_cargo(client, cargo="CS", limit="4", holder="A"):
    r = client.post(
        "/cargo",
        json={
            "cargo_id": cargo,
            "started_at": "2026-01-01T09:00:00+00:00",
            "temp_upper_limit": limit,
            "initial_holder": holder,
        },
    )
    assert r.status_code == 201, r.text


def measure(client, eid, at, temp, cargo="CS"):
    r = client.post(
        "/events",
        json={
            "event_id": eid,
            "cargo_id": cargo,
            "occurred_at": at,
            "temperature": str(temp),
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def handover(client, eid, at, giver, receiver, cargo="CS"):
    r = client.post(
        "/events",
        json={
            "event_id": eid,
            "cargo_id": cargo,
            "occurred_at": at,
            "giver": giver,
            "receiver": receiver,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def interval_view(client, iid, cargo="CS"):
    r = client.get(f"/cargo/{cargo}/responsibilities/{iid}")
    assert r.status_code == 200, r.text
    return r.json()


def stance(client, iid, payload, cargo="CS", expected=201):
    r = client.post(
        f"/cargo/{cargo}/responsibilities/{iid}/stances", json=payload
    )
    assert r.status_code == expected, r.text
    return r


def seal(client, case_id, iid, snapshot, cargo="CS", expected=201):
    r = client.post(
        "/evidence-seals",
        json={
            "case_id": case_id,
            "cargo_id": cargo,
            "interval_id": iid,
            "snapshot_id": snapshot,
        },
    )
    assert r.status_code == expected, r.text
    return r


def build_subsecond_interval(client):
    """10:00:00.000 超限开启(A) → 10:00:00.500 交接 A→B →
    10:00:01.900 正常闭合。返回区间标识。"""
    setup_cargo(client)
    measure(client, "m0", "2026-01-01T10:00:00.000+00:00", 8)
    handover(client, "h0", "2026-01-01T10:00:00.500+00:00", "A", "B")
    measure(client, "m9", "2026-01-01T10:00:01.900+00:00", 2)
    return "m0"


# --------------------------------------------------------------- 快照与查询格式


def test_report_and_responsibilities_show_subsecond_times(client):
    """报告 / 责任视图的亚秒时刻带小数；整秒时刻保持秒级 Z 格式。"""
    iid = build_subsecond_interval(client)

    report = client.get("/cargo/CS/report").json()
    ex = report["excursions"][0]
    assert ex["start"] == "2026-01-01T10:00:00Z"          # 整秒不带小数
    assert ex["end"] == "2026-01-01T10:00:01.900000Z"    # 亚秒带微秒
    seg_a, seg_b = ex["segments"]
    assert seg_a["start"] == "2026-01-01T10:00:00Z"
    assert seg_a["end"] == "2026-01-01T10:00:00.500000Z"
    assert seg_b["start"] == "2026-01-01T10:00:00.500000Z"
    assert seg_b["end"] == "2026-01-01T10:00:01.900000Z"
    assert seg_a["responsible_party"] == "A"
    assert seg_b["responsible_party"] == "B"

    iv = interval_view(client, iid)
    assert iv["end"] == "2026-01-01T10:00:01.900000Z"
    assert iv["snapshot_id"].startswith("sha256:")
    assert len(iv["snapshot_id"]) == 71


def test_snapshot_distinguishes_subsecond_endpoint(client):
    """10:00:01.900 闭合与补录 10:00:01.100 提前闭合的快照必须不同。"""
    iid = build_subsecond_interval(client)
    snap_before = interval_view(client, iid)["snapshot_id"]

    measure(client, "m1", "2026-01-01T10:00:01.100+00:00", 2)
    iv = interval_view(client, iid)
    assert iv["end"] == "2026-01-01T10:00:01.100000Z"
    assert iv["snapshot_id"] != snap_before

    # 责任片段：交接点不变，B 片段终点提前到 .100
    seg_b = iv["segments"][1]
    assert seg_b["responsible_party"] == "B"
    assert seg_b["end"] == "2026-01-01T10:00:01.100000Z"


def test_whole_second_snapshot_format_unchanged():
    """整秒坐标的快照与历史秒级格式逐字节一致（旧标识不变）。"""
    import json
    from decimal import Decimal

    from app import responsibility as resp
    from app.chain import ExcursionResult, Segment

    ex = ExcursionResult(
        start=_dt("2026-01-01T10:00:00+00:00"),
        end=_dt("2026-01-01T10:00:50+00:00"),
        segments=(
            Segment(_dt("2026-01-01T10:00:00+00:00"),
                    _dt("2026-01-01T10:00:30+00:00"), "A"),
            Segment(_dt("2026-01-01T10:00:30+00:00"),
                    _dt("2026-01-01T10:00:50+00:00"), "B"),
        ),
        start_event_id="m0",
        end_event_id="m2",
    )
    # 规范化载荷中的时间必须仍是秒级 Z 格式（不带 .000000）
    triples = json.loads(resp.attested_segments_json(ex, "A"))
    assert triples == [["2026-01-01T10:00:00Z", "2026-01-01T10:00:30Z", "A"]]


def _dt(s):
    from datetime import datetime

    return datetime.fromisoformat(s)


# --------------------------------------------------------------- 表态有效性


def test_old_stance_rejected_and_marked_not_current_on_subsecond_backfill(client):
    """复现缺陷：先 .900 闭合并表态，后补录 .100 提前闭合——旧快照表态被拒、
    旧表态 is_current=false（即使整秒显示同为 10:00:01）。"""
    iid = build_subsecond_interval(client)
    snap = interval_view(client, iid)["snapshot_id"]
    stance(
        client, iid,
        {"snapshot_id": snap, "responsible_party": "B",
         "stance": "confirm", "stance_id": "st-b1"},
    )

    # 补录同整秒内更早的正常读数：边界提前
    measure(client, "m1", "2026-01-01T10:00:01.100+00:00", 2)
    new_snap = interval_view(client, iid)["snapshot_id"]
    assert new_snap != snap

    # 沿用旧快照（同一秒）的新表态必须被拒
    r = stance(
        client, iid,
        {"snapshot_id": snap, "responsible_party": "B",
         "stance": "confirm", "stance_id": "st-b2"},
        expected=409,
    )
    assert r.json()["error"]["code"] == "stale_snapshot"

    # 旧表态历史保留但已失效；current_snapshot_id 为亚秒新快照
    hist = client.get(f"/cargo/CS/responsibilities/{iid}/stances").json()
    assert hist["current_snapshot_id"] == new_snap
    assert len(hist["history"]) == 1
    assert hist["history"][0]["stance_id"] == "st-b1"
    assert hist["history"][0]["is_current"] is False

    # 按新快照可正常表态，且新表态对当前片段有效
    stance(
        client, iid,
        {"snapshot_id": new_snap, "responsible_party": "B",
         "stance": "confirm", "stance_id": "st-b3"},
    )
    hist = client.get(f"/cargo/CS/responsibilities/{iid}/stances").json()
    by_id = {h["stance_id"]: h["is_current"] for h in hist["history"]}
    assert by_id == {"st-b1": False, "st-b3": True}


def test_subsecond_handover_moves_segment_party_invalidation(client):
    """补录区间内的亚秒交接使责任片段变化：旧表态失效、快照变化。"""
    setup_cargo(client)
    measure(client, "m0", "2026-01-01T11:00:00.200+00:00", 8)
    measure(client, "m2", "2026-01-01T11:00:02.000+00:00", 2)
    iid = "m0"
    snap = interval_view(client, iid)["snapshot_id"]
    stance(
        client, iid,
        {"snapshot_id": snap, "responsible_party": "A",
         "stance": "confirm", "stance_id": "st-a1"},
    )

    # 区间内补录交接 11:00:01.100 A->B：A 片段缩短并新增 B 片段
    handover(client, "h1", "2026-01-01T11:00:01.100+00:00", "A", "B")
    iv = interval_view(client, iid)
    assert iv["snapshot_id"] != snap
    parties = [s["responsible_party"] for s in iv["segments"]]
    assert parties == ["A", "B"]

    r = stance(
        client, iid,
        {"snapshot_id": snap, "responsible_party": "A",
         "stance": "confirm", "stance_id": "st-a2"},
        expected=409,
    )
    assert r.json()["error"]["code"] == "stale_snapshot"
    hist = client.get(f"/cargo/CS/responsibilities/{iid}/stances").json()
    assert hist["history"][0]["is_current"] is False


def test_legacy_truncated_stance_not_current_when_current_has_subsecond(client):
    """模拟旧版本写入的表态（attested_segments 截为整秒）：当前片段含亚秒、
    无法证实仍相同时，不得认定旧表态有效。"""
    from app import responsibility as resp
    from app.chain import ExcursionResult, Segment
    from app.models import IntervalSource, Stance, StanceKind

    iid = build_subsecond_interval(client)
    # 当前区间（.900 闭合，B 片段 .500-.900）
    iv = interval_view(client, iid)
    assert iv["segments"][1]["end"] == "2026-01-01T10:00:01.900000Z"

    legacy = Stance(
        stance_id="legacy-b",
        cargo_id="CS",
        interval_key=iid,
        opener_source=IntervalSource.own,
        start_event_id=iid,
        vehicle_reading_id=None,
        responsible_party="B",
        kind=StanceKind.confirm,
        reason=None,
        # 旧版本坐标：微秒被截零（.500→整秒 .000，.900→整秒 .000）
        snapshot_id="sha256:" + "0" * 64,
        attested_segments=(
            '[["2026-01-01T10:00:00Z","2026-01-01T10:00:02Z","B"]]'
        ),
    )

    db = SessionLocal()
    try:
        from app.timeline import load_cargo_context

        events, loadings, readings, effective, quarantine, standards = (
            load_cargo_context(db, "CS")
        )
        cargo = db.get(__cargo_model(), "CS")
        _, closed = resp.rebuild(
            cargo, events, loadings, readings, effective, quarantine, standards
        )
        ex = closed[iid]
        assert resp.is_stance_current(legacy, ex) is False

        # 整秒边界的当前区间：旧整秒坐标与实际一致时仍有效（不误伤）
        setup_cargo(client, cargo="CW")
        measure(client, "w0", "2026-01-01T12:00:00+00:00", 8, cargo="CW")
        measure(client, "w1", "2026-01-01T12:00:50+00:00", 2, cargo="CW")
        events2, loadings2, readings2, eff2, q2, std2 = load_cargo_context(
            db, "CW"
        )
        cargo2 = db.get(__cargo_model(), "CW")
        _, closed2 = resp.rebuild(
            cargo2, events2, loadings2, readings2, eff2, q2, std2
        )
        whole = Stance(
            stance_id="legacy-w",
            cargo_id="CW",
            interval_key="w0",
            opener_source=IntervalSource.own,
            start_event_id="w0",
            vehicle_reading_id=None,
            responsible_party="A",
            kind=StanceKind.confirm,
            reason=None,
            snapshot_id="sha256:" + "1" * 64,
            attested_segments=(
                '[["2026-01-01T12:00:00Z","2026-01-01T12:00:50Z","A"]]'
            ),
        )
        assert resp.is_stance_current(whole, closed2["w0"]) is True
    finally:
        db.close()


def __cargo_model():
    from app.models import Cargo

    return Cargo


# --------------------------------------------------------------- 封存


def test_seal_content_keeps_subsecond_and_diff_is_changed(client):
    """新封存包按亚秒时刻记录；补录 .100 提前闭合后对照必须报 changed
    （而非漏报 unchanged），且原包 / 摘要绝不改写。"""
    iid = build_subsecond_interval(client)
    snap = interval_view(client, iid)["snapshot_id"]
    stance(
        client, iid,
        {"snapshot_id": snap, "responsible_party": "B",
         "stance": "confirm", "stance_id": "st-b1"},
    )
    r = seal(client, "CASE-SUB", iid, snap)
    body = r.json()
    digest = body["content_digest"]

    content = body["content"]
    assert content["interval"]["end"] == "2026-01-01T10:00:01.900000Z"
    seg_b = content["interval"]["segments"][1]
    assert seg_b["end"] == "2026-01-01T10:00:01.900000Z"
    assert seg_b["start"] == "2026-01-01T10:00:00.500000Z"
    # 证据事件时刻保留亚秒
    m9 = next(e for e in content["own_events"] if e["event_id"] == "m9")
    assert m9["occurred_at"] == "2026-01-01T10:00:01.900000Z"
    h0 = next(e for e in content["own_events"] if e["event_id"] == "h0")
    assert h0["occurred_at"] == "2026-01-01T10:00:00.500000Z"
    # 封存时有效表态的片段坐标带亚秒
    sealed_stance = content["current_valid_stances"][0]
    assert sealed_stance["segments"][0]["end"] == \
        "2026-01-01T10:00:01.900000Z"

    # 补录 .100 提前闭合
    measure(client, "m1", "2026-01-01T10:00:01.100+00:00", 2)

    d = client.get("/evidence-seals/CASE-SUB/diff").json()
    assert d["status"] == "changed"
    assert d["sealed_snapshot_id"] == snap
    assert d["current_snapshot_id"] != snap
    assert d["current_interval"]["end"] == "2026-01-01T10:00:01.100000Z"
    # B 片段（起点 .500）终点变化逐片段列出
    seg_changes = {x["start"]: x for x in d["segment_changes"]}
    changed_b = seg_changes["2026-01-01T10:00:00.500000Z"]
    assert changed_b["change"] == "changed"
    assert changed_b["sealed"][1] == "2026-01-01T10:00:01.900000Z"
    assert changed_b["current"][1] == "2026-01-01T10:00:01.100000Z"
    # 证据项：新增闭合读数 m1；m9 不再是闭合点但仍在区间时间范围内，
    # 作为区间内事件保留（其 read_at 不变），逐项可核
    own_changes = {x["key"]: x["change"]
                   for x in d["evidence_changes"]["own_events"]}
    assert own_changes.get("m1") == "added"

    # 原封存包与摘要恒不变
    got = client.get("/evidence-seals/CASE-SUB").json()
    assert got["content_digest"] == digest
    assert got["content"]["interval"]["end"] == "2026-01-01T10:00:01.900000Z"


def test_seal_with_stale_subsecond_snapshot_rejected_no_record(client):
    """先 .900 取得快照；补录 .100 后用旧快照封存 -> 409 stale_snapshot，
    不留封存记录。"""
    iid = build_subsecond_interval(client)
    snap = interval_view(client, iid)["snapshot_id"]
    measure(client, "m1", "2026-01-01T10:00:01.100+00:00", 2)

    seal(client, "CASE-STALE", iid, snap, expected=409)
    db = SessionLocal()
    try:
        assert db.get(EvidenceSeal, "CASE-STALE") is None
    finally:
        db.close()


def test_seal_replay_returns_original_package_after_subsecond_change(client):
    """同案件编号同请求重试：即使补录亚秒读数后现况已变，仍返回原包。"""
    iid = build_subsecond_interval(client)
    snap = interval_view(client, iid)["snapshot_id"]
    seal(client, "CASE-RP", iid, snap)

    measure(client, "m1", "2026-01-01T10:00:01.100+00:00", 2)
    r = seal(client, "CASE-RP", iid, snap, expected=200)
    body = r.json()
    assert body["replayed"] is True
    assert body["seal"]["snapshot_id"] == snap
    assert body["seal"]["content"]["interval"]["end"] == \
        "2026-01-01T10:00:01.900000Z"


def test_subsecond_interval_reopens_diff_status(client):
    """封存 .900 闭合的区间后，把闭合读数 m9 更正为超限值：区间再无正常
    闭合点而重新开放。对照明确 reopened（当前末段 end=null，起点保持亚秒
    坐标），原包不变。"""
    iid = build_subsecond_interval(client)
    snap = interval_view(client, iid)["snapshot_id"]
    seal(client, "CASE-RO", iid, snap)

    r = client.post(
        "/corrections",
        json={
            "correction_id": "corr-m9",
            "target_type": "own_event",
            "target_id": "m9",
            "expected_version": 0,
            "new_temperature": "9",
        },
    )
    assert r.status_code == 201, r.text
    report = client.get("/cargo/CS/report").json()
    assert len(report["excursions"]) == 1
    assert report["excursions"][0]["is_open"] is True

    d = client.get("/evidence-seals/CASE-RO/diff").json()
    assert d["status"] == "reopened"
    assert d["current_interval"]["is_open"] is True
    assert d["current_interval"]["end"] is None
    # 开放区间起点仍是亚秒交接切出的片段坐标
    cur_starts = [s["start"] for s in d["current_interval"]["segments"]]
    assert "2026-01-01T10:00:00.500000Z" in cur_starts
    assert d["current_interval"]["segments"][-1]["end"] is None
    # 封存的闭合片段在对照中体现变化，原封存包仍为 .900 闭合
    assert any(x["change"] == "changed" for x in d["segment_changes"])
    got = client.get("/evidence-seals/CASE-RO").json()
    assert got["content"]["interval"]["end"] == "2026-01-01T10:00:01.900000Z"


def test_legacy_sealed_package_with_truncated_times_not_unchanged(client):
    """旧封存包（内容时间截为整秒、快照为旧哈希）在当前区间含亚秒边界时：
    快照对不上 -> 不得报 unchanged（按现有查询字段明确 changed）。"""
    iid = build_subsecond_interval(client)
    current_snap = interval_view(client, iid)["snapshot_id"]

    # 直接构造一个“旧版本”封存包：interval 时间为秒级，snapshot 用旧秒级载荷
    db = SessionLocal()
    try:
        import hashlib
        import json

        from app.models import EvidenceSeal

        legacy_content = {
            "case_id": "CASE-OLD",
            "sealed_at": "2026-01-02T00:00:00Z",
            "cargo": {
                "cargo_id": "CS",
                "started_at": "2026-01-01T09:00:00Z",
                "temp_upper_limit": "4.000",
                "initial_holder": "A",
            },
            "interval": {
                "interval_id": iid,
                "start": "2026-01-01T10:00:00Z",
                "end": "2026-01-01T10:00:01Z",      # 精度丢失（实际 .900）
                "is_open": False,
                "start_source": "own",
                "end_source": "own",
                "start_event_id": "m0",
                "end_event_id": "m9",
                "start_reading_id": None,
                "end_reading_id": None,
                "start_trip_id": None,
                "end_trip_id": None,
                "snapshot_id": "sha256:" + "a" * 64,
                "segments": [
                    ["2026-01-01T10:00:00Z", "2026-01-01T10:00:00Z", "A"],
                    ["2026-01-01T10:00:00Z", "2026-01-01T10:00:01Z", "B"],
                ],
            },
            "current_valid_stances": [],
            "summary": "pending",
            "own_events": [
                {"event_id": "m0", "event_type": "measurement",
                 "occurred_at": "2026-01-01T10:00:00Z",
                 "temperature": "8.000", "effective_temperature": "8.000",
                 "version": 0, "temp_upper_limit": "4.000",
                 "giver": None, "receiver": None},
                {"event_id": "m9", "event_type": "measurement",
                 "occurred_at": "2026-01-01T10:00:01Z",
                 "temperature": "2.000", "effective_temperature": "2.000",
                 "version": 0, "temp_upper_limit": "4.000",
                 "giver": None, "receiver": None},
            ],
            "vehicle_readings": [],
            "loading_windows": [],
            "temp_standards": [
                {"version": 0, "effective_at": "2026-01-01T09:00:00Z",
                 "temp_upper_limit": "4.000", "change_id": None,
                 "reason": None}
            ],
        }
        canonical = json.dumps(
            legacy_content, ensure_ascii=False, separators=(",", ":")
        )
        digest = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
        row = EvidenceSeal(
            case_id="CASE-OLD",
            cargo_id="CS",
            interval_key=iid,
            snapshot_id="sha256:" + "a" * 64,
            request_hash=digest,
            content=canonical,
            content_digest=digest,
            sealed_at=_dt("2026-01-02T00:00:00+00:00"),
        )
        db.add(row)
        db.commit()
    finally:
        db.close()

    # 对照：当前快照（带亚秒）与旧秒级快照不同 -> changed，绝不 unchanged
    d = client.get("/evidence-seals/CASE-OLD/diff").json()
    assert d["status"] == "changed"
    assert d["current_snapshot_id"] == current_snap
    assert d["sealed_snapshot_id"] == "sha256:" + "a" * 64
    # 原包查询恒为旧内容（不补写精度）
    got = client.get("/evidence-seals/CASE-OLD").json()
    assert got["content"]["interval"]["end"] == "2026-01-01T10:00:01Z"
    assert got["content_digest"]
    # 片段 / 证据项差异按现有字段呈现
    assert d["segment_changes"], "精度丢失导致的片段差异必须逐项列出"
    own_changes = {x["key"]: x for x in d["evidence_changes"]["own_events"]}
    m9 = own_changes.get("m9")
    assert m9 is not None and m9["change"] == "changed"
    assert "occurred_at" in m9["fields"]


# --------------------------------------------------------------- 整秒回归


def test_whole_second_flow_completely_unchanged(client):
    """全整秒数据：快照 / 表态 / 封存 / 对照语义与格式与旧版本完全一致。"""
    setup_cargo(client, cargo="CZ")
    measure(client, "z0", "2026-01-01T10:00:00+00:00", 8, cargo="CZ")
    handover(client, "zh", "2026-01-01T10:00:30+00:00", "A", "B", cargo="CZ")
    measure(client, "z1", "2026-01-01T10:00:50+00:00", 2, cargo="CZ")
    iv = interval_view(client, "z0", cargo="CZ")
    # 整秒快照载荷为秒级格式
    assert iv["start"] == "2026-01-01T10:00:00Z"
    assert iv["end"] == "2026-01-01T10:00:50Z"
    snap = iv["snapshot_id"]

    stance(
        client, "z0",
        {"snapshot_id": snap, "responsible_party": "A",
         "stance": "confirm", "stance_id": "st-za"},
        cargo="CZ",
    )
    seal(client, "CASE-Z", "z0", snap, cargo="CZ")

    # 无任何变化 -> unchanged，表态仍有效
    d = client.get("/evidence-seals/CASE-Z/diff").json()
    assert d["status"] == "unchanged"
    hist = client.get("/cargo/CZ/responsibilities/z0/stances").json()
    assert hist["history"][0]["is_current"] is True

    got = client.get("/evidence-seals/CASE-Z").json()
    assert got["content"]["interval"]["end"] == "2026-01-01T10:00:50Z"
