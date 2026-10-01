"""模拟适配器：车载终端与门控信号。

真实场站中这些信号来自车载终端 CAN 总线/门控器；这里用确定性伪随机 +
数据库故障状态模拟，保证演示可复现：有未闭环卡滞故障的车会读到"卡滞"，
有按钮故障的车会读到"无响应"。
"""
from __future__ import annotations

import hashlib
from datetime import datetime


def _jitter(vehicle_id: int, salt: str, modulo: int) -> int:
    """按 车辆+分钟+salt 生成确定性伪随机数，同一分钟内读数稳定。"""
    minute = datetime.now().strftime("%Y%m%d%H%M")
    digest = hashlib.md5(f"{vehicle_id}:{salt}:{minute}".encode()).hexdigest()
    return int(digest[:8], 16) % modulo


def terminal_signals(conn, vehicle_id: int) -> dict:
    """车载终端信号：坡板位置、呼叫按钮、终端在线状态、信号龄期。"""
    open_types = {
        r["type"]
        for r in conn.execute(
            "SELECT type FROM faults WHERE vehicle_id=? AND status IN ('open','repaired')",
            (vehicle_id,),
        )
    }
    if "ramp_jam" in open_types:
        ramp = "jammed"
    else:
        ramp = "deployed" if _jitter(vehicle_id, "ramp", 2) else "stowed"
    button = "no_response" if "button_no_response" in open_types else "ok"
    return {
        "terminal_online": _jitter(vehicle_id, "online", 20) != 0,  # 95% 在线
        "ramp_position": ramp,            # stowed 收回 / deployed 展开 / jammed 卡滞
        "call_button": button,            # ok / no_response
        "signal_age_s": _jitter(vehicle_id, "age", 8) + 1,
        "source": "sim-terminal-adapter",
    }


def door_signals(conn, vehicle_id: int) -> dict:
    """门控器信号：前后门状态与车辆屈膝（kneeling）状态。"""
    open_types = {
        r["type"]
        for r in conn.execute(
            "SELECT type FROM faults WHERE vehicle_id=? AND status IN ('open','repaired')",
            (vehicle_id,),
        )
    }
    doors_ok = "other" not in open_types
    return {
        "front_door": "closed" if doors_ok else "fault",
        "rear_door": "closed",
        "kneeling": _jitter(vehicle_id, "kneel", 3) == 0,
        "interlock": "ok" if doors_ok else "fault",
        "source": "sim-door-adapter",
    }
