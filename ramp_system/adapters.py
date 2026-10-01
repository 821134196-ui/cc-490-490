"""模拟适配器：车辆终端与场站门控信号（无真实硬件）。"""
from __future__ import annotations

import json
import threading
from collections import deque

from .store import ts


class TerminalAdapter:
    """模拟车辆车载终端：接收调度发车通知。"""

    def __init__(self):
        self.inbox = deque(maxlen=200)
        self.lock = threading.Lock()
        self._seq = 0

    def dispatch_notice(self, trip, inspection) -> dict:
        self._seq += 1
        payload = {
            "signal": f"TERM:DISPATCH:{trip['id']}#{self._seq:05d}",
            "vehicle_id": trip["vehicle_id"],
            "line_id": trip["line_id"],
            "departure_at": trip["departure_at"],
            "inspection_id": inspection["id"],
            "message": "坡板点检合格，准予发车",
            "delivered_at": ts(),
        }
        with self.lock:
            self.inbox.append(payload)
        return payload

    def recent(self, n: int = 20):
        with self.lock:
            return list(self.inbox)[-n:]


class GateAdapter:
    """模拟门控：放行时抬杆，记录开启次数（同一放行只开一次）。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.log = deque(maxlen=200)
        self._seq = 0
        self.open_count = 0

    def open_gate(self, trip, inspection) -> dict:
        with self.lock:
            self._seq += 1
            self.open_count += 1
            payload = {
                "signal": f"GATE:OPEN:{trip['id']}#{self._seq:05d}",
                "vehicle_id": trip["vehicle_id"],
                "barrier": "RAISED",
                "inspection_id": inspection["id"],
                "opened_at": ts(),
            }
            self.log.append(payload)
        return payload

    def recent(self, n: int = 20):
        with self.lock:
            return list(self.log)[-n:]

    def reset(self):
        with self.lock:
            self.log.clear()
            self.open_count = 0
            self._seq = 0


class PhotoStorageAdapter:
    """本地目录存放测试照片（此处只登记文件元数据，上传时落盘）。"""

    PHOTO_MANIFEST = "photos.json"

    def __init__(self, directory: str):
        import os
        self.directory = directory
        os.makedirs(directory, exist_ok=True)
        self.manifest_path = os.path.join(directory, self.PHOTO_MANIFEST)
        self.lock = threading.Lock()

    def save(self, filename: str, data: bytes) -> dict:
        import os
        path = os.path.join(self.directory, filename)
        with open(path, "wb") as fh:
            fh.write(data)
        meta = {"file": filename, "bytes": len(data), "saved_at": ts()}
        with self.lock:
            manifest = []
            if os.path.exists(self.manifest_path):
                try:
                    with open(self.manifest_path, encoding="utf-8") as fh:
                        manifest = json.load(fh)
                except (json.JSONDecodeError, OSError):
                    manifest = []
            manifest.append(meta)
            with open(self.manifest_path, "w", encoding="utf-8") as fh:
                json.dump(manifest, fh, ensure_ascii=False, indent=2)
        return meta
