"""生成并打印三个必选样例：坡板卡滞、临时换车、复检失败。

用法：python3 -m ramp_system.samples
数据同时写入 data/ramp.db（供 Web 界面查看）。
"""
from __future__ import annotations

import json
import os

from . import seed as seed_mod
from .store import Store

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "data", "ramp.db")
PHOTO_DIR = os.path.join(BASE_DIR, "data", "photos")


def _pick(store: Store, *, vehicle=None, kinds=(), limit=20):
    sql = "SELECT * FROM events WHERE 1=1"
    args = []
    if vehicle:
        sql += " AND vehicle_id=?"
        args.append(vehicle)
    if kinds:
        sql += f" AND kind IN ({','.join('?' * len(kinds))})"
        args.extend(kinds)
    sql += " ORDER BY seq"
    return [{"at": r["at"], "kind": r["kind"],
             "actor": r["actor_id"], "detail": r["detail"]}
            for r in store.conn.execute(sql, args).fetchall()]


def main():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    if os.path.exists(DB_PATH):
        for suf in ("", "-wal", "-shm"):
            p = DB_PATH + suf
            if os.path.exists(p):
                os.remove(p)
    store = Store(DB_PATH)
    info = seed_mod.seed(store, PHOTO_DIR)

    print("=" * 72)
    print("演示数据已生成：班次", info["shift_id"],
          " 待发任务", len(info["trip_ids"]), "个")
    print("数据库:", DB_PATH)
    print("照片目录:", PHOTO_DIR)
    print("=" * 72)

    print("\n【样例 1】坡板卡滞 + 临时换车（车辆 103 → 备901，线路 12路 TR-003）")
    for e in _pick(store, vehicle="V-103",
                   kinds=("INSPECTION_FAIL", "FAULT_REPORT",
                          "VEHICLE_RAMP_DISABLED", "REPAIR_START",
                          "REPAIR_COMPLETE", "REINSPECTION_PASS",
                          "VEHICLE_BACK_IN_SERVICE")):
        print(f"  {e['at']}  {e['kind']:<26} {e['detail']}")
    for e in _pick(store, kinds=("SWAP", "RELEASE_GRANTED")):
        if e["detail"] and "103" in e["detail"]:
            print(f"  {e['at']}  {e['kind']:<26} {e['detail']}")

    print("\n【样例 2】备用车容量耗尽（备901/备902/备903 全部占用后换车被拒）")
    for e in _pick(store, kinds=("SWAP_DENIED_CAPACITY",)):
        print(f"  {e['at']}  {e['kind']:<26} {e['detail']}")

    print("\n【样例 3】复检失败（车辆 102 呼叫按钮，维修后复检仍无响应 → 继续待修）")
    for e in _pick(store, vehicle="V-102",
                   kinds=("INSPECTION_FAIL", "FAULT_REPORT", "VEHICLE_DOWN",
                          "REPAIR_START", "REPAIR_COMPLETE",
                          "REINSPECTION_FAIL", "REINSPECTION_FAILED_HOLD")):
        print(f"  {e['at']}  {e['kind']:<26} {e['detail']}")

    print("\n八类留痕统计：")
    rows = store.conn.execute("""
        SELECT fault_type, COUNT(*) c FROM faults GROUP BY fault_type
        UNION ALL
        SELECT 'TEMP_SWAP(events.SWAP)', COUNT(*) FROM events WHERE kind='SWAP'
        UNION ALL
        SELECT 'MISRELEASE(events)', COUNT(*) FROM events WHERE kind='MISRELEASE_FLAGGED'
    """).fetchall()
    for r in rows:
        print(f"  - {r[0]:<28} {r[1]} 条")
    print("\n样例 JSON 元数据：")
    print(json.dumps(info["samples"], ensure_ascii=False, indent=2,
                     default=str))


if __name__ == "__main__":
    main()
