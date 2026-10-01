"""演示数据与样例场景。

首次启动时执行：登记车辆/线路/人员/班次，并自动生成三个样例——
1. 坡板卡滞：S2-03 点检展开/收回不合格 → 故障 ramp_jam，无障碍功能停用
2. 临时换车：调度员把该班次换到备用车 备-01
3. 复检失败：维修完工后驾驶员复检仍不合格 → 故障重开
另含：一班合格放行、一班误放行拦截、一条已过期维修单（提醒线程记"维修延期"）、
一班已过发车未点检（提醒线程记"司机漏检"）。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

from .db import Database
from .domain import (
    DomainError,
    complete_repair,
    create_shift,
    release_shift,
    report_fault,
    submit_inspection,
    swap_vehicle,
)

ALL_PASS = {"appearance": True, "deploy": True, "locking": True, "button": True}


def _photo(photos_dir: str, name: str, title: str, color: str) -> str:
    """生成本地测试照片（SVG 占位图），返回文件名。"""
    os.makedirs(photos_dir, exist_ok=True)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360">'
        f'<rect width="640" height="360" fill="{color}"/>'
        f'<rect x="16" y="16" width="608" height="328" fill="none" stroke="#ffffff"'
        f' stroke-width="4" stroke-dasharray="12 8"/>'
        f'<text x="320" y="168" font-size="40" fill="#ffffff" text-anchor="middle"'
        f' font-family="sans-serif">{title}</text>'
        f'<text x="320" y="224" font-size="22" fill="#ffffff" text-anchor="middle"'
        f' font-family="sans-serif">演示测试照片 · 坡板点检</text></svg>'
    )
    with open(os.path.join(photos_dir, name), "w", encoding="utf-8") as fh:
        fh.write(svg)
    return name


def seed_if_empty(db: Database, photos_dir: str) -> bool:
    conn = db.connect()
    try:
        if conn.execute("SELECT COUNT(*) c FROM staff").fetchone()["c"]:
            return False
    finally:
        conn.close()

    today = datetime.now().date()
    today_s = today.isoformat()

    def t(**kw):  # 相对当前时间的发车时刻
        return (datetime.now() + timedelta(**kw)).strftime("%H:%M")

    conn = db.connect()
    try:
        staff = [
            (1, "王建国", "driver"), (2, "李秀兰", "driver"),
            (3, "张伟民", "driver"), (4, "赵燕", "driver"),
            (5, "陈晨", "dispatcher"), (6, "周正", "dispatcher"),
            (7, "刘铁柱", "mechanic"), (8, "孙鹏", "mechanic"),
        ]
        conn.executemany("INSERT INTO staff (id, name, role) VALUES (?,?,?)", staff)
        vehicles = [
            (1, "S2-01", "沪A·D7101", "电动伸缩式", 38, 1, 0),
            (2, "S2-02", "沪A·D7102", "手动翻转式", 38, 1, 0),
            (3, "S2-03", "沪A·D7103", "电动伸缩式", 40, 1, 0),
            (4, "S2-04", "沪A·D7104", "手动翻转式", 35, 1, 0),
            (5, "S3-01", "沪A·D7301", "电动伸缩式", 42, 1, 0),
            (6, "S3-02", "沪A·D7302", "手动翻转式", 36, 1, 0),
            (7, "备-01", "沪A·D7901", "电动伸缩式", 40, 1, 1),
            (8, "备-02", "沪A·D7902", "手动翻转式", 28, 1, 1),
        ]
        conn.executemany(
            "INSERT INTO vehicles (id, fleet_no, plate_no, ramp_type, capacity,"
            " accessible, is_backup) VALUES (?,?,?,?,?,?,?)", vehicles)
        routes = [
            (1, "71路", "延安路骨干线", 1, 35),
            (2, "122路", "虹桥枢纽线", 1, 30),
            (3, "303路", "夜宵线", 0, 25),
        ]
        conn.executemany(
            "INSERT INTO routes (id, code, name, requires_accessible, min_capacity)"
            " VALUES (?,?,?,?,?)", routes)
        conn.commit()
    finally:
        conn.close()

    # 班次
    s1 = create_shift(db, route_id=1, vehicle_id=1, driver_id=1,
                      service_date=today_s, depart_time=t(minutes=-30))["id"]
    s2 = create_shift(db, route_id=1, vehicle_id=2, driver_id=2,
                      service_date=today_s, depart_time=t(minutes=10))["id"]
    s3 = create_shift(db, route_id=2, vehicle_id=3, driver_id=3,
                      service_date=today_s, depart_time=t(minutes=40))["id"]
    create_shift(db, route_id=2, vehicle_id=4, driver_id=4,
                 service_date=today_s, depart_time=t(minutes=95))
    create_shift(db, route_id=3, vehicle_id=5, driver_id=1,
                 service_date=today_s, depart_time=t(minutes=-50))  # 将记"司机漏检"
    create_shift(db, route_id=1, vehicle_id=6, driver_id=2,
                 service_date=today_s, depart_time=t(minutes=150))

    jam_photo = _photo(photos_dir, "sample-ramp-jam.svg", "样例：坡板卡滞", "#b3401e")
    ok_photo = _photo(photos_dir, "sample-inspect-ok.svg", "样例：点检合格", "#1256a0")

    # 样例一：坡板卡滞 —— S2-03 展开/收回测试不合格
    submit_inspection(
        db, shift_id=s3, vehicle_id=3, inspector_id=3,
        items={**ALL_PASS, "deploy": False},
        notes="坡板展开到一半卡死，无法到位",
        photos=[{"item": "deploy", "path": jam_photo}],
    )

    # 样例二：临时换车 —— 调度员把班次换到备用车 备-01
    swap_vehicle(db, shift_id=s3, to_vehicle_id=7, actor_id=5,
                 reason="S2-03 坡板卡滞，无障碍功能停用")

    # 样例三：复检失败 —— 维修完工后复检仍不合格，故障重开
    conn = db.connect()
    try:
        repair_id = conn.execute(
            "SELECT id FROM repairs WHERE vehicle_id=3 ORDER BY id LIMIT 1"
        ).fetchone()["id"]
    finally:
        conn.close()
    complete_repair(db, repair_id=repair_id, actor_id=7)
    submit_inspection(
        db, shift_id=None, vehicle_id=3, inspector_id=1,
        items={**ALL_PASS, "deploy": False},
        notes="复检：坡板展开仍卡滞，需返修",
    )
    # 让这张返修单已过期，提醒线程会记"维修延期"
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE repairs SET deadline=? WHERE vehicle_id=3 AND status='pending'",
            ((datetime.now() - timedelta(minutes=20)).replace(microsecond=0).isoformat(sep=" "),),
        )
        conn.commit()
    finally:
        conn.close()

    # 正常样例：S2-01 点检合格并放行
    submit_inspection(db, shift_id=s1, vehicle_id=1, inspector_id=1, items=ALL_PASS,
                      notes="各项正常", photos=[{"item": "appearance", "path": ok_photo}])
    release_shift(db, shift_id=s1, actor_id=5, idempotency_key=f"seed-release-{s1}")

    # 误放行拦截样例：S2-02 未点检，调度员尝试放行被拦截留痕
    try:
        release_shift(db, shift_id=s2, actor_id=6, idempotency_key=f"seed-bad-{s2}")
    except DomainError:
        pass

    # 一条整车待修样例（门控联锁故障）
    report_fault(db, vehicle_id=6, type_="other", severity="vehicle",
                 description="门控联锁故障，整车待修", reported_by=2)
    return True
