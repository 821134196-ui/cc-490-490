"""SQLite 数据访问 + 全部业务规则。

规则要点：
* 同一车辆同一班次只能有一份有效(active=1)点检 —— 部分唯一索引保证；
* 外观/展开收回/锁止/呼叫按钮(及固定销、承重标识)全部合格才放行；
* 故障 → 无障碍功能停用或整车待修；线路要求无障碍时必须换备用车；
* 维修完成须由驾驶员复检，维修人员不能批准复检；
* 放行按 trip 幂等，并发只有一单成功，门控只开一次。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta

# ---------- 枚举 ----------
ROLE_DRIVER = "driver"
ROLE_DISPATCHER = "dispatcher"
ROLE_MECHANIC = "mechanic"
ROLE_LABELS = {ROLE_DRIVER: "驾驶员", ROLE_DISPATCHER: "调度员", ROLE_MECHANIC: "维修人员"}

VEH_IN_SERVICE = "IN_SERVICE"        # 正常运营
VEH_RAMP_DISABLED = "RAMP_DISABLED"  # 无障碍功能停用
VEH_UNDER_REPAIR = "UNDER_REPAIR"    # 整车待修
VEH_RESERVED = "RESERVED"            # 备用待用
VEH_STATUS_LABELS = {
    VEH_IN_SERVICE: "正常", VEH_RAMP_DISABLED: "无障碍停用",
    VEH_UNDER_REPAIR: "整车待修", VEH_RESERVED: "备用待命",
}

TRIP_PLANNED = "PLANNED"
TRIP_RELEASED = "RELEASED"
TRIP_DEPARTED = "DEPARTED"

# 故障类型（对应八类留痕中的设备/管理故障）
F_RAMP_JAM = "RAMP_JAM"                 # 坡板卡滞
F_PIN_MISSING = "PIN_MISSING"           # 固定销缺失
F_BUTTON_DEAD = "BUTTON_DEAD"           # 呼叫按钮无响应
F_LOAD_LABEL_LOST = "LOAD_LABEL_LOST"   # 承重标识脱落
F_DRIVER_MISSED = "DRIVER_MISSED_CHECK" # 司机漏检
F_REPAIR_DELAY = "REPAIR_DELAY"         # 维修延期
F_MISRELEASE = "MISRELEASE"             # 误放行
FAULT_LABELS = {
    F_RAMP_JAM: "坡板卡滞", F_PIN_MISSING: "固定销缺失",
    F_BUTTON_DEAD: "呼叫按钮无响应", F_LOAD_LABEL_LOST: "承重标识脱落",
    F_DRIVER_MISSED: "司机漏检", F_REPAIR_DELAY: "维修延期",
    F_MISRELEASE: "误放行",
}

CONSEQUENCE_RAMP = "RAMP_DISABLED"   # 无障碍功能停用
CONSEQUENCE_FULL = "VEHICLE_DOWN"    # 整车待修

REPAIR_OPEN = "OPEN"
REPAIR_IN_PROGRESS = "IN_PROGRESS"
REPAIR_DELAYED = "DELAYED"
REPAIR_DONE = "DONE"
REPAIR_REINSPECTED = "REINSPECTED"

RAMP_TYPES = {"MANUAL_FOLD": "手动折叠坡板", "ELECTRIC": "电动坡板", "SEMI_AUTO": "半自动坡板"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS staff(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lines(
  id TEXT PRIMARY KEY, code TEXT NOT NULL, name TEXT NOT NULL,
  accessible_required INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS vehicles(
  id TEXT PRIMARY KEY, fleet_no TEXT NOT NULL, plate TEXT NOT NULL,
  ramp_type TEXT NOT NULL, load_rating_kg INTEGER NOT NULL,
  accessible INTEGER NOT NULL DEFAULT 1, is_spare INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'IN_SERVICE', note TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS shifts(
  id TEXT PRIMARY KEY, label TEXT NOT NULL, work_date TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS trips(
  id TEXT PRIMARY KEY, shift_id TEXT NOT NULL REFERENCES shifts(id),
  line_id TEXT NOT NULL REFERENCES lines(id),
  vehicle_id TEXT NOT NULL REFERENCES vehicles(id),
  driver_id TEXT NOT NULL REFERENCES staff(id),
  seq INTEGER NOT NULL, departure_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'PLANNED');
CREATE INDEX IF NOT EXISTS idx_trips_shift ON trips(shift_id);
CREATE TABLE IF NOT EXISTS inspections(
  id TEXT PRIMARY KEY, shift_id TEXT NOT NULL, vehicle_id TEXT NOT NULL,
  driver_id TEXT NOT NULL, trip_id TEXT,
  round INTEGER NOT NULL DEFAULT 1, repair_id TEXT,
  result TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
  appearance INTEGER NOT NULL, deploy_retract INTEGER NOT NULL,
  locking INTEGER NOT NULL, call_button INTEGER NOT NULL,
  pin_present INTEGER NOT NULL, load_label INTEGER NOT NULL,
  note TEXT DEFAULT '', photos TEXT DEFAULT '[]',
  created_at TEXT NOT NULL, finalized_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_insp_veh_shift ON inspections(shift_id, vehicle_id);
-- 同一车辆同一班次至多一份有效点检
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_inspection
  ON inspections(shift_id, vehicle_id) WHERE active = 1;
CREATE TABLE IF NOT EXISTS ramp_tests(
  id TEXT PRIMARY KEY, inspection_id TEXT NOT NULL REFERENCES inspections(id),
  deploy_seconds REAL, retract_seconds REAL, cycles INTEGER NOT NULL DEFAULT 1,
  result TEXT NOT NULL, detail TEXT DEFAULT '', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS faults(
  id TEXT PRIMARY KEY, vehicle_id TEXT, trip_id TEXT, inspection_id TEXT,
  fault_type TEXT NOT NULL, consequence TEXT NOT NULL DEFAULT 'NONE',
  description TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'OPEN',
  reported_by TEXT, repair_id TEXT, created_at TEXT NOT NULL, resolved_at TEXT);
CREATE TABLE IF NOT EXISTS repairs(
  id TEXT PRIMARY KEY, fault_id TEXT NOT NULL REFERENCES faults(id),
  vehicle_id TEXT NOT NULL, mechanic_id TEXT,
  status TEXT NOT NULL DEFAULT 'OPEN', due_at TEXT,
  started_at TEXT, completed_at TEXT, summary TEXT DEFAULT '',
  bench_test TEXT DEFAULT '', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS swaps(
  id TEXT PRIMARY KEY, trip_id TEXT NOT NULL, shift_id TEXT NOT NULL,
  from_vehicle_id TEXT NOT NULL, to_vehicle_id TEXT NOT NULL,
  dispatcher_id TEXT NOT NULL, reason TEXT DEFAULT '', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS releases(
  id TEXT PRIMARY KEY, trip_id TEXT NOT NULL UNIQUE,
  shift_id TEXT NOT NULL, vehicle_id TEXT NOT NULL, inspection_id TEXT,
  released_by TEXT NOT NULL, gate_signal TEXT DEFAULT '', terminal_signal TEXT DEFAULT '',
  flagged INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS handovers(
  id TEXT PRIMARY KEY, trip_id TEXT NOT NULL, driver_id TEXT NOT NULL,
  shift_note TEXT DEFAULT '', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, kind TEXT NOT NULL,
  vehicle_id TEXT, trip_id TEXT, actor_id TEXT, actor_role TEXT, detail TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_events_trip ON events(trip_id);
"""

CHECK_ITEMS = ["appearance", "deploy_retract", "locking",
               "call_button", "pin_present", "load_label"]
ITEM_LABELS = {
    "appearance": "坡板外观", "deploy_retract": "展开/收回",
    "locking": "锁止", "call_button": "车内呼叫按钮",
    "pin_present": "固定销", "load_label": "承重标识",
}
# 出场放行硬性四项（题目指定），固定销与承重标识同样参与合格判定
GATE_ITEMS = ["appearance", "deploy_retract", "locking", "call_button"]


def now() -> datetime:
    return datetime.now()


def ts(dt: datetime | None = None) -> str:
    return (dt or now()).isoformat(timespec="seconds")


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


class BizError(Exception):
    """业务规则错误，HTTP 层映射为 4xx。"""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


class Store:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA foreign_keys=ON;")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._seq = 0

    def reset(self):
        with self.lock:
            cur = self.conn.cursor()
            for t in ("events", "releases", "swaps", "repairs", "faults",
                      "ramp_tests", "inspections", "handovers", "trips",
                      "shifts", "vehicles", "lines", "staff"):
                cur.execute(f"DELETE FROM {t}")
            self.conn.commit()

    # ---------- 基础 ----------
    def _id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:04d}"

    def event(self, kind: str, *, vehicle_id=None, trip_id=None,
              actor_id=None, actor_role=None, detail=None):
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO events(at,kind,vehicle_id,trip_id,actor_id,actor_role,detail)"
                " VALUES(?,?,?,?,?,?,?)",
                (ts(), kind, vehicle_id, trip_id, actor_id, actor_role,
                 json.dumps(detail, ensure_ascii=False) if detail is not None else ""))
            self.conn.commit()
            return cur.lastrowid

    def _staff(self, staff_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM staff WHERE id=?", (staff_id,)).fetchone()
        if not row:
            raise BizError("UNKNOWN_STAFF", f"人员 {staff_id} 不存在", 404)
        return row

    def _require_role(self, staff_id: str, role: str):
        row = self._staff(staff_id)
        if row["role"] != role:
            raise BizError(
                "FORBIDDEN_ROLE",
                f"{ROLE_LABELS.get(row['role'], row['role'])}不能执行"
                f"仅{ROLE_LABELS[role]}可执行的操作", 403)
        return row

    # ---------- 查询 ----------
    def active_shift(self):
        return self.conn.execute(
            "SELECT * FROM shifts WHERE active=1 ORDER BY work_date DESC, id LIMIT 1"
        ).fetchone()

    def active_inspection(self, shift_id: str, vehicle_id: str):
        return self.conn.execute(
            "SELECT * FROM inspections WHERE shift_id=? AND vehicle_id=? AND active=1",
            (shift_id, vehicle_id)).fetchone()

    def _trip_view_where(self, where: str, args=()):
        return self.conn.execute(f"""
            SELECT t.*, l.code line_code, l.name line_name, l.accessible_required,
                   v.fleet_no, v.plate, v.ramp_type, v.status vehicle_status, v.is_spare,
                   s.name driver_name
            FROM trips t
            JOIN lines l ON l.id=t.line_id
            JOIN vehicles v ON v.id=t.vehicle_id
            JOIN staff s ON s.id=t.driver_id
            {where} ORDER BY t.shift_id, t.departure_at, t.seq
        """, args).fetchall()

    # ---------- 点检 ----------
    def submit_inspection(self, *, shift_id: str, vehicle_id: str, driver_id: str,
                          trip_id: str | None, items: dict, ramp_test: dict | None = None,
                          note: str = "", photos: list | None = None,
                          round_no: int = 1, repair_id: str | None = None,
                          allow_supersede: bool = False) -> dict:
        """驾驶员提交点检（初检/复检）。返回 {inspection_id, result}。"""
        with self.lock:
            staff = self._require_role(driver_id, ROLE_DRIVER)
            vehicle = self.conn.execute(
                "SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
            if not vehicle:
                raise BizError("UNKNOWN_VEHICLE", "车辆不存在", 404)
            shift = self.conn.execute(
                "SELECT * FROM shifts WHERE id=?", (shift_id,)).fetchone()
            if not shift:
                raise BizError("UNKNOWN_SHIFT", "班次不存在", 404)

            missing = [k for k in CHECK_ITEMS if k not in items]
            if missing:
                raise BizError("ITEMS_MISSING",
                               f"点检项缺失: {','.join(ITEM_LABELS[k] for k in missing)}")
            vals = {k: 1 if items[k] else 0 for k in CHECK_ITEMS}
            result = "PASS" if all(vals[k] for k in CHECK_ITEMS) else "FAIL"

            existing = self.active_inspection(shift_id, vehicle_id)
            if existing is not None:
                # 复检：必须有维修单，且旧点检为 FAIL，才允许作废旧单
                if not (allow_supersede and repair_id and existing["result"] == "FAIL"):
                    raise BizError(
                        "DUPLICATE_INSPECTION",
                        f"车辆 {vehicle['fleet_no']} 本班次已存在一份有效点检"
                        f"（{'合格' if existing['result'] == 'PASS' else '不合格'}），"
                        f"同一车辆同一班次只能有一份有效点检", 409)

            cur = self.conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                if existing is not None:
                    cur.execute("UPDATE inspections SET active=0 WHERE id=?",
                                (existing["id"],))
                iid = self._id("INS")
                cur.execute("""
                    INSERT INTO inspections(id,shift_id,vehicle_id,driver_id,trip_id,
                        round,repair_id,result,active,appearance,deploy_retract,locking,
                        call_button,pin_present,load_label,note,photos,created_at,finalized_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (iid, shift_id, vehicle_id, driver_id, trip_id, round_no, repair_id,
                     result, 1, vals["appearance"], vals["deploy_retract"], vals["locking"],
                     vals["call_button"], vals["pin_present"], vals["load_label"],
                     note, json.dumps(photos or [], ensure_ascii=False), ts(), ts()))
                tid = None
                if ramp_test:
                    tid = self._id("TST")
                    cur.execute("""
                        INSERT INTO ramp_tests(id,inspection_id,deploy_seconds,retract_seconds,
                            cycles,result,detail,created_at)
                        VALUES(?,?,?,?,?,?,?,?)""",
                        (tid, iid, ramp_test.get("deploy_seconds"),
                         ramp_test.get("retract_seconds"), ramp_test.get("cycles", 1),
                         ramp_test.get("result", "PASS" if vals["deploy_retract"] else "FAIL"),
                         ramp_test.get("detail", ""), ts()))
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise

            kind = "REINSPECTION_PASS" if round_no > 1 and result == "PASS" else (
                "REINSPECTION_FAIL" if round_no > 1 else
                ("INSPECTION_PASS" if result == "PASS" else "INSPECTION_FAIL"))
            self.event(kind, vehicle_id=vehicle_id, trip_id=trip_id,
                       actor_id=driver_id, actor_role=staff["role"],
                       detail={"result": result, "round": round_no,
                               "items": vals, "test": ramp_test, "note": note})
            return {"inspection_id": iid, "result": result,
                    "superseded": existing["id"] if existing else None}

    # ---------- 故障与停用 ----------
    def report_fault(self, *, vehicle_id: str, fault_type: str, reported_by: str,
                     consequence: str = CONSEQUENCE_RAMP, description: str = "",
                     trip_id: str | None = None, inspection_id: str | None = None,
                     auto_status: bool = True) -> dict:
        with self.lock:
            staff = self._staff(reported_by)
            if fault_type not in FAULT_LABELS:
                raise BizError("UNKNOWN_FAULT_TYPE", "未知故障类型")
            vehicle = self.conn.execute(
                "SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
            if not vehicle:
                raise BizError("UNKNOWN_VEHICLE", "车辆不存在", 404)
            fid = self._id("FLT")
            self.conn.execute("""
                INSERT INTO faults(id,vehicle_id,trip_id,inspection_id,fault_type,
                    consequence,description,status,reported_by,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (fid, vehicle_id, trip_id, inspection_id, fault_type, consequence,
                 description, "OPEN", reported_by, ts()))
            new_status = None
            if auto_status and consequence in (CONSEQUENCE_RAMP, CONSEQUENCE_FULL):
                new_status = (VEH_RAMP_DISABLED if consequence == CONSEQUENCE_RAMP
                              else VEH_UNDER_REPAIR)
                self.conn.execute("UPDATE vehicles SET status=? WHERE id=?",
                                  (new_status, vehicle_id))
            self.conn.commit()
            self.event("FAULT_REPORT", vehicle_id=vehicle_id, trip_id=trip_id,
                       actor_id=reported_by, actor_role=staff["role"],
                       detail={"fault_type": fault_type,
                               "fault_label": FAULT_LABELS[fault_type],
                               "consequence": consequence,
                               "vehicle_status": new_status, "description": description})
            if new_status == VEH_RAMP_DISABLED:
                self.event("VEHICLE_RAMP_DISABLED", vehicle_id=vehicle_id, trip_id=trip_id,
                           actor_id=reported_by, actor_role=staff["role"],
                           detail={"reason": FAULT_LABELS[fault_type]})
            elif new_status == VEH_UNDER_REPAIR:
                self.event("VEHICLE_DOWN", vehicle_id=vehicle_id, trip_id=trip_id,
                           actor_id=reported_by, actor_role=staff["role"],
                           detail={"reason": FAULT_LABELS[fault_type]})
            return {"fault_id": fid, "vehicle_status": new_status}

    # ---------- 备用车调配 ----------
    def available_spares(self, shift_id: str) -> list:
        """本班次可调配的无障碍备用车。"""
        used = {r["vehicle_id"] for r in self.conn.execute(
            "SELECT vehicle_id FROM trips WHERE shift_id=? AND status!='DEPARTED'",
            (shift_id,)).fetchall()}
        rows = self.conn.execute(
            "SELECT * FROM vehicles WHERE is_spare=1 AND accessible=1 "
            "AND status IN ('IN_SERVICE','RESERVED') ORDER BY fleet_no").fetchall()
        return [dict(r, assigned=r["id"] in used) for r in rows
                if r["id"] not in used]

    def swap_vehicle(self, *, trip_id: str, dispatcher_id: str,
                     spare_vehicle_id: str | None = None, reason: str = "") -> dict:
        """调度员临时换车：线路要求无障碍且无可用备用车时拒绝（容量约束）。"""
        with self.lock:
            self._require_role(dispatcher_id, ROLE_DISPATCHER)
            trip = self.conn.execute("SELECT * FROM trips WHERE id=?", (trip_id,)).fetchone()
            if not trip:
                raise BizError("UNKNOWN_TRIP", "班次任务不存在", 404)
            line = self.conn.execute("SELECT * FROM lines WHERE id=?",
                                     (trip["line_id"],)).fetchone()
            old_v = self.conn.execute("SELECT * FROM vehicles WHERE id=?",
                                      (trip["vehicle_id"],)).fetchone()

            if not line["accessible_required"]:
                # 非无障碍线路可不换车，但调用换车接口仍按申请处理
                pass

            spares = self.available_spares(trip["shift_id"])
            if spare_vehicle_id is None:
                if not spares:
                    raise BizError("SPARE_NONE_AVAILABLE",
                                   "已无可用无障碍备用车，备用容量不足，无法换车", 409)
                spare_vehicle_id = spares[0]["id"]
            else:
                if not any(s["id"] == spare_vehicle_id for s in spares):
                    raise BizError("SPARE_NOT_AVAILABLE",
                                   "指定车辆不是可用备用车（已占用/在修/非无障碍）", 409)

            spare = self.conn.execute("SELECT * FROM vehicles WHERE id=?",
                                      (spare_vehicle_id,)).fetchone()
            cur = self.conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                cur.execute("UPDATE trips SET vehicle_id=?, status='PLANNED' WHERE id=?",
                            (spare_vehicle_id, trip_id))
                cur.execute("UPDATE vehicles SET status='IN_SERVICE' WHERE id=?",
                            (spare_vehicle_id,))
                sid = self._id("SWP")
                cur.execute("""INSERT INTO swaps(id,trip_id,shift_id,from_vehicle_id,
                    to_vehicle_id,dispatcher_id,reason,created_at)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (sid, trip_id, trip["shift_id"], trip["vehicle_id"],
                     spare_vehicle_id, dispatcher_id, reason or "临时换车", ts()))
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
            self.event("SPARE_ASSIGNED", vehicle_id=spare_vehicle_id, trip_id=trip_id,
                       actor_id=dispatcher_id, actor_role=ROLE_DISPATCHER,
                       detail={"from": old_v["fleet_no"], "to": spare["fleet_no"],
                               "reason": reason})
            self.event("SWAP", vehicle_id=spare_vehicle_id, trip_id=trip_id,
                       actor_id=dispatcher_id, actor_role=ROLE_DISPATCHER,
                       detail={"swap_id": sid, "from_vehicle": old_v["fleet_no"],
                               "to_vehicle": spare["fleet_no"], "reason": reason})
            remaining = self.available_spares(trip["shift_id"])
            return {"swap_id": sid, "spare_vehicle_id": spare_vehicle_id,
                    "spares_remaining": len(remaining)}

    # ---------- 维修 ----------
    def open_repair(self, *, fault_id: str, mechanic_id: str, due_minutes: int = 60) -> dict:
        with self.lock:
            self._require_role(mechanic_id, ROLE_MECHANIC)
            fault = self.conn.execute("SELECT * FROM faults WHERE id=?",
                                      (fault_id,)).fetchone()
            if not fault:
                raise BizError("UNKNOWN_FAULT", "故障单不存在", 404)
            if fault["repair_id"]:
                raise BizError("REPAIR_EXISTS", "该故障已有维修单", 409)
            rid = self._id("RPR")
            due = now() + timedelta(minutes=due_minutes)
            self.conn.execute("""INSERT INTO repairs(id,fault_id,vehicle_id,mechanic_id,
                status,due_at,created_at) VALUES(?,?,?,?,?,?,?)""",
                (rid, fault_id, fault["vehicle_id"], mechanic_id,
                 REPAIR_IN_PROGRESS, ts(due), ts()))
            self.conn.execute("UPDATE faults SET status='REPAIRING',repair_id=? WHERE id=?",
                              (rid, fault_id))
            self.conn.commit()
            self.event("REPAIR_START", vehicle_id=fault["vehicle_id"],
                       trip_id=fault["trip_id"], actor_id=mechanic_id,
                       actor_role=ROLE_MECHANIC,
                       detail={"fault_id": fault_id, "due_at": ts(due)})
            return {"repair_id": rid, "due_at": ts(due)}

    def complete_repair(self, *, repair_id: str, mechanic_id: str,
                        summary: str = "", bench_test: str = "PASS") -> dict:
        """维修完成：记录台架测试；车辆仍须驾驶员复检合格才恢复运营。"""
        with self.lock:
            self._require_role(mechanic_id, ROLE_MECHANIC)
            repair = self.conn.execute("SELECT * FROM repairs WHERE id=?",
                                       (repair_id,)).fetchone()
            if not repair:
                raise BizError("UNKNOWN_REPAIR", "维修单不存在", 404)
            if repair["status"] == REPAIR_DONE:
                raise BizError("REPAIR_ALREADY_DONE", "该维修单已完工", 409)
            self.conn.execute("""UPDATE repairs SET status='DONE',completed_at=?,
                summary=?,bench_test=? WHERE id=?""",
                              (ts(), summary, bench_test, repair_id))
            self.conn.commit()
            self.event("REPAIR_COMPLETE", vehicle_id=repair["vehicle_id"],
                       actor_id=mechanic_id, actor_role=ROLE_MECHANIC,
                       detail={"repair_id": repair_id, "bench_test": bench_test,
                               "summary": summary,
                               "note": "维修完工，等待驾驶员复检"})
            return {"repair_id": repair_id, "awaiting_reinspection": True}

    def delay_repair(self, *, repair_id: str, mechanic_id: str, reason: str) -> dict:
        with self.lock:
            self._require_role(mechanic_id, ROLE_MECHANIC)
            repair = self.conn.execute("SELECT * FROM repairs WHERE id=?",
                                       (repair_id,)).fetchone()
            if not repair:
                raise BizError("UNKNOWN_REPAIR", "维修单不存在", 404)
            new_due = now() + timedelta(minutes=120)
            self.conn.execute("UPDATE repairs SET status='DELAYED',due_at=? WHERE id=?",
                              (ts(new_due), repair_id))
            self.conn.commit()
            self.event("REPAIR_DELAY", vehicle_id=repair["vehicle_id"],
                       actor_id=mechanic_id, actor_role=ROLE_MECHANIC,
                       detail={"repair_id": repair_id, "reason": reason,
                               "new_due_at": ts(new_due)})
            # 维修延期留痕
            self.conn.execute("""INSERT INTO faults(id,vehicle_id,fault_type,consequence,
                description,status,reported_by,created_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (self._id("FLT"), repair["vehicle_id"], F_REPAIR_DELAY, "NONE",
                 f"维修延期：{reason}", "RESOLVED", mechanic_id, ts()))
            self.conn.commit()
            return {"repair_id": repair_id, "new_due_at": ts(new_due)}

    # ---------- 复检 ----------
    def reinspect(self, *, repair_id: str, driver_id: str, items: dict,
                  ramp_test: dict | None = None, note: str = "", photos: list | None = None):
        """复检只能由驾驶员执行；维修人员批准复检 → 403。"""
        with self.lock:
            repair = self.conn.execute("SELECT * FROM repairs WHERE id=?",
                                       (repair_id,)).fetchone()
            if not repair:
                raise BizError("UNKNOWN_REPAIR", "维修单不存在", 404)
            if repair["status"] != REPAIR_DONE:
                raise BizError("REPAIR_NOT_DONE",
                               "维修尚未完工，不能复检", 409)
            shift = self.active_shift()
            fault = self.conn.execute("SELECT * FROM faults WHERE id=?",
                                      (repair["fault_id"],)).fetchone()
            trip_id = fault["trip_id"] if fault else None
            out = self.submit_inspection(
                shift_id=shift["id"], vehicle_id=repair["vehicle_id"],
                driver_id=driver_id, trip_id=trip_id, items=items,
                ramp_test=ramp_test, note=note, photos=photos,
                round_no=2, repair_id=repair_id, allow_supersede=True)

            if out["result"] == "PASS":
                # 复检合格：恢复运营、故障关闭
                self.conn.execute(
                    "UPDATE vehicles SET status='IN_SERVICE' WHERE id=?",
                    (repair["vehicle_id"],))
                self.conn.execute(
                    "UPDATE faults SET status='RESOLVED',resolved_at=? WHERE id=?",
                    (ts(), repair["fault_id"]))
                self.conn.execute(
                    "UPDATE faults SET status='RESOLVED',resolved_at=? WHERE repair_id=?",
                    (ts(), repair_id))
                self.conn.execute(
                    "UPDATE repairs SET status='REINSPECTED' WHERE id=?",
                    (repair_id,))
                self.conn.commit()
                self.event("VEHICLE_BACK_IN_SERVICE", vehicle_id=repair["vehicle_id"],
                           actor_id=driver_id, actor_role=ROLE_DRIVER,
                           detail={"repair_id": repair_id})
            else:
                # 复检失败：重新开故障/维修流程，车辆维持停用
                self.conn.execute("UPDATE repairs SET status='DELAYED' WHERE id=?",
                                  (repair_id,))
                self.conn.commit()
                self.event("REINSPECTION_FAILED_HOLD",
                           vehicle_id=repair["vehicle_id"], actor_id=driver_id,
                           actor_role=ROLE_DRIVER,
                           detail={"repair_id": repair_id,
                                   "note": "复检不合格，车辆继续停用，需重新维修"})
            return out

    # ---------- 放行（幂等 + 并发安全） ----------
    def can_release(self, trip: sqlite3.Row) -> tuple[bool, str, sqlite3.Row | None]:
        insp = self.active_inspection(trip["shift_id"], trip["vehicle_id"])
        if insp is None:
            return False, "车辆本班次尚无有效点检", None
        if insp["result"] != "PASS":
            failed = [ITEM_LABELS[k] for k in CHECK_ITEMS if not insp[k]]
            return False, "点检不合格：" + "、".join(failed), insp
        bad = [k for k in GATE_ITEMS if not insp[k]]
        if bad:
            return False, "硬性项目未合格：" + "、".join(ITEM_LABELS[k] for k in bad), insp
        veh = self.conn.execute("SELECT status FROM vehicles WHERE id=?",
                                (trip["vehicle_id"],)).fetchone()
        if veh["status"] != VEH_IN_SERVICE:
            return False, f"车辆状态为{VEH_STATUS_LABELS.get(veh['status'])}，禁止出场", insp
        return True, "OK", insp

    def release(self, *, trip_id: str, dispatcher_id: str, gate_adapter,
                terminal_adapter) -> dict:
        with self.lock:
            staff = self._require_role(dispatcher_id, ROLE_DISPATCHER)
            trip = self.conn.execute("SELECT * FROM trips WHERE id=?",
                                     (trip_id,)).fetchone()
            if not trip:
                raise BizError("UNKNOWN_TRIP", "班次任务不存在", 404)

            existing = self.conn.execute(
                "SELECT * FROM releases WHERE trip_id=?", (trip_id,)).fetchone()
            if existing:
                # 幂等：返回同一放行单，门控不重复动作
                self.event("RELEASE_DUPLICATE", vehicle_id=existing["vehicle_id"],
                           trip_id=trip_id, actor_id=dispatcher_id,
                           actor_role=staff["role"],
                           detail={"release_id": existing["id"],
                                   "note": "重复放行请求被幂等拦截，门控未再次开启"})
                return {"release_id": existing["id"], "duplicate": True,
                        "gate_signal": existing["gate_signal"],
                        "terminal_signal": existing["terminal_signal"]}

            ok, reason, insp = self.can_release(trip)
            if not ok:
                self.event("RELEASE_BLOCKED", vehicle_id=trip["vehicle_id"],
                           trip_id=trip_id, actor_id=dispatcher_id,
                           actor_role=staff["role"], detail={"reason": reason})
                raise BizError("RELEASE_BLOCKED", f"放行被阻断：{reason}", 409)

            rid = self._id("REL")
            cur = self.conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                cur.execute("""INSERT INTO releases(id,trip_id,shift_id,vehicle_id,
                    inspection_id,released_by,created_at) VALUES(?,?,?,?,?,?,?)""",
                    (rid, trip_id, trip["shift_id"], trip["vehicle_id"],
                     insp["id"], dispatcher_id, ts()))
                cur.execute("UPDATE trips SET status='RELEASED' WHERE id=?", (trip_id,))
                cur.execute("COMMIT")
            except sqlite3.IntegrityError:
                cur.execute("ROLLBACK")
                row = self.conn.execute(
                    "SELECT * FROM releases WHERE trip_id=?", (trip_id,)).fetchone()
                self.event("RELEASE_DUPLICATE", vehicle_id=row["vehicle_id"],
                           trip_id=trip_id, actor_id=dispatcher_id,
                           actor_role=staff["role"],
                           detail={"release_id": row["id"], "note": "并发竞争落败"})
                return {"release_id": row["id"], "duplicate": True,
                        "gate_signal": row["gate_signal"],
                        "terminal_signal": row["terminal_signal"]}
            except Exception:
                cur.execute("ROLLBACK")
                raise

            # 模拟适配器：车辆终端 + 门控（只执行一次）
            gate_sig = gate_adapter.open_gate(trip, insp)
            term_sig = terminal_adapter.dispatch_notice(trip, insp)
            self.conn.execute(
                "UPDATE releases SET gate_signal=?,terminal_signal=? WHERE id=?",
                (gate_sig["signal"], term_sig["signal"], rid))
            self.conn.commit()
            self.event("GATE_SIGNAL", vehicle_id=trip["vehicle_id"], trip_id=trip_id,
                       actor_id=dispatcher_id, actor_role=staff["role"], detail=gate_sig)
            self.event("TERMINAL_SIGNAL", vehicle_id=trip["vehicle_id"], trip_id=trip_id,
                       actor_id=dispatcher_id, actor_role=staff["role"], detail=term_sig)
            self.event("RELEASE_GRANTED", vehicle_id=trip["vehicle_id"], trip_id=trip_id,
                       actor_id=dispatcher_id, actor_role=staff["role"],
                       detail={"release_id": rid, "gate": gate_sig["signal"],
                               "terminal": term_sig["signal"]})
            return {"release_id": rid, "duplicate": False,
                    "gate_signal": gate_sig["signal"],
                    "terminal_signal": term_sig["signal"]}

    # ---------- 驾驶员交接 ----------
    def handover(self, *, trip_id: str, driver_id: str, shift_note: str = ""):
        with self.lock:
            self._require_role(driver_id, ROLE_DRIVER)
            hid = self._id("HND")
            self.conn.execute(
                "INSERT INTO handovers(id,trip_id,driver_id,shift_note,created_at)"
                " VALUES(?,?,?,?,?)", (hid, trip_id, driver_id, shift_note, ts()))
            self.conn.commit()
            self.event("HANDOVER", trip_id=trip_id, actor_id=driver_id,
                       actor_role=ROLE_DRIVER, detail={"note": shift_note})
            return {"handover_id": hid}

    # ---------- 提醒（进程内任务） ----------
    def scan_reminders(self) -> list:
        """发车时限提醒 + 维修超时。返回当前活跃提醒列表。"""
        out = []
        t = now()
        for r in self._trip_view_where("WHERE t.status='PLANNED'"):
            dep = parse_ts(r["departure_at"])
            mins = (dep - t).total_seconds() / 60
            insp = self.active_inspection(r["shift_id"], r["vehicle_id"])
            if 0 <= mins <= 15:
                if insp is None:
                    out.append({"kind": "DEPARTURE_NO_CHECK", "trip_id": r["id"],
                                "minutes_left": round(mins, 1),
                                "message": f"{r['line_code']} {r['fleet_no']} "
                                           f"距发车 {mins:.0f} 分钟，尚未点检！"})
                elif insp["result"] != "PASS" or r["vehicle_status"] != VEH_IN_SERVICE:
                    out.append({"kind": "DEPARTURE_BLOCKED", "trip_id": r["id"],
                                "minutes_left": round(mins, 1),
                                "message": f"{r['line_code']} {r['fleet_no']} "
                                           f"距发车 {mins:.0f} 分钟，点检/车辆不合格，需换车"})
            if mins < 0 and insp is None and r["status"] == "PLANNED":
                out.append({"kind": "MISSED_CHECK", "trip_id": r["id"],
                            "minutes_late": round(-mins, 1),
                            "message": f"{r['line_code']} {r['fleet_no']} 已过发车时间仍无点检"})
        for r in self.conn.execute(
                "SELECT rp.*, f.fault_type FROM repairs rp JOIN faults f ON f.id=rp.fault_id "
                "WHERE rp.status IN ('OPEN','IN_PROGRESS','DELAYED')").fetchall():
            due = parse_ts(r["due_at"])
            mins = (due - t).total_seconds() / 60
            if mins <= 0:
                out.append({"kind": "REPAIR_OVERDUE", "repair_id": r["id"],
                            "minutes_overdue": round(-mins, 1),
                            "message": f"维修单 {r['id']} 已超时 {-mins:.0f} 分钟"})
            elif mins <= 10:
                out.append({"kind": "REPAIR_DUE_SOON", "repair_id": r["id"],
                            "minutes_left": round(mins, 1),
                            "message": f"维修单 {r['id']} 将于 {mins:.0f} 分钟后到期"})
        return out

    # ---------- 汇总给前端 ----------
    def board_payload(self) -> dict:
        def rows(sql, args=()):
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

        shift = self.active_shift()
        shift_id = shift["id"] if shift else None
        trips = [dict(r) for r in self._trip_view_where(
            "WHERE t.shift_id=?", (shift_id,))] if shift_id else []
        for t in trips:
            insp = self.active_inspection(shift_id, t["vehicle_id"])
            t["inspection"] = dict(insp) if insp else None
            rel = self.conn.execute("SELECT * FROM releases WHERE trip_id=?",
                                    (t["id"],)).fetchone()
            t["release"] = dict(rel) if rel else None

        vehicles = rows("SELECT * FROM vehicles ORDER BY is_spare DESC, fleet_no")
        inspections = rows(
            "SELECT * FROM inspections WHERE shift_id=? ORDER BY created_at DESC",
            (shift_id,))
        tests = rows("""SELECT rt.*, i.vehicle_id, i.round FROM ramp_tests rt
            JOIN inspections i ON i.id=rt.inspection_id
            WHERE i.shift_id=? ORDER BY rt.created_at DESC""", (shift_id,))
        faults = rows("SELECT * FROM faults ORDER BY created_at DESC")
        repairs = rows("""SELECT rp.*, f.fault_type, v.fleet_no FROM repairs rp
            JOIN faults f ON f.id=rp.fault_id JOIN vehicles v ON v.id=rp.vehicle_id
            ORDER BY rp.created_at DESC""")
        swaps = rows("SELECT * FROM swaps WHERE shift_id=? ORDER BY created_at DESC",
                     (shift_id,))
        releases = rows("""SELECT r.*, t.departure_at, l.code line_code, v.fleet_no
            FROM releases r JOIN trips t ON t.id=r.trip_id
            JOIN lines l ON l.id=t.line_id JOIN vehicles v ON v.id=r.vehicle_id
            WHERE r.shift_id=? ORDER BY r.created_at DESC""", (shift_id,))
        events = rows("SELECT * FROM events ORDER BY seq DESC LIMIT(200)")
        for e in events:
            try:
                e["detail_obj"] = json.loads(e["detail"]) if e["detail"] else {}
            except json.JSONDecodeError:
                e["detail_obj"] = {}
        staff_rows = rows("SELECT * FROM staff ORDER BY role, id")
        lines = rows("SELECT * FROM lines ORDER BY code")
        spares = self.available_spares(shift_id) if shift_id else []
        return {
            "shift": dict(shift) if shift else None,
            "now": ts(),
            "trips": trips, "vehicles": vehicles, "inspections": inspections,
            "ramp_tests": tests, "faults": faults, "repairs": repairs,
            "swaps": swaps, "releases": releases, "events": events,
            "staff": staff_rows, "lines": lines,
            "spares_available": spares,
            "spares_total": sum(1 for v in vehicles if v["is_spare"]),
            "reminders": self.scan_reminders(),
            "enums": {
                "roles": ROLE_LABELS, "vehicle_status": VEH_STATUS_LABELS,
                "faults": FAULT_LABELS, "ramp_types": RAMP_TYPES,
                "items": ITEM_LABELS,
            },
        }
