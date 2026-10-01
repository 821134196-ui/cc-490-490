"""演示数据 + 三个必选样例：坡板卡滞、临时换车、复检失败。"""
from __future__ import annotations

import os
from datetime import timedelta

from .store import (Store, ROLE_DRIVER, ROLE_DISPATCHER, ROLE_MECHANIC,
                    VEH_IN_SERVICE, VEH_RESERVED, F_RAMP_JAM, F_BUTTON_DEAD,
                    F_PIN_MISSING, F_LOAD_LABEL_LOST, F_DRIVER_MISSED,
                    F_MISRELEASE, CONSEQUENCE_RAMP, CONSEQUENCE_FULL,
                    REPAIR_DONE, now, ts)

# 演示人员
DEMO_STAFF = [
    ("S-D01", "王建国", ROLE_DRIVER),
    ("S-D02", "李海涛", ROLE_DRIVER),
    ("S-D03", "陈晓峰", ROLE_DRIVER),
    ("S-D04", "赵明亮", ROLE_DRIVER),
    ("S-C01", "调度-周琳", ROLE_DISPATCHER),
    ("S-C02", "调度-孙倩", ROLE_DISPATCHER),
    ("S-M01", "维修-刘工", ROLE_MECHANIC),
    ("S-M02", "维修-吴工", ROLE_MECHANIC),
]

DEMO_LINES = [
    ("L-1", "1路", "火车站—市民中心", 1),
    ("L-7", "7路", "东站—人民医院", 1),
    ("L-12", "12路", "公交南站—大学城", 1),
    ("L-42", "42路", "机场—会展中心", 1),
    ("L-K3", "K3", "夜间快线（环线）", 0),  # 不强制无障碍
]

# id, 自编号, 车牌, 坡板类型, 承重kg, 无障碍, 备用
DEMO_VEHICLES = [
    ("V-101", "101", "苏A·10123", "MANUAL_FOLD", 300, 1, 0),
    ("V-102", "102", "苏A·10245", "ELECTRIC", 350, 1, 0),
    ("V-103", "103", "苏A·10367", "SEMI_AUTO", 300, 1, 0),
    ("V-104", "104", "苏A·10488", "MANUAL_FOLD", 300, 1, 0),
    ("V-105", "105", "苏A·10592", "ELECTRIC", 400, 1, 0),
    ("V-106", "106", "苏A·10631", "MANUAL_FOLD", 250, 1, 0),
    # 备用车
    ("V-901", "备901", "苏A·B9011", "ELECTRIC", 350, 1, 1),
    ("V-902", "备902", "苏A·B9022", "MANUAL_FOLD", 300, 1, 1),
    ("V-903", "备903", "苏A·B9033", "SEMI_AUTO", 300, 1, 1),
]

ALL_PASS = {k: True for k in
            ("appearance", "deploy_retract", "locking",
             "call_button", "pin_present", "load_label")}
# 各缺陷组合
FAIL_JAM = {**ALL_PASS, "deploy_retract": False}          # 坡板卡滞
FAIL_PIN = {**ALL_PASS, "pin_present": False}             # 固定销缺失
FAIL_BUTTON = {**ALL_PASS, "call_button": False}          # 按钮无响应
FAIL_LABEL = {**ALL_PASS, "load_label": False}            # 承重标识脱落


def _min_photo(name: str) -> bytes:
    """1×1 PNG 占位照片。"""
    import base64
    png = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
           "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
    return base64.b64decode(png)


def seed(store: Store, photo_dir: str | None = None, *, base_time=None) -> dict:
    """重建演示数据；base_time 可注入（测试用）。"""
    from .adapters import PhotoStorageAdapter

    store.reset()
    base = base_time or now()
    conn = store.conn

    for sid, name, role in DEMO_STAFF:
        conn.execute("INSERT INTO staff(id,name,role) VALUES(?,?,?)", (sid, name, role))
    for lid, code, name, req in DEMO_LINES:
        conn.execute(
            "INSERT INTO lines(id,code,name,accessible_required) VALUES(?,?,?,?)",
            (lid, code, name, req))
    for vid, fleet, plate, rtype, load, acc, spare in DEMO_VEHICLES:
        conn.execute("""INSERT INTO vehicles(id,fleet_no,plate,ramp_type,load_rating_kg,
            accessible,is_spare,status) VALUES(?,?,?,?,?,?,?,?)""",
                     (vid, fleet, plate, rtype, load, acc, spare,
                      VEH_RESERVED if spare else VEH_IN_SERVICE))

    shift_id = "SH-0730"
    conn.execute("INSERT INTO shifts(id,label,work_date,active) VALUES(?,?,?,1)",
                 (shift_id, "早班 07:30", base.strftime("%Y-%m-%d")))

    # 待发板：发车时间相对当前 8~55 分钟
    trip_plan = [
        ("L-1", "V-101", "S-D01", 8),
        ("L-7", "V-102", "S-D02", 16),
        ("L-12", "V-103", "S-D03", 24),
        ("L-42", "V-104", "S-D04", 32),
        ("L-7", "V-105", "S-D01", 42),
        ("L-1", "V-106", "S-D02", 52),
        ("L-K3", "V-103", "S-D03", 60),  # 同车后一班（演示唯一约束冲突场景）
    ]
    trip_ids = []
    for i, (line, veh, drv, mins) in enumerate(trip_plan, 1):
        tid = f"TR-{i:03d}"
        conn.execute("""INSERT INTO trips(id,shift_id,line_id,vehicle_id,driver_id,
            seq,departure_at,status) VALUES(?,?,?,?,?,?,?,?)""",
                     (tid, shift_id, line, veh, drv, i,
                      ts(base + timedelta(minutes=mins)), "PLANNED"))
        trip_ids.append(tid)
    conn.commit()

    photos = None
    if photo_dir:
        photos = PhotoStorageAdapter(photo_dir)
        for nm in ("ramp_deploy_pass.png", "ramp_jam.png", "call_button.png",
                   "pin_missing.png", "load_label.png", "retest_fail.png"):
            photos.save(nm, _min_photo(nm))

    samples = {}

    # —— 样例 1：V-103 坡板卡滞（TR-003）→ 无障碍停用 → 报修 → 维修 → 复检通过
    jam = store.submit_inspection(
        shift_id=shift_id, vehicle_id="V-103", driver_id="S-D03",
        trip_id="TR-003", items=FAIL_JAM,
        ramp_test={"deploy_seconds": 22.5, "retract_seconds": 18.0, "cycles": 1,
                   "result": "FAIL", "detail": "展开至 60% 卡滞，需人工助力"},
        note="坡板展开卡滞，无法到位",
        photos=["ramp_jam.png"])
    flt1 = store.report_fault(
        vehicle_id="V-103", fault_type=F_RAMP_JAM, reported_by="S-D03",
        consequence=CONSEQUENCE_RAMP, description="展开至 60% 位置卡滞",
        trip_id="TR-003", inspection_id=jam["inspection_id"])
    # 调度员临时换车 → 备901（样例 2）
    swap = store.swap_vehicle(
        trip_id="TR-003", dispatcher_id="S-C01", spare_vehicle_id="V-901",
        reason="坡板卡滞，临时调配无障碍备用车")
    # 换上车须完成点检（合格）
    store.submit_inspection(
        shift_id=shift_id, vehicle_id="V-901", driver_id="S-D03",
        trip_id="TR-003", items=ALL_PASS,
        ramp_test={"deploy_seconds": 6.2, "retract_seconds": 5.4, "cycles": 2,
                   "result": "PASS", "detail": "电动展开收回顺畅"})
    rel1 = store.release(trip_id="TR-003", dispatcher_id="S-C01",
                         gate_adapter=_Gate(), terminal_adapter=_Term())
    # 维修
    rpr1 = store.open_repair(fault_id=flt1["fault_id"], mechanic_id="S-M01",
                             due_minutes=45)
    store.complete_repair(repair_id=rpr1["repair_id"], mechanic_id="S-M01",
                          summary="更换滑轨轴承并润滑", bench_test="PASS")
    retest = store.reinspect(
        repair_id=rpr1["repair_id"], driver_id="S-D03", items=ALL_PASS,
        ramp_test={"deploy_seconds": 6.8, "retract_seconds": 5.9, "cycles": 3,
                   "result": "PASS", "detail": "连续 3 次展开收回顺畅"})
    samples["ramp_jam_swap"] = {"fault": flt1, "swap": swap,
                                "release": rel1, "repair": rpr1,
                                "reinspection": retest}

    # —— 样例 3：V-102 呼叫按钮无响应 → 整车待修 → 维修完工 → 复检失败
    btn = store.submit_inspection(
        shift_id=shift_id, vehicle_id="V-102", driver_id="S-D02",
        trip_id="TR-002", items=FAIL_BUTTON,
        ramp_test={"deploy_seconds": 7.1, "retract_seconds": 6.0, "cycles": 1,
                   "result": "PASS", "detail": "坡板动作正常"},
        note="坡板正常但车内呼叫按钮无响应", photos=["call_button.png"])
    flt2 = store.report_fault(
        vehicle_id="V-102", fault_type=F_BUTTON_DEAD, reported_by="S-D02",
        consequence=CONSEQUENCE_FULL, description="按压呼叫按钮终端无反应",
        trip_id="TR-002", inspection_id=btn["inspection_id"])
    # 备用车容量样例：先把备902、备903 通过其它故障线路占用
    pin = store.submit_inspection(
        shift_id=shift_id, vehicle_id="V-105", driver_id="S-D01",
        trip_id="TR-005", items=FAIL_PIN, note="固定销缺失",
        photos=["pin_missing.png"])
    flt3 = store.report_fault(
        vehicle_id="V-105", fault_type=F_PIN_MISSING, reported_by="S-D01",
        consequence=CONSEQUENCE_RAMP, trip_id="TR-005",
        inspection_id=pin["inspection_id"])
    store.swap_vehicle(trip_id="TR-005", dispatcher_id="S-C01",
                       spare_vehicle_id="V-902", reason="固定销缺失换车")
    # V-105 固定销维修：开工后因缺料延期（维修延期留痕）
    rpr3 = store.open_repair(fault_id=flt3["fault_id"], mechanic_id="S-M01",
                             due_minutes=30)
    store.delay_repair(repair_id=rpr3["repair_id"], mechanic_id="S-M01",
                       reason="原厂固定销缺料，预计 2 小时后到场")
    lbl = store.submit_inspection(
        shift_id=shift_id, vehicle_id="V-106", driver_id="S-D02",
        trip_id="TR-006", items=FAIL_LABEL, note="承重标识脱落",
        photos=["load_label.png"])
    flt4 = store.report_fault(
        vehicle_id="V-106", fault_type=F_LOAD_LABEL_LOST, reported_by="S-D02",
        consequence=CONSEQUENCE_RAMP, trip_id="TR-006",
        inspection_id=lbl["inspection_id"])
    store.swap_vehicle(trip_id="TR-006", dispatcher_id="S-C01",
                       spare_vehicle_id="V-903", reason="承重标识脱落换车")
    # 此时已无备用车：TR-002 换车应失败（容量约束）——仅留痕尝试
    try:
        store.swap_vehicle(trip_id="TR-002", dispatcher_id="S-C01",
                           reason="按钮无响应，申请备用车")
        capacity = "unexpected-success"
    except Exception as exc:  # BizError SPARE_NONE_AVAILABLE
        store.event("SWAP_DENIED_CAPACITY", vehicle_id="V-102", trip_id="TR-002",
                    actor_id="S-C01", actor_role=ROLE_DISPATCHER,
                    detail={"reason": str(exc)})
        capacity = f"denied: {getattr(exc, 'code', 'ERR')}"

    rpr2 = store.open_repair(fault_id=flt2["fault_id"], mechanic_id="S-M02",
                             due_minutes=-5)  # 直接已超时，供提醒演示
    store.complete_repair(repair_id=rpr2["repair_id"], mechanic_id="S-M02",
                          summary="更换按钮线束", bench_test="PASS")
    # 复检仍失败：按钮还是无响应
    retest_fail = store.reinspect(
        repair_id=rpr2["repair_id"], driver_id="S-D02", items=FAIL_BUTTON,
        ramp_test={"deploy_seconds": 7.0, "retract_seconds": 6.1, "cycles": 1,
                   "result": "PASS", "detail": "坡板正常，复检按钮仍无响应"},
        note="复检不合格：按钮依旧无响应", photos=["retest_fail.png"])
    samples["retest_fail"] = {"fault": flt2, "repair": rpr2,
                              "reinspection": retest_fail,
                              "spare_capacity": capacity}

    # —— 管理类留痕：司机漏检 / 误放行 / 维修延期
    store.report_fault(vehicle_id="V-101", fault_type=F_DRIVER_MISSED,
                       reported_by="S-C01", consequence="NONE",
                       description="TR-001 临近发车仍未提交点检，系统标记司机漏检",
                       trip_id="TR-001")
    store.event("DRIVER_MISSED_CHECK", vehicle_id="V-101", trip_id="TR-001",
                actor_id="S-C01", actor_role=ROLE_DISPATCHER,
                detail={"note": "发车前 15 分钟未点检"})
    # 误放行留痕：演示中以事件记录“历史误放行”，不写入 releases 成功单
    store.report_fault(vehicle_id="V-104", fault_type=F_MISRELEASE,
                       reported_by="S-C02", consequence="NONE",
                       description="历史班次曾在坡板未点检情况下被人工抬杆放行，已追溯登记",
                       trip_id="TR-004")
    store.event("MISRELEASE_FLAGGED", vehicle_id="V-104", trip_id="TR-004",
                actor_id="S-C02", actor_role=ROLE_DISPATCHER,
                detail={"severity": "high", "action": "上报并通报"})

    # 正常线路：V-104 当前班次补一份合格点检
    store.submit_inspection(
        shift_id=shift_id, vehicle_id="V-104", driver_id="S-D04",
        trip_id="TR-004", items=ALL_PASS,
        ramp_test={"deploy_seconds": 6.5, "retract_seconds": 5.5, "cycles": 2,
                   "result": "PASS"})

    # 驾驶员交接样例
    store.handover(trip_id="TR-003", driver_id="S-D03",
                   shift_note="原车103坡板卡滞已停用，换乘备901，呼叫设备正常")

    return {"shift_id": shift_id, "trip_ids": trip_ids, "samples": samples}


class _Gate:
    """种子阶段简化门控（避免耦合适配器实例）。"""
    n = 0

    def open_gate(self, trip, insp):
        type(self).n += 1
        return {"signal": f"GATE:OPEN:SEED{type(self).n}", "barrier": "RAISED"}


class _Term:
    n = 0

    def dispatch_notice(self, trip, insp):
        type(self).n += 1
        return {"signal": f"TERM:DISPATCH:SEED{type(self).n}",
                "message": "准予发车"}
