"""HTTP 服务（标准库）+ 进程内提醒线程。

启动：python -m ramp_system.server [--port 8080] [--reset]
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import seed as seed_mod
from .adapters import GateAdapter, TerminalAdapter, PhotoStorageAdapter
from .store import (Store, BizError, ROLE_LABELS, VEH_STATUS_LABELS,
                    FAULT_LABELS, F_DRIVER_MISSED, F_REPAIR_DELAY,
                    CONSEQUENCE_RAMP, CONSEQUENCE_FULL)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "ramp.db")
PHOTO_DIR = os.path.join(DATA_DIR, "photos")

ALL_ITEMS = ["appearance", "deploy_retract", "locking",
             "call_button", "pin_present", "load_label"]


class ReminderThread(threading.Thread):
    """进程内任务：每 20 秒扫描发车时限与维修超时，到期落留痕。"""

    def __init__(self, store: Store, interval: float = 20.0):
        super().__init__(daemon=True, name="reminder")
        self.store = store
        self.interval = interval
        self._stop = threading.Event()
        self.alerts_lock = threading.Lock()
        self.alerts: list[dict] = []
        self._fired_keys: set[str] = set()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception as exc:  # 提醒线程绝不能拖垮进程
                print(f"[reminder] {exc}")

    def tick(self):
        reminders = self.store.scan_reminders()
        with self.alerts_lock:
            self.alerts = reminders
        for r in reminders:
            key = f"{r['kind']}:{r.get('trip_id') or r.get('repair_id')}"
            # 只在关键节点落一次留痕：过发车时间未点检、维修超时
            if r["kind"] in ("MISSED_CHECK", "REPAIR_OVERDUE") and key not in self._fired_keys:
                self._fired_keys.add(key)
                with self.store.lock:
                    if r["kind"] == "MISSED_CHECK":
                        trip = self.store.conn.execute(
                            "SELECT * FROM trips WHERE id=?",
                            (r["trip_id"],)).fetchone()
                        already = self.store.conn.execute(
                            "SELECT 1 FROM faults WHERE trip_id=? AND fault_type=? "
                            "AND consequence='NONE' LIMIT 1",
                            (r["trip_id"], F_DRIVER_MISSED)).fetchone()
                        if trip and not already:
                            self.store.report_fault(
                                vehicle_id=trip["vehicle_id"],
                                fault_type=F_DRIVER_MISSED,
                                reported_by=trip["driver_id"],
                                consequence="NONE",
                                description="系统自动标记：超过发车时间仍未点检",
                                trip_id=trip["id"], auto_status=False)
                    else:
                        self.store.event("REPAIR_OVERDUE_FLAG",
                                         detail={"repair_id": r["repair_id"],
                                                 "minutes_overdue": r["minutes_overdue"],
                                                 "note": "维修超时，已通知调度"})


class AppState:
    def __init__(self, db_path: str = DB_PATH, photo_dir: str = PHOTO_DIR,
                 reset: bool = False):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        if reset and os.path.exists(db_path):
            for suffix in ("", "-wal", "-shm"):
                p = db_path + suffix
                if os.path.exists(p):
                    os.remove(p)
        self.store = Store(db_path)
        self.gate = GateAdapter()
        self.terminal = TerminalAdapter()
        self.photos = PhotoStorageAdapter(photo_dir)
        self.reminder = ReminderThread(self.store)
        seeded = self.store.active_shift()
        if not seeded:
            seed_mod.seed(self.store, photo_dir)
        self.reminder.start()


def make_handler(state: AppState):
    store = state.store

    class Handler(BaseHTTPRequestHandler):
        server_version = "RampCheck/1.0"

        def log_message(self, fmt, *args):
            print(f"[http] {self.address_string()} {fmt % args}")

        # ---- 工具 ----
        def _json(self, obj, status: int = 200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                raise BizError("BAD_JSON", "请求体不是合法 JSON")

        def _staff_id(self, body: dict, qs: dict) -> str:
            sid = body.get("staff_id") or (qs.get("as") or [None])[0]
            if not sid:
                raise BizError("NO_STAFF", "缺少 staff_id（当前操作者）")
            return sid

        # ---- 静态 ----
        def _serve_static(self, path: str):
            if path == "/":
                path = "/index.html"
            rel = path.lstrip("/").replace("..", "")
            full = os.path.join(STATIC_DIR, rel)
            if not os.path.isfile(full):
                self.send_error(404)
                return
            ctype = {"html": "text/html; charset=utf-8",
                     "css": "text/css; charset=utf-8",
                     "js": "application/javascript; charset=utf-8",
                     "png": "image/png", "svg": "image/svg+xml",
                     "json": "application/json"}.get(
                         full.rsplit(".", 1)[-1], "application/octet-stream")
            with open(full, "rb") as fh:
                data = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        # ---- 路由 ----
        def do_GET(self):
            parsed = urlparse(self.path)
            path, qs = parsed.path, parse_qs(parsed.query)
            try:
                if path == "/api/board":
                    payload = store.board_payload()
                    with state.reminder.alerts_lock:
                        payload["live_reminders"] = list(state.reminder.alerts)
                    payload["gate_log"] = state.gate.recent()
                    payload["terminal_log"] = state.terminal.recent()
                    return self._json(payload)
                if path == "/api/photos":
                    f = (qs.get("file") or [""])[0]
                    full = os.path.join(state.photos.directory, os.path.basename(f))
                    if not os.path.isfile(full):
                        raise BizError("NO_PHOTO", "照片不存在", 404)
                    with open(full, "rb") as fh:
                        data = fh.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    return self.wfile.write(data)
                if path.startswith("/api/"):
                    raise BizError("NOT_FOUND", "未知接口", 404)
                return self._serve_static(path)
            except BizError as exc:
                return self._json({"error": exc.code, "message": str(exc)}, exc.status)

        def do_POST(self):
            parsed = urlparse(self.path)
            path = parsed.path
            try:
                body = self._body()
                qs = parse_qs(parsed.query)
                if path == "/api/inspections":
                    out = store.submit_inspection(
                        shift_id=body["shift_id"], vehicle_id=body["vehicle_id"],
                        driver_id=self._staff_id(body, qs), trip_id=body.get("trip_id"),
                        items=self._items(body), ramp_test=body.get("ramp_test"),
                        note=body.get("note", ""), photos=body.get("photos") or [],
                        allow_supersede=bool(body.get("allow_supersede")))
                    return self._json(out, 201)

                if path == "/api/faults":
                    out = store.report_fault(
                        vehicle_id=body["vehicle_id"],
                        fault_type=body["fault_type"],
                        reported_by=self._staff_id(body, qs),
                        consequence=body.get("consequence", CONSEQUENCE_RAMP),
                        description=body.get("description", ""),
                        trip_id=body.get("trip_id"),
                        inspection_id=body.get("inspection_id"))
                    return self._json(out, 201)

                if path == "/api/swaps":
                    out = store.swap_vehicle(
                        trip_id=body["trip_id"],
                        dispatcher_id=self._staff_id(body, qs),
                        spare_vehicle_id=body.get("spare_vehicle_id"),
                        reason=body.get("reason", ""))
                    return self._json(out, 201)

                if path == "/api/repairs/open":
                    out = store.open_repair(
                        fault_id=body["fault_id"],
                        mechanic_id=self._staff_id(body, qs),
                        due_minutes=int(body.get("due_minutes", 60)))
                    return self._json(out, 201)

                if path == "/api/repairs/complete":
                    out = store.complete_repair(
                        repair_id=body["repair_id"],
                        mechanic_id=self._staff_id(body, qs),
                        summary=body.get("summary", ""),
                        bench_test=body.get("bench_test", "PASS"))
                    return self._json(out)

                if path == "/api/repairs/delay":
                    out = store.delay_repair(
                        repair_id=body["repair_id"],
                        mechanic_id=self._staff_id(body, qs),
                        reason=body.get("reason", "配件待料"))
                    return self._json(out)

                if path == "/api/reinspections":
                    out = store.reinspect(
                        repair_id=body["repair_id"],
                        driver_id=self._staff_id(body, qs),
                        items=self._items(body),
                        ramp_test=body.get("ramp_test"),
                        note=body.get("note", ""),
                        photos=body.get("photos") or [])
                    return self._json(out, 201)

                if path == "/api/releases":
                    out = store.release(
                        trip_id=body["trip_id"],
                        dispatcher_id=self._staff_id(body, qs),
                        gate_adapter=state.gate, terminal_adapter=state.terminal)
                    return self._json(out, 201 if not out.get("duplicate") else 200)

                if path == "/api/handovers":
                    out = store.handover(
                        trip_id=body["trip_id"],
                        driver_id=self._staff_id(body, qs),
                        shift_note=body.get("shift_note", ""))
                    return self._json(out, 201)

                if path == "/api/photos":
                    # 简单登记：文件名 + base64（演示）
                    import base64
                    name = body.get("file", "photo.png")
                    data = base64.b64decode(body.get("data", ""))
                    return self._json(state.photos.save(name, data), 201)

                if path == "/api/admin/reset":
                    seed_mod.seed(store, state.photos.directory)
                    state.gate.reset()
                    return self._json({"ok": True, "reseeded": True})

                raise BizError("NOT_FOUND", f"未知接口 {path}", 404)
            except KeyError as exc:
                return self._json({"error": "MISSING_FIELD",
                                   "message": f"缺少字段: {exc.args[0]}"}, 400)
            except BizError as exc:
                return self._json({"error": exc.code, "message": str(exc)}, exc.status)

        @staticmethod
        def _items(body) -> dict:
            raw = body.get("items")
            if not isinstance(raw, dict):
                raise BizError("ITEMS_MISSING", "需要 items 点检结果")
            bad = [k for k in raw if k not in ALL_ITEMS]
            if bad:
                raise BizError("BAD_ITEM", f"未知点检项: {','.join(bad)}")
            return raw

    return Handler


def serve(port: int = 8080, reset: bool = False):
    state = AppState(reset=reset)
    httpd = ThreadingHTTPServer(("0.0.0.0", port), make_handler(state))
    print(f"公交场站轮椅坡板点检与放行系统 → http://localhost:{port}")
    print(f"数据库: {DB_PATH}  照片目录: {PHOTO_DIR}")
    print("角色：驾驶员 S-D01..S-D04 / 调度员 S-C01,S-C02 / 维修 S-M01,S-M02")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state.reminder.stop()
        httpd.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="轮椅坡板点检与放行系统")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)))
    parser.add_argument("--reset", action="store_true", help="重置并重新生成演示数据")
    args = parser.parse_args(argv)
    serve(args.port, args.reset)


if __name__ == "__main__":
    main()
