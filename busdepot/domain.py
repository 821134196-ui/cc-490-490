"""领域规则：点检、故障、维修、复检、换车、交接、放行。

约束落点：
- 班次唯一            -> shifts UNIQUE(route_id, service_date, depart_time)
- 同一车辆同一班次一份有效点检 -> inspections 部分唯一索引 + 事务内预检
- 故障阻断放行        -> release_shift 中检查车辆状态与未闭环故障
- 换车容量/无障碍匹配  -> swap_vehicle 中按线路要求校验
- 职责分离            -> 驾驶员点检/复检，调度员换车/放行，维修工完成维修且不能复检
- 放行幂等            -> releases UNIQUE(shift_id) + idempotency_key，并发只成功一次
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from .db import Database

ROLE_LABELS = {"driver": "驾驶员", "dispatcher": "调度员", "mechanic": "维修人员"}

FAULT_TYPES = {
    "ramp_jam": "坡板卡滞",
    "pin_missing": "固定销缺失",
    "button_no_response": "按钮无响应",
    "label_peeling": "承重标识脱落",
    "other": "其他故障",
}

ITEM_LABELS = {
    "appearance": "坡板外观与承重标识",
    "deploy": "坡板展开/收回测试",
    "locking": "锁止与固定销",
    "button": "车内呼叫按钮",
}

# 点检不合格项 -> 故障类型
ITEM_TO_FAULT = {
    "appearance": "label_peeling",
    "deploy": "ramp_jam",
    "locking": "pin_missing",
    "button": "button_no_response",
}

VEHICLE_STATUS_LABELS = {
    "active": "正常",
    "access_disabled": "无障碍功能停用",
    "out_of_service": "整车待修",
}

REPAIR_SLA_HOURS = 4  # 维修时限


class DomainError(Exception):
    status = 409


class Forbidden(DomainError):
    status = 403


def now() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def log_event(conn, type_: str, message: str, payload: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO events (type, message, payload, created_at) VALUES (?,?,?,?)",
        (type_, message, json.dumps(payload or {}, ensure_ascii=False), now()),
    )


def _one(conn, sql: str, args=(), err: str | None = None):
    row = conn.execute(sql, args).fetchone()
    if row is None and err:
        raise DomainError(err)
    return row


def get_staff(conn, staff_id: int):
    return _one(conn, "SELECT * FROM staff WHERE id=?", (staff_id,), "人员不存在")


def require_role(staff, *roles: str) -> None:
    if staff["role"] not in roles:
        want = "、".join(ROLE_LABELS[r] for r in roles)
        raise Forbidden(f"职责分离：{ROLE_LABELS[staff['role']]}不能执行该操作（需{want}）")


# ---------------------------------------------------------------- 班次

def create_shift(db: Database, *, route_id: int, vehicle_id: int, driver_id: int,
                 service_date: str, depart_time: str) -> dict:
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _one(conn, "SELECT id FROM routes WHERE id=?", (route_id,), "线路不存在")
        _one(conn, "SELECT id FROM vehicles WHERE id=?", (vehicle_id,), "车辆不存在")
        driver = get_staff(conn, driver_id)
        require_role(driver, "driver")
        try:
            cur = conn.execute(
                "INSERT INTO shifts (route_id, vehicle_id, driver_id, service_date, depart_time)"
                " VALUES (?,?,?,?,?)",
                (route_id, vehicle_id, driver_id, service_date, depart_time),
            )
        except sqlite3.IntegrityError:
            raise DomainError("班次已存在：同一线路同一日期同一发车时间只能有一个班次")
        conn.commit()
        return {"id": cur.lastrowid}
    finally:
        conn.close()


# ---------------------------------------------------------------- 故障（内部）

def _create_repair(conn, fault_id: int, vehicle_id: int) -> int:
    deadline = (datetime.now() + timedelta(hours=REPAIR_SLA_HOURS)).replace(microsecond=0).isoformat(sep=" ")
    cur = conn.execute(
        "INSERT INTO repairs (fault_id, vehicle_id, deadline) VALUES (?,?,?)",
        (fault_id, vehicle_id, deadline),
    )
    return cur.lastrowid


def _refresh_vehicle_status(conn, vehicle_id: int) -> None:
    """存在未闭环故障（open 或 repaired 待复检）时车辆保持停用。"""
    blocking = conn.execute(
        "SELECT severity FROM faults WHERE vehicle_id=? AND status IN ('open','repaired')",
        (vehicle_id,),
    ).fetchall()
    if not blocking:
        status = "active"
    elif any(f["severity"] == "vehicle" for f in blocking):
        status = "out_of_service"
    else:
        status = "access_disabled"
    conn.execute("UPDATE vehicles SET status=? WHERE id=?", (status, vehicle_id))


def _create_fault(conn, *, vehicle_id: int, shift_id, inspection_id, type_: str,
                  severity: str, description: str, reported_by: int) -> int:
    cur = conn.execute(
        "INSERT INTO faults (vehicle_id, shift_id, inspection_id, type, severity,"
        " description, status, reported_by, created_at) VALUES (?,?,?,?,?,?,'open',?,?)",
        (vehicle_id, shift_id, inspection_id, type_, severity, description, reported_by, now()),
    )
    fault_id = cur.lastrowid
    _create_repair(conn, fault_id, vehicle_id)
    vehicle = _one(conn, "SELECT fleet_no FROM vehicles WHERE id=?", (vehicle_id,))
    label = FAULT_TYPES.get(type_, type_)
    sev_label = "整车待修" if severity == "vehicle" else "无障碍功能停用"
    log_event(
        conn, type_,
        f"{vehicle['fleet_no']} 报告故障【{label}】，车辆进入{sev_label}",
        {"fault_id": fault_id, "vehicle_id": vehicle_id, "shift_id": shift_id},
    )
    return fault_id


def report_fault(db: Database, *, vehicle_id: int, type_: str, severity: str = "access",
                 description: str = "", reported_by: int, shift_id=None) -> dict:
    if type_ not in FAULT_TYPES:
        raise DomainError(f"未知故障类型：{type_}")
    if severity not in ("access", "vehicle"):
        raise DomainError("故障级别须为 access（无障碍功能停用）或 vehicle（整车待修）")
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        get_staff(conn, reported_by)
        _one(conn, "SELECT id FROM vehicles WHERE id=?", (vehicle_id,), "车辆不存在")
        fault_id = _create_fault(
            conn, vehicle_id=vehicle_id, shift_id=shift_id, inspection_id=None,
            type_=type_, severity=severity, description=description, reported_by=reported_by,
        )
        _refresh_vehicle_status(conn, vehicle_id)
        conn.commit()
        return {"fault_id": fault_id}
    finally:
        conn.close()


# ---------------------------------------------------------------- 点检 / 复检

def submit_inspection(db: Database, *, vehicle_id: int, inspector_id: int, items: dict,
                      shift_id=None, notes: str = "", photos: list | None = None) -> dict:
    """提交点检/复检。items: {appearance, deploy, locking, button} -> bool。

    - 仅驾驶员可执行（维修人员不能批准复检，调度员也不能代检）
    - 同一车辆同一班次只能有一份有效点检（唯一索引兜底并发）
    - 不合格项自动生成对应故障与维修单；复检不合格则故障重开
    - 复检合格后闭环已修复故障并恢复车辆状态
    """
    for key in ITEM_LABELS:
        if key not in items:
            raise DomainError(f"缺少点检项：{ITEM_LABELS[key]}")
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        inspector = get_staff(conn, inspector_id)
        require_role(inspector, "driver")
        vehicle = _one(conn, "SELECT * FROM vehicles WHERE id=?", (vehicle_id,), "车辆不存在")

        shift = None
        if shift_id is not None:
            shift = _one(conn, "SELECT * FROM shifts WHERE id=?", (shift_id,), "班次不存在")
            if shift["status"] != "pending":
                raise DomainError("班次不是待发状态，不能点检")
            if shift["vehicle_id"] != vehicle_id:
                raise DomainError("该车不是本班次的当前车辆（可能已换车）")
            dup = conn.execute(
                "SELECT id FROM inspections WHERE shift_id=? AND vehicle_id=? AND valid=1",
                (shift_id, vehicle_id),
            ).fetchone()
            if dup:
                raise DomainError("同一车辆同一班次只能有一份有效点检")

        if shift_id is not None:
            history = conn.execute(
                "SELECT COUNT(*) c FROM inspections WHERE vehicle_id=? AND shift_id=?",
                (vehicle_id, shift_id),
            ).fetchone()["c"]
        else:
            # 车辆级检查：该车有任何历史点检即为复检
            history = conn.execute(
                "SELECT COUNT(*) c FROM inspections WHERE vehicle_id=?",
                (vehicle_id,),
            ).fetchone()["c"]
        kind = "reinspection" if history else "initial"
        result = "pass" if all(bool(items[k]) for k in ITEM_LABELS) else "fail"

        try:
            cur = conn.execute(
                "INSERT INTO inspections (shift_id, vehicle_id, inspector_id, kind,"
                " item_appearance, item_deploy, item_locking, item_button, notes, result,"
                " valid, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,1,?)",
                (shift_id, vehicle_id, inspector_id, kind,
                 int(bool(items["appearance"])), int(bool(items["deploy"])),
                 int(bool(items["locking"])), int(bool(items["button"])),
                 notes, result, now()),
            )
        except sqlite3.IntegrityError:
            raise DomainError("同一车辆同一班次只能有一份有效点检")
        insp_id = cur.lastrowid

        for p in (photos or []):
            conn.execute(
                "INSERT INTO inspection_photos (inspection_id, item, path) VALUES (?,?,?)",
                (insp_id, p.get("item", ""), p["path"]),
            )

        if result == "fail":
            for item_key, ok in items.items():
                if ok or item_key not in ITEM_TO_FAULT:
                    continue
                ftype = ITEM_TO_FAULT[item_key]
                existing = conn.execute(
                    "SELECT * FROM faults WHERE vehicle_id=? AND type=?"
                    " AND status IN ('open','repaired') ORDER BY id DESC LIMIT 1",
                    (vehicle_id, ftype),
                ).fetchone()
                if existing and existing["status"] == "repaired":
                    # 复检失败：故障重开并重新派单
                    conn.execute("UPDATE faults SET status='open', repaired_at=NULL WHERE id=?",
                                 (existing["id"],))
                    _create_repair(conn, existing["id"], vehicle_id)
                    log_event(
                        conn, "reinspection_failed",
                        f"{vehicle['fleet_no']} 复检不合格：{ITEM_LABELS[item_key]}仍不合格，"
                        f"故障【{FAULT_TYPES[ftype]}】重开",
                        {"fault_id": existing["id"], "vehicle_id": vehicle_id,
                         "inspection_id": insp_id},
                    )
                elif not existing:
                    _create_fault(
                        conn, vehicle_id=vehicle_id, shift_id=shift_id, inspection_id=insp_id,
                        type_=ftype, severity="access",
                        description=f"点检不合格项：{ITEM_LABELS[item_key]}",
                        reported_by=inspector_id,
                    )
        else:
            # 合格：闭环所有"已修复待复检"的故障
            repaired = conn.execute(
                "SELECT id, type FROM faults WHERE vehicle_id=? AND status='repaired'",
                (vehicle_id,),
            ).fetchall()
            for f in repaired:
                conn.execute("UPDATE faults SET status='closed', closed_at=? WHERE id=?",
                             (now(), f["id"]))
                log_event(
                    conn, "fault_closed",
                    f"{vehicle['fleet_no']} 复检合格，故障【{FAULT_TYPES.get(f['type'], f['type'])}】闭环",
                    {"fault_id": f["id"], "vehicle_id": vehicle_id, "inspection_id": insp_id},
                )

        _refresh_vehicle_status(conn, vehicle_id)
        log_event(
            conn, "inspection_passed" if result == "pass" else "inspection_failed",
            f"{vehicle['fleet_no']} {'复检' if kind == 'reinspection' else '点检'}"
            f"{'合格' if result == 'pass' else '不合格'}（{inspector['name']}）",
            {"inspection_id": insp_id, "vehicle_id": vehicle_id, "shift_id": shift_id,
             "kind": kind, "result": result},
        )
        conn.commit()
        return {"id": insp_id, "kind": kind, "result": result}
    finally:
        conn.close()


# ---------------------------------------------------------------- 维修

def complete_repair(db: Database, *, repair_id: int, actor_id: int) -> dict:
    """维修完成：仅维修人员可完工；完工后故障转为"已修复待复检"，
    关联的不合格点检作废，车辆须重新点检合格才能恢复。"""
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        actor = get_staff(conn, actor_id)
        require_role(actor, "mechanic")
        repair = _one(conn, "SELECT * FROM repairs WHERE id=?", (repair_id,), "维修单不存在")
        if repair["status"] != "pending":
            raise DomainError("维修单已完工")
        fault = _one(conn, "SELECT * FROM faults WHERE id=?", (repair["fault_id"],))
        conn.execute(
            "UPDATE repairs SET status='done', assignee_id=?, completed_at=? WHERE id=?",
            (actor_id, now(), repair_id),
        )
        conn.execute("UPDATE faults SET status='repaired', repaired_at=? WHERE id=?",
                     (now(), fault["id"]))
        if fault["inspection_id"]:
            conn.execute("UPDATE inspections SET valid=0 WHERE id=?", (fault["inspection_id"],))
        _refresh_vehicle_status(conn, fault["vehicle_id"])
        vehicle = _one(conn, "SELECT fleet_no FROM vehicles WHERE id=?", (fault["vehicle_id"],))
        log_event(
            conn, "repair_completed",
            f"{vehicle['fleet_no']} 故障【{FAULT_TYPES.get(fault['type'], fault['type'])}】"
            f"维修完成（{actor['name']}），待复检",
            {"repair_id": repair_id, "fault_id": fault["id"], "vehicle_id": fault["vehicle_id"]},
        )
        conn.commit()
        return {"repair_id": repair_id, "fault_id": fault["id"]}
    finally:
        conn.close()


# ---------------------------------------------------------------- 换车

def swap_vehicle(db: Database, *, shift_id: int, to_vehicle_id: int, actor_id: int,
                 reason: str = "") -> dict:
    """调度员临时换车：校验备用车状态、无障碍要求与载客容量。"""
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        actor = get_staff(conn, actor_id)
        require_role(actor, "dispatcher")
        shift = _one(conn, "SELECT * FROM shifts WHERE id=?", (shift_id,), "班次不存在")
        if shift["status"] != "pending":
            raise DomainError("班次不是待发状态，不能换车")
        if shift["vehicle_id"] == to_vehicle_id:
            raise DomainError("不能换为同一车辆")
        route = _one(conn, "SELECT * FROM routes WHERE id=?", (shift["route_id"],))
        target = _one(conn, "SELECT * FROM vehicles WHERE id=?", (to_vehicle_id,), "车辆不存在")
        if target["status"] != "active":
            raise DomainError(
                f"备用车 {target['fleet_no']} 当前为"
                f"【{VEHICLE_STATUS_LABELS[target['status']]}】，不可用")
        if route["requires_accessible"] and not target["accessible"]:
            raise DomainError(f"线路 {route['code']} 要求无障碍车辆，{target['fleet_no']} 不满足")
        if target["capacity"] < route["min_capacity"]:
            raise DomainError(
                f"备用车容量不足：{target['fleet_no']} 核载 {target['capacity']} 人，"
                f"线路 {route['code']} 要求 ≥{route['min_capacity']} 人")
        conflict = conn.execute(
            "SELECT id FROM shifts WHERE vehicle_id=? AND service_date=? AND status='pending'"
            " AND id<>?",
            (to_vehicle_id, shift["service_date"], shift_id),
        ).fetchone()
        if conflict:
            raise DomainError(f"备用车 {target['fleet_no']} 当日已被其他待发班次占用")
        conn.execute(
            "INSERT INTO swaps (shift_id, from_vehicle_id, to_vehicle_id, dispatcher_id,"
            " reason, created_at) VALUES (?,?,?,?,?,?)",
            (shift_id, shift["vehicle_id"], to_vehicle_id, actor_id, reason, now()),
        )
        conn.execute("UPDATE shifts SET vehicle_id=? WHERE id=?", (to_vehicle_id, shift_id))
        old = _one(conn, "SELECT fleet_no FROM vehicles WHERE id=?", (shift["vehicle_id"],))
        log_event(
            conn, "vehicle_swap",
            f"班次#{shift_id} 临时换车：{old['fleet_no']} → {target['fleet_no']}"
            f"（{actor['name']}：{reason or '未填写原因'}）",
            {"shift_id": shift_id, "from": shift["vehicle_id"], "to": to_vehicle_id},
        )
        conn.commit()
        return {"shift_id": shift_id, "vehicle_id": to_vehicle_id}
    finally:
        conn.close()


# ---------------------------------------------------------------- 交接

def handover_driver(db: Database, *, shift_id: int, to_driver_id: int, actor_id: int) -> dict:
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        actor = get_staff(conn, actor_id)
        shift = _one(conn, "SELECT * FROM shifts WHERE id=?", (shift_id,), "班次不存在")
        if shift["status"] != "pending":
            raise DomainError("班次不是待发状态，不能交接")
        if actor["role"] != "dispatcher" and actor["id"] != shift["driver_id"]:
            raise Forbidden("职责分离：仅调度员或当班驾驶员可办理交接")
        to_driver = get_staff(conn, to_driver_id)
        require_role(to_driver, "driver")
        from_driver = get_staff(conn, shift["driver_id"])
        conn.execute(
            "INSERT INTO handovers (shift_id, from_driver_id, to_driver_id, created_at)"
            " VALUES (?,?,?,?)",
            (shift_id, shift["driver_id"], to_driver_id, now()),
        )
        conn.execute("UPDATE shifts SET driver_id=? WHERE id=?", (to_driver_id, shift_id))
        log_event(
            conn, "driver_handover",
            f"班次#{shift_id} 驾驶员交接：{from_driver['name']} → {to_driver['name']}",
            {"shift_id": shift_id, "from": shift["driver_id"], "to": to_driver_id},
        )
        conn.commit()
        return {"shift_id": shift_id, "driver_id": to_driver_id}
    finally:
        conn.close()


# ---------------------------------------------------------------- 放行

def release_shift(db: Database, *, shift_id: int, actor_id: int, idempotency_key: str) -> dict:
    """出场放行。仅调度员；全部点检合格且无未闭环故障才放行。

    幂等：releases.shift_id 唯一 + idempotency_key。并发/重复提交：
    - 同 key 重试 -> 返回原放行记录（不重复放行）
    - 不同 key    -> 拒绝并留痕
    误放行尝试一律拦截并留痕（erroneous_release_blocked）。
    """
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        actor = get_staff(conn, actor_id)
        require_role(actor, "dispatcher")

        existing = conn.execute("SELECT * FROM releases WHERE shift_id=?", (shift_id,)).fetchone()
        if existing:
            if existing["idempotency_key"] == idempotency_key:
                conn.commit()
                return {"release_id": existing["id"], "idempotent": True}
            log_event(conn, "release_duplicate_blocked",
                      f"班次#{shift_id} 重复放行被拦截（已放行，记录#{existing['id']}）",
                      {"shift_id": shift_id, "release_id": existing["id"]})
            conn.commit()
            raise DomainError(f"该班次已放行（放行记录#{existing['id']}），重复放行被拒绝")

        shift = _one(conn, "SELECT * FROM shifts WHERE id=?", (shift_id,), "班次不存在")

        def block(reason: str):
            log_event(conn, "erroneous_release_blocked",
                      f"误放行拦截：班次#{shift_id} {reason}",
                      {"shift_id": shift_id, "reason": reason})
            conn.commit()
            raise DomainError(reason)

        if shift["status"] != "pending":
            block("班次状态不是待发")
        vehicle = _one(conn, "SELECT * FROM vehicles WHERE id=?", (shift["vehicle_id"],))
        if vehicle["status"] != "active":
            block(f"车辆 {vehicle['fleet_no']} 处于"
                  f"【{VEHICLE_STATUS_LABELS[vehicle['status']]}】状态，禁止放行")
        open_faults = conn.execute(
            "SELECT COUNT(*) c FROM faults WHERE vehicle_id=? AND status IN ('open','repaired')",
            (vehicle["id"],),
        ).fetchone()["c"]
        if open_faults:
            block(f"车辆 {vehicle['fleet_no']} 存在 {open_faults} 条未闭环故障，禁止放行")
        insp = conn.execute(
            "SELECT * FROM inspections WHERE shift_id=? AND vehicle_id=? AND valid=1",
            (shift_id, vehicle["id"]),
        ).fetchone()
        if not insp:
            block(f"车辆 {vehicle['fleet_no']} 本班次缺少有效点检，禁止放行")
        if insp["result"] != "pass":
            block(f"车辆 {vehicle['fleet_no']} 点检不合格，禁止放行")

        try:
            cur = conn.execute(
                "INSERT INTO releases (shift_id, vehicle_id, released_by, idempotency_key,"
                " created_at) VALUES (?,?,?,?,?)",
                (shift_id, vehicle["id"], actor_id, idempotency_key, now()),
            )
        except sqlite3.IntegrityError:
            # 并发下另一事务已放行
            other = conn.execute("SELECT * FROM releases WHERE shift_id=?", (shift_id,)).fetchone()
            if other and other["idempotency_key"] == idempotency_key:
                conn.commit()
                return {"release_id": other["id"], "idempotent": True}
            log_event(conn, "release_duplicate_blocked",
                      f"班次#{shift_id} 并发放行被拦截，仅一单生效",
                      {"shift_id": shift_id})
            conn.commit()
            raise DomainError("该班次已被并发放行，本次放行被拒绝")
        conn.execute("UPDATE shifts SET status='released' WHERE id=?", (shift_id,))
        log_event(conn, "release_granted",
                  f"班次#{shift_id} 车辆 {vehicle['fleet_no']} 放行出场（{actor['name']}）",
                  {"shift_id": shift_id, "vehicle_id": vehicle["id"]})
        conn.commit()
        return {"release_id": cur.lastrowid, "idempotent": False}
    finally:
        conn.close()


# ---------------------------------------------------------------- 坡板测试

def run_ramp_test(db: Database, *, vehicle_id: int, actor_id: int, signals: dict) -> dict:
    """记录一次展开/收回测试，信号来自模拟车载终端适配器。"""
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        actor = get_staff(conn, actor_id)
        vehicle = _one(conn, "SELECT * FROM vehicles WHERE id=?", (vehicle_id,), "车辆不存在")
        ok = signals.get("ramp_position") in ("stowed", "deployed") \
            and signals.get("call_button") == "ok"
        result = "pass" if ok else "fail"
        cur = conn.execute(
            "INSERT INTO ramp_tests (vehicle_id, actor_id, action, result, signals, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (vehicle_id, actor_id, "deploy_retract", result,
             json.dumps(signals, ensure_ascii=False), now()),
        )
        log_event(
            conn, "ramp_tested",
            f"{vehicle['fleet_no']} 坡板展开/收回测试{'合格' if ok else '不合格'}"
            f"（{actor['name']}）",
            {"vehicle_id": vehicle_id, "result": result, "test_id": cur.lastrowid},
        )
        conn.commit()
        return {"id": cur.lastrowid, "result": result}
    finally:
        conn.close()
