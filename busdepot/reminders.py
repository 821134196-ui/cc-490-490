"""进程内任务提醒：发车时限与维修超时。

reminder_tick 由后台线程周期调用，负责把"司机漏检""维修延期"写入留痕
（各只记一次）；compute_reminders 供 API 实时计算当前提醒列表。
"""
from __future__ import annotations

import threading
from datetime import datetime

from .db import Database
from .domain import log_event

DEPART_WARN_MINUTES = 15  # 发车前多少分钟开始提醒


def _shift_dt(row) -> datetime:
    return datetime.fromisoformat(f"{row['service_date']} {row['depart_time']}")


def compute_reminders(db: Database) -> list[dict]:
    conn = db.connect()
    try:
        now = datetime.now()
        rems: list[dict] = []
        shifts = conn.execute(
            "SELECT s.*, r.code route_code, v.fleet_no FROM shifts s"
            " JOIN routes r ON r.id=s.route_id JOIN vehicles v ON v.id=s.vehicle_id"
            " WHERE s.status='pending'",
        ).fetchall()
        for s in shifts:
            dt = _shift_dt(s)
            mins = (dt - now).total_seconds() / 60
            insp = conn.execute(
                "SELECT result FROM inspections WHERE shift_id=? AND vehicle_id=? AND valid=1",
                (s["id"], s["vehicle_id"]),
            ).fetchone()
            passed = insp is not None and insp["result"] == "pass"
            if not passed and 0 <= mins <= DEPART_WARN_MINUTES:
                rems.append({
                    "type": "departure_deadline",
                    "message": f"班次#{s['id']}（{s['route_code']} {s['depart_time']} "
                               f"{s['fleet_no']}）距发车 {int(mins)} 分钟，点检未完成",
                })
            elif not passed and mins < 0:
                rems.append({
                    "type": "departure_overdue",
                    "message": f"班次#{s['id']}（{s['route_code']} {s['depart_time']} "
                               f"{s['fleet_no']}）已过发车时间仍未放行",
                })
        repairs = conn.execute(
            "SELECT rp.*, v.fleet_no FROM repairs rp JOIN vehicles v ON v.id=rp.vehicle_id"
            " WHERE rp.status='pending'",
        ).fetchall()
        for rp in repairs:
            overdue = (now - datetime.fromisoformat(rp["deadline"])).total_seconds() / 60
            if overdue > 0:
                rems.append({
                    "type": "repair_overdue",
                    "message": f"维修单#{rp['id']}（{rp['fleet_no']}）已超时 "
                               f"{int(overdue)} 分钟未完工",
                })
        return rems
    finally:
        conn.close()


def reminder_tick(db: Database) -> None:
    """周期任务：司机漏检与维修延期留痕（各只记一次）。"""
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        now = datetime.now()
        shifts = conn.execute(
            "SELECT s.*, r.code route_code, v.fleet_no, st.name driver_name"
            " FROM shifts s JOIN routes r ON r.id=s.route_id"
            " JOIN vehicles v ON v.id=s.vehicle_id JOIN staff st ON st.id=s.driver_id"
            " WHERE s.status='pending' AND s.missed_inspection_logged=0",
        ).fetchall()
        for s in shifts:
            if _shift_dt(s) >= now:
                continue
            insp = conn.execute(
                "SELECT 1 FROM inspections WHERE shift_id=? AND vehicle_id=? LIMIT 1",
                (s["id"], s["vehicle_id"]),
            ).fetchone()
            if insp:
                conn.execute("UPDATE shifts SET missed_inspection_logged=1 WHERE id=?",
                             (s["id"],))
                continue
            log_event(
                conn, "driver_missed_inspection",
                f"司机漏检：班次#{s['id']}（{s['route_code']} {s['depart_time']} "
                f"{s['fleet_no']}）已过发车时间，驾驶员 {s['driver_name']} 未提交点检",
                {"shift_id": s["id"], "driver_id": s["driver_id"]},
            )
            conn.execute("UPDATE shifts SET missed_inspection_logged=1 WHERE id=?", (s["id"],))
        repairs = conn.execute(
            "SELECT rp.*, v.fleet_no FROM repairs rp JOIN vehicles v ON v.id=rp.vehicle_id"
            " WHERE rp.status='pending' AND rp.delayed_logged=0",
        ).fetchall()
        for rp in repairs:
            if datetime.fromisoformat(rp["deadline"]) >= now:
                continue
            log_event(
                conn, "repair_delayed",
                f"维修延期：维修单#{rp['id']}（{rp['fleet_no']}）超过时限仍未完工",
                {"repair_id": rp["id"], "vehicle_id": rp["vehicle_id"]},
            )
            conn.execute("UPDATE repairs SET delayed_logged=1 WHERE id=?", (rp["id"],))
        conn.commit()
    finally:
        conn.close()


def start_reminder_thread(db: Database, interval_s: float = 5.0) -> threading.Thread:
    stop = threading.Event()

    def loop():
        while not stop.wait(interval_s):
            try:
                reminder_tick(db)
                for rem in compute_reminders(db):
                    print(f"[提醒] {rem['message']}", flush=True)
            except Exception as exc:  # 提醒线程不拖垮主进程
                print(f"[提醒] 任务异常：{exc}", flush=True)

    thread = threading.Thread(target=loop, name="reminder", daemon=True)
    thread.start()
    return thread
