"""SQLite 数据层：连接管理与建表。

所有业务写操作通过 Database.connect() 取新连接并在 domain.py 中以
BEGIN IMMEDIATE 事务执行，保证并发下唯一约束（班次唯一、有效点检唯一、
放行幂等）由 SQLite 强制串行化。
"""
from __future__ import annotations

import os
import sqlite3

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS staff (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL,
    role  TEXT NOT NULL CHECK (role IN ('driver', 'dispatcher', 'mechanic'))
);

CREATE TABLE IF NOT EXISTS vehicles (
    id          INTEGER PRIMARY KEY,
    fleet_no    TEXT NOT NULL UNIQUE,          -- 车辆自编号，如 S2-01
    plate_no    TEXT NOT NULL,                 -- 车牌
    ramp_type   TEXT NOT NULL,                 -- 坡板类型：手动翻转式 / 电动伸缩式
    capacity    INTEGER NOT NULL,              -- 核载人数
    accessible  INTEGER NOT NULL DEFAULT 1,    -- 是否无障碍车
    is_backup   INTEGER NOT NULL DEFAULT 0,    -- 是否备用车
    status      TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'access_disabled', 'out_of_service'))
);

CREATE TABLE IF NOT EXISTS routes (
    id                  INTEGER PRIMARY KEY,
    code                TEXT NOT NULL UNIQUE,  -- 线路号
    name                TEXT NOT NULL,
    requires_accessible INTEGER NOT NULL DEFAULT 1,
    min_capacity        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS shifts (
    id           INTEGER PRIMARY KEY,
    route_id     INTEGER NOT NULL REFERENCES routes(id),
    vehicle_id   INTEGER NOT NULL REFERENCES vehicles(id),
    driver_id    INTEGER NOT NULL REFERENCES staff(id),
    service_date TEXT NOT NULL,                -- YYYY-MM-DD
    depart_time  TEXT NOT NULL,                -- HH:MM
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'released', 'cancelled')),
    missed_inspection_logged INTEGER NOT NULL DEFAULT 0,
    -- 班次唯一：同一线路同一日期同一发车时间只能有一个班次
    UNIQUE (route_id, service_date, depart_time)
);

CREATE TABLE IF NOT EXISTS inspections (
    id               INTEGER PRIMARY KEY,
    shift_id         INTEGER REFERENCES shifts(id),   -- 车辆级复检可为空
    vehicle_id       INTEGER NOT NULL REFERENCES vehicles(id),
    inspector_id     INTEGER NOT NULL REFERENCES staff(id),
    kind             TEXT NOT NULL DEFAULT 'initial'
                     CHECK (kind IN ('initial', 'reinspection')),
    item_appearance  INTEGER NOT NULL,   -- 坡板外观与承重标识
    item_deploy      INTEGER NOT NULL,   -- 展开/收回测试
    item_locking     INTEGER NOT NULL,   -- 锁止与固定销
    item_button      INTEGER NOT NULL,   -- 车内呼叫按钮
    notes            TEXT NOT NULL DEFAULT '',
    result           TEXT NOT NULL CHECK (result IN ('pass', 'fail')),
    valid            INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL
);
-- 同一车辆同一班次只能有一份有效点检（部分唯一索引）
CREATE UNIQUE INDEX IF NOT EXISTS uq_valid_inspection
    ON inspections (shift_id, vehicle_id) WHERE valid = 1 AND shift_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS inspection_photos (
    id            INTEGER PRIMARY KEY,
    inspection_id INTEGER NOT NULL REFERENCES inspections(id),
    item          TEXT NOT NULL DEFAULT '',
    path          TEXT NOT NULL            -- 本地目录 photos/ 下的文件名
);

CREATE TABLE IF NOT EXISTS faults (
    id            INTEGER PRIMARY KEY,
    vehicle_id    INTEGER NOT NULL REFERENCES vehicles(id),
    shift_id      INTEGER REFERENCES shifts(id),
    inspection_id INTEGER REFERENCES inspections(id),
    type          TEXT NOT NULL,           -- ramp_jam / pin_missing / button_no_response / label_peeling / other
    severity      TEXT NOT NULL DEFAULT 'access' CHECK (severity IN ('access', 'vehicle')),
    description   TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open', 'repaired', 'closed')),
    reported_by   INTEGER NOT NULL REFERENCES staff(id),
    created_at    TEXT NOT NULL,
    repaired_at   TEXT,
    closed_at     TEXT
);

CREATE TABLE IF NOT EXISTS repairs (
    id           INTEGER PRIMARY KEY,
    fault_id     INTEGER NOT NULL REFERENCES faults(id),
    vehicle_id   INTEGER NOT NULL REFERENCES vehicles(id),
    assignee_id  INTEGER REFERENCES staff(id),
    status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done')),
    deadline     TEXT NOT NULL,
    delayed_logged INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS ramp_tests (
    id         INTEGER PRIMARY KEY,
    vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
    actor_id   INTEGER NOT NULL REFERENCES staff(id),
    action     TEXT NOT NULL DEFAULT 'deploy_retract',  -- 展开/收回循环
    result     TEXT NOT NULL CHECK (result IN ('pass', 'fail')),
    signals    TEXT NOT NULL DEFAULT '',                -- 适配器信号快照 JSON
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS releases (
    id              INTEGER PRIMARY KEY,
    shift_id        INTEGER NOT NULL UNIQUE REFERENCES shifts(id),  -- 放行幂等：一班次一放行
    vehicle_id      INTEGER NOT NULL REFERENCES vehicles(id),
    released_by     INTEGER NOT NULL REFERENCES staff(id),
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS swaps (
    id              INTEGER PRIMARY KEY,
    shift_id        INTEGER NOT NULL REFERENCES shifts(id),
    from_vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
    to_vehicle_id   INTEGER NOT NULL REFERENCES vehicles(id),
    dispatcher_id   INTEGER NOT NULL REFERENCES staff(id),
    reason          TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS handovers (
    id             INTEGER PRIMARY KEY,
    shift_id       INTEGER NOT NULL REFERENCES shifts(id),
    from_driver_id INTEGER NOT NULL REFERENCES staff(id),
    to_driver_id   INTEGER NOT NULL REFERENCES staff(id),
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    type       TEXT NOT NULL,     -- ramp_jam / vehicle_swap / repair_delayed / erroneous_release_blocked ...
    message    TEXT NOT NULL,
    payload    TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_schema(self) -> None:
        conn = self.connect()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()
