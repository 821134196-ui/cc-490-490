"""核心规则测试：班次唯一、故障阻断、换车容量、职责分离、放行幂等。"""
import threading

import pytest

from busdepot.db import Database
from busdepot.domain import (
    DomainError,
    Forbidden,
    complete_repair,
    create_shift,
    release_shift,
    report_fault,
    submit_inspection,
    swap_vehicle,
)

ALL_PASS = {"appearance": True, "deploy": True, "locking": True, "button": True}
DATE = "2026-10-01"


@pytest.fixture()
def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    conn = database.connect()
    conn.executemany(
        "INSERT INTO staff (id, name, role) VALUES (?,?,?)",
        [(1, "司机甲", "driver"), (2, "司机乙", "driver"),
         (3, "调度甲", "dispatcher"), (4, "调度乙", "dispatcher"),
         (5, "维修甲", "mechanic")],
    )
    conn.executemany(
        "INSERT INTO vehicles (id, fleet_no, plate_no, ramp_type, capacity,"
        " accessible, is_backup) VALUES (?,?,?,?,?,?,?)",
        [(1, "T-01", "沪A·0001", "电动伸缩式", 38, 1, 0),
         (2, "T-02", "沪A·0002", "手动翻转式", 38, 1, 0),
         (3, "备-小", "沪A·0003", "手动翻转式", 20, 1, 1),   # 容量不足
         (4, "备-大", "沪A·0004", "电动伸缩式", 40, 1, 1)],
    )
    conn.execute(
        "INSERT INTO routes (id, code, name, requires_accessible, min_capacity)"
        " VALUES (1, '71路', '测试线', 1, 35)")
    conn.commit()
    conn.close()
    return database


def make_shift(db, vehicle_id=1, depart="08:00"):
    return create_shift(db, route_id=1, vehicle_id=vehicle_id, driver_id=1,
                        service_date=DATE, depart_time=depart)["id"]


# ---------------------------------------------------------- 班次唯一

def test_shift_unique(db):
    make_shift(db, depart="08:00")
    with pytest.raises(DomainError, match="班次已存在"):
        make_shift(db, vehicle_id=2, depart="08:00")  # 同线路同日期同时刻


# ---------------------------------------------------------- 点检唯一

def test_one_valid_inspection_per_shift(db):
    sid = make_shift(db)
    submit_inspection(db, shift_id=sid, vehicle_id=1, inspector_id=1, items=ALL_PASS)
    with pytest.raises(DomainError, match="同一车辆同一班次只能有一份有效点检"):
        submit_inspection(db, shift_id=sid, vehicle_id=1, inspector_id=1, items=ALL_PASS)


# ---------------------------------------------------------- 故障阻断

def test_fault_blocks_release(db):
    sid = make_shift(db)
    # 点检不合格 -> 自动生成故障，车辆停用
    r = submit_inspection(db, shift_id=sid, vehicle_id=1, inspector_id=1,
                          items={**ALL_PASS, "deploy": False})
    assert r["result"] == "fail"
    with pytest.raises(DomainError):
        release_shift(db, shift_id=sid, actor_id=3, idempotency_key="k1")
    # 拦截已留痕
    conn = db.connect()
    n = conn.execute(
        "SELECT COUNT(*) c FROM events WHERE type='erroneous_release_blocked'"
    ).fetchone()["c"]
    conn.close()
    assert n == 1


def test_uninspected_vehicle_cannot_release(db):
    sid = make_shift(db)
    with pytest.raises(DomainError, match="缺少有效点检"):
        release_shift(db, shift_id=sid, actor_id=3, idempotency_key="k1")


def test_repair_then_reinspect_then_release(db):
    sid = make_shift(db)
    submit_inspection(db, shift_id=sid, vehicle_id=1, inspector_id=1,
                      items={**ALL_PASS, "locking": False})
    conn = db.connect()
    repair_id = conn.execute("SELECT id FROM repairs").fetchone()["id"]
    conn.close()
    complete_repair(db, repair_id=repair_id, actor_id=5)
    # 复检合格（维修完工后原不合格点检已作废，可重新点检同一班次）
    r = submit_inspection(db, shift_id=sid, vehicle_id=1, inspector_id=1, items=ALL_PASS)
    assert r["kind"] == "reinspection" and r["result"] == "pass"
    out = release_shift(db, shift_id=sid, actor_id=3, idempotency_key="k1")
    assert out["release_id"]


# ---------------------------------------------------------- 换车容量

def test_swap_capacity_enforced(db):
    sid = make_shift(db)
    with pytest.raises(DomainError, match="容量不足"):
        swap_vehicle(db, shift_id=sid, to_vehicle_id=3, actor_id=3)  # 备-小 20 < 35
    ok = swap_vehicle(db, shift_id=sid, to_vehicle_id=4, actor_id=3)  # 备-大 40 ≥ 35
    assert ok["vehicle_id"] == 4


def test_swap_rejects_disabled_vehicle(db):
    report_fault(db, vehicle_id=4, type_="ramp_jam", severity="access",
                 description="卡滞", reported_by=1)
    sid = make_shift(db)
    with pytest.raises(DomainError, match="不可用"):
        swap_vehicle(db, shift_id=sid, to_vehicle_id=4, actor_id=3)


# ---------------------------------------------------------- 职责分离

def test_role_separation(db):
    sid = make_shift(db)
    # 维修人员不能点检/复检
    with pytest.raises(Forbidden):
        submit_inspection(db, shift_id=sid, vehicle_id=1, inspector_id=5, items=ALL_PASS)
    # 调度员不能点检
    with pytest.raises(Forbidden):
        submit_inspection(db, shift_id=sid, vehicle_id=1, inspector_id=3, items=ALL_PASS)
    # 驾驶员不能换车
    with pytest.raises(Forbidden):
        swap_vehicle(db, shift_id=sid, to_vehicle_id=4, actor_id=1)
    # 驾驶员不能放行
    submit_inspection(db, shift_id=sid, vehicle_id=1, inspector_id=1, items=ALL_PASS)
    with pytest.raises(Forbidden):
        release_shift(db, shift_id=sid, actor_id=1, idempotency_key="k1")
    # 驾驶员不能完工维修单
    report_fault(db, vehicle_id=2, type_="ramp_jam", severity="access",
                 description="x", reported_by=1)
    conn = db.connect()
    repair_id = conn.execute("SELECT id FROM repairs").fetchone()["id"]
    conn.close()
    with pytest.raises(Forbidden):
        complete_repair(db, repair_id=repair_id, actor_id=1)


def test_mechanic_cannot_approve_reinspection(db):
    sid = make_shift(db)
    submit_inspection(db, shift_id=sid, vehicle_id=1, inspector_id=1,
                      items={**ALL_PASS, "button": False})
    conn = db.connect()
    repair_id = conn.execute("SELECT id FROM repairs").fetchone()["id"]
    conn.close()
    complete_repair(db, repair_id=repair_id, actor_id=5)
    with pytest.raises(Forbidden):
        submit_inspection(db, shift_id=sid, vehicle_id=1, inspector_id=5, items=ALL_PASS)


# ---------------------------------------------------------- 放行幂等

def test_release_idempotent_same_key(db):
    sid = make_shift(db)
    submit_inspection(db, shift_id=sid, vehicle_id=1, inspector_id=1, items=ALL_PASS)
    first = release_shift(db, shift_id=sid, actor_id=3, idempotency_key="same-key")
    second = release_shift(db, shift_id=sid, actor_id=3, idempotency_key="same-key")
    assert first["release_id"] == second["release_id"]
    assert second["idempotent"] is True
    conn = db.connect()
    n = conn.execute("SELECT COUNT(*) c FROM releases WHERE shift_id=?", (sid,)).fetchone()["c"]
    conn.close()
    assert n == 1


def test_release_duplicate_different_key_rejected(db):
    sid = make_shift(db)
    submit_inspection(db, shift_id=sid, vehicle_id=1, inspector_id=1, items=ALL_PASS)
    release_shift(db, shift_id=sid, actor_id=3, idempotency_key="key-a")
    with pytest.raises(DomainError, match="已放行"):
        release_shift(db, shift_id=sid, actor_id=4, idempotency_key="key-b")


def test_concurrent_release_only_one_succeeds(db):
    sid = make_shift(db)
    submit_inspection(db, shift_id=sid, vehicle_id=1, inspector_id=1, items=ALL_PASS)
    results, errors = [], []

    def attempt(key):
        try:
            results.append(release_shift(db, shift_id=sid, actor_id=3,
                                         idempotency_key=key))
        except DomainError as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=attempt, args=(f"concurrent-{i}",))
               for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 1, "两人并发放行只成功一次"
    assert len(errors) == 7
    conn = db.connect()
    n = conn.execute("SELECT COUNT(*) c FROM releases WHERE shift_id=?", (sid,)).fetchone()["c"]
    conn.close()
    assert n == 1
