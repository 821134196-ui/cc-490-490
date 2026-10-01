"""Flask Web 层：REST API + 静态界面。"""
from __future__ import annotations

import json
import os
import uuid

from flask import Flask, jsonify, request, send_from_directory

from . import adapters, domain, reminders
from .db import Database
from .domain import DomainError, Forbidden
from .seed import seed_if_empty

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _actor_id() -> int:
    raw = request.headers.get("X-Staff-Id") or request.args.get("staff_id")
    if not raw:
        raise Forbidden("缺少当前身份（X-Staff-Id）")
    try:
        return int(raw)
    except ValueError:
        raise Forbidden("非法的身份标识")


def create_app(db_path: str = "data/depot.db", photos_dir: str = "photos",
               with_reminders: bool = True) -> Flask:
    app = Flask(__name__, static_folder=os.path.join(BASE_DIR, "static"),
                static_url_path="/static")
    db = Database(db_path)
    os.makedirs(photos_dir, exist_ok=True)
    app.config["DB"] = db
    app.config["PHOTOS_DIR"] = os.path.abspath(photos_dir)
    seeded = seed_if_empty(db, photos_dir)
    if with_reminders:
        reminders.start_reminder_thread(db)
    if seeded:
        print("[种子] 已生成演示数据与样例：坡板卡滞 / 临时换车 / 复检失败", flush=True)

    @app.errorhandler(DomainError)
    def _domain_err(exc: DomainError):
        return jsonify({"error": str(exc)}), exc.status

    # ------------------------------------------------------------ 页面与照片
    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/photos/<path:name>")
    def photo(name: str):
        return send_from_directory(app.config["PHOTOS_DIR"], name)

    @app.post("/api/photos")
    def upload_photo():
        file = request.files.get("file")
        if not file or not file.filename:
            return jsonify({"error": "缺少文件"}), 400
        ext = os.path.splitext(file.filename)[1].lower() or ".bin"
        name = f"{uuid.uuid4().hex[:12]}{ext}"
        file.save(os.path.join(app.config["PHOTOS_DIR"], name))
        return jsonify({"path": name}), 201

    # ------------------------------------------------------------ 基础数据
    @app.get("/api/bootstrap")
    def bootstrap():
        conn = db.connect()
        try:
            return jsonify({
                "staff": [dict(r) for r in conn.execute(
                    "SELECT * FROM staff ORDER BY role, id")],
                "routes": [dict(r) for r in conn.execute("SELECT * FROM routes ORDER BY id")],
                "vehicles": [dict(r) for r in conn.execute(
                    "SELECT * FROM vehicles ORDER BY is_backup, fleet_no")],
                "role_labels": domain.ROLE_LABELS,
                "vehicle_status_labels": domain.VEHICLE_STATUS_LABELS,
                "fault_types": domain.FAULT_TYPES,
                "item_labels": domain.ITEM_LABELS,
            })
        finally:
            conn.close()

    # ------------------------------------------------------------ 待发板
    @app.get("/api/board")
    def board():
        conn = db.connect()
        try:
            rows = conn.execute(
                "SELECT s.*, r.code route_code, r.name route_name,"
                " v.fleet_no, v.status vehicle_status, v.ramp_type, v.capacity,"
                " st.name driver_name,"
                " (SELECT result FROM inspections i WHERE i.shift_id=s.id"
                "   AND i.vehicle_id=s.vehicle_id AND i.valid=1) insp_result,"
                " (SELECT id FROM releases rel WHERE rel.shift_id=s.id) release_id,"
                " (SELECT COUNT(*) FROM faults f WHERE f.vehicle_id=s.vehicle_id"
                "   AND f.status IN ('open','repaired')) open_faults"
                " FROM shifts s JOIN routes r ON r.id=s.route_id"
                " JOIN vehicles v ON v.id=s.vehicle_id JOIN staff st ON st.id=s.driver_id"
                " ORDER BY s.service_date, r.code, s.depart_time",
            ).fetchall()
            return jsonify({"shifts": [dict(r) for r in rows]})
        finally:
            conn.close()

    @app.post("/api/shifts")
    def new_shift():
        data = request.get_json(force=True)
        return jsonify(domain.create_shift(
            db, route_id=int(data["route_id"]), vehicle_id=int(data["vehicle_id"]),
            driver_id=int(data["driver_id"]), service_date=data["service_date"],
            depart_time=data["depart_time"])), 201

    @app.get("/api/shifts/<int:shift_id>")
    def shift_detail(shift_id: int):
        conn = db.connect()
        try:
            shift = conn.execute(
                "SELECT s.*, r.code route_code, r.name route_name, r.min_capacity,"
                " r.requires_accessible, v.fleet_no, v.ramp_type, v.capacity,"
                " v.status vehicle_status, st.name driver_name"
                " FROM shifts s JOIN routes r ON r.id=s.route_id"
                " JOIN vehicles v ON v.id=s.vehicle_id JOIN staff st ON st.id=s.driver_id"
                " WHERE s.id=?", (shift_id,)).fetchone()
            if not shift:
                return jsonify({"error": "班次不存在"}), 404
            inspections = conn.execute(
                "SELECT i.*, st.name inspector_name FROM inspections i"
                " JOIN staff st ON st.id=i.inspector_id"
                " WHERE i.shift_id=? ORDER BY i.id DESC", (shift_id,)).fetchall()
            photos = conn.execute(
                "SELECT p.* FROM inspection_photos p JOIN inspections i"
                " ON i.id=p.inspection_id WHERE i.shift_id=?", (shift_id,)).fetchall()
            swaps = conn.execute(
                "SELECT sw.*, a.fleet_no from_no, b.fleet_no to_no, st.name dispatcher_name"
                " FROM swaps sw JOIN vehicles a ON a.id=sw.from_vehicle_id"
                " JOIN vehicles b ON b.id=sw.to_vehicle_id"
                " JOIN staff st ON st.id=sw.dispatcher_id WHERE sw.shift_id=?",
                (shift_id,)).fetchall()
            return jsonify({
                "shift": dict(shift),
                "inspections": [dict(r) for r in inspections],
                "photos": [dict(r) for r in photos],
                "swaps": [dict(r) for r in swaps],
            })
        finally:
            conn.close()

    @app.post("/api/shifts/<int:shift_id>/handover")
    def handover(shift_id: int):
        data = request.get_json(force=True)
        return jsonify(domain.handover_driver(
            db, shift_id=shift_id, to_driver_id=int(data["to_driver_id"]),
            actor_id=_actor_id()))

    @app.post("/api/shifts/<int:shift_id>/swap")
    def swap(shift_id: int):
        data = request.get_json(force=True)
        return jsonify(domain.swap_vehicle(
            db, shift_id=shift_id, to_vehicle_id=int(data["to_vehicle_id"]),
            actor_id=_actor_id(), reason=data.get("reason", "")))

    @app.post("/api/shifts/<int:shift_id>/release")
    def release(shift_id: int):
        data = request.get_json(force=True) or {}
        key = data.get("idempotency_key") or request.headers.get("Idempotency-Key")
        if not key:
            return jsonify({"error": "缺少幂等键 idempotency_key"}), 400
        return jsonify(domain.release_shift(
            db, shift_id=shift_id, actor_id=_actor_id(), idempotency_key=key))

    # ------------------------------------------------------------ 点检 / 复检
    @app.post("/api/inspections")
    def new_inspection():
        data = request.get_json(force=True)
        return jsonify(domain.submit_inspection(
            db, vehicle_id=int(data["vehicle_id"]), inspector_id=_actor_id(),
            items=data["items"], shift_id=data.get("shift_id"),
            notes=data.get("notes", ""), photos=data.get("photos"))), 201

    @app.get("/api/inspections")
    def list_inspections():
        conn = db.connect()
        try:
            where, args = [], []
            if request.args.get("shift_id"):
                where.append("i.shift_id=?")
                args.append(int(request.args["shift_id"]))
            if request.args.get("vehicle_id"):
                where.append("i.vehicle_id=?")
                args.append(int(request.args["vehicle_id"]))
            if request.args.get("kind"):
                where.append("i.kind=?")
                args.append(request.args["kind"])
            sql = (
                "SELECT i.*, v.fleet_no, st.name inspector_name FROM inspections i"
                " JOIN vehicles v ON v.id=i.vehicle_id JOIN staff st ON st.id=i.inspector_id"
                + (" WHERE " + " AND ".join(where) if where else "")
                + " ORDER BY i.id DESC LIMIT 100")
            rows = [dict(r) for r in conn.execute(sql, args)]
            for row in rows:
                row["photos"] = [dict(p) for p in conn.execute(
                    "SELECT item, path FROM inspection_photos WHERE inspection_id=?",
                    (row["id"],))]
            return jsonify({"inspections": rows})
        finally:
            conn.close()

    # ------------------------------------------------------------ 坡板测试 / 信号
    @app.get("/api/vehicles/<int:vehicle_id>/signals")
    def signals(vehicle_id: int):
        conn = db.connect()
        try:
            return jsonify({
                "terminal": adapters.terminal_signals(conn, vehicle_id),
                "door": adapters.door_signals(conn, vehicle_id),
            })
        finally:
            conn.close()

    @app.post("/api/vehicles/<int:vehicle_id>/ramp-test")
    def ramp_test(vehicle_id: int):
        conn = db.connect()
        try:
            sig = adapters.terminal_signals(conn, vehicle_id)
        finally:
            conn.close()
        return jsonify(domain.run_ramp_test(
            db, vehicle_id=vehicle_id, actor_id=_actor_id(), signals=sig))

    @app.get("/api/ramp-tests")
    def ramp_tests():
        conn = db.connect()
        try:
            rows = conn.execute(
                "SELECT rt.*, v.fleet_no, st.name actor_name FROM ramp_tests rt"
                " JOIN vehicles v ON v.id=rt.vehicle_id JOIN staff st ON st.id=rt.actor_id"
                " ORDER BY rt.id DESC LIMIT 50").fetchall()
            return jsonify({"tests": [dict(r) for r in rows]})
        finally:
            conn.close()

    # ------------------------------------------------------------ 故障 / 维修
    @app.post("/api/faults")
    def new_fault():
        data = request.get_json(force=True)
        return jsonify(domain.report_fault(
            db, vehicle_id=int(data["vehicle_id"]), type_=data["type"],
            severity=data.get("severity", "access"),
            description=data.get("description", ""),
            reported_by=_actor_id(), shift_id=data.get("shift_id"))), 201

    @app.get("/api/faults")
    def list_faults():
        conn = db.connect()
        try:
            rows = conn.execute(
                "SELECT f.*, v.fleet_no, st.name reporter_name FROM faults f"
                " JOIN vehicles v ON v.id=f.vehicle_id JOIN staff st ON st.id=f.reported_by"
                " ORDER BY f.id DESC LIMIT 100").fetchall()
            return jsonify({"faults": [dict(r) for r in rows]})
        finally:
            conn.close()

    @app.get("/api/repairs")
    def list_repairs():
        conn = db.connect()
        try:
            rows = conn.execute(
                "SELECT rp.*, f.type fault_type, f.description, v.fleet_no,"
                " st.name assignee_name FROM repairs rp"
                " JOIN faults f ON f.id=rp.fault_id JOIN vehicles v ON v.id=rp.vehicle_id"
                " LEFT JOIN staff st ON st.id=rp.assignee_id"
                " ORDER BY rp.status, rp.deadline").fetchall()
            return jsonify({"repairs": [dict(r) for r in rows]})
        finally:
            conn.close()

    @app.post("/api/repairs/<int:repair_id>/complete")
    def repair_done(repair_id: int):
        return jsonify(domain.complete_repair(db, repair_id=repair_id,
                                              actor_id=_actor_id()))

    # ------------------------------------------------------------ 复检
    @app.get("/api/rechecks")
    def rechecks():
        conn = db.connect()
        try:
            pending = conn.execute(
                "SELECT DISTINCT v.id vehicle_id, v.fleet_no, v.status FROM faults f"
                " JOIN vehicles v ON v.id=f.vehicle_id WHERE f.status='repaired'"
            ).fetchall()
            history = conn.execute(
                "SELECT i.*, v.fleet_no, st.name inspector_name FROM inspections i"
                " JOIN vehicles v ON v.id=i.vehicle_id JOIN staff st ON st.id=i.inspector_id"
                " WHERE i.kind='reinspection' ORDER BY i.id DESC LIMIT 50").fetchall()
            return jsonify({
                "pending": [dict(r) for r in pending],
                "history": [dict(r) for r in history],
            })
        finally:
            conn.close()

    # ------------------------------------------------------------ 放行 / 留痕 / 提醒
    @app.get("/api/releases")
    def list_releases():
        conn = db.connect()
        try:
            rows = conn.execute(
                "SELECT rel.*, v.fleet_no, st.name released_by_name,"
                " s.depart_time, r.code route_code FROM releases rel"
                " JOIN vehicles v ON v.id=rel.vehicle_id"
                " JOIN staff st ON st.id=rel.released_by"
                " JOIN shifts s ON s.id=rel.shift_id JOIN routes r ON r.id=s.route_id"
                " ORDER BY rel.id DESC LIMIT 100").fetchall()
            return jsonify({"releases": [dict(r) for r in rows]})
        finally:
            conn.close()

    @app.get("/api/events")
    def list_events():
        conn = db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT 200").fetchall()
            return jsonify({"events": [dict(r) for r in rows]})
        finally:
            conn.close()

    @app.get("/api/reminders")
    def list_reminders():
        return jsonify({"reminders": reminders.compute_reminders(db)})

    return app
