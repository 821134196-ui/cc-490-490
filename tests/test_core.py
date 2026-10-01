"""业务规则测试：班次唯一、故障阻断、换车容量、职责分离、放行幂等（并发）。"""
import threading
import unittest

from ramp_system.adapters import GateAdapter, TerminalAdapter
from ramp_system import seed as seed_mod
from ramp_system.store import (Store, BizError,
                               ROLE_DRIVER, ROLE_DISPATCHER, ROLE_MECHANIC,
                               VEH_IN_SERVICE, VEH_UNDER_REPAIR, VEH_RAMP_DISABLED,
                               F_RAMP_JAM, F_BUTTON_DEAD, CONSEQUENCE_RAMP,
                               CONSEQUENCE_FULL)

ALL_PASS = {k: True for k in
            ("appearance", "deploy_retract", "locking",
             "call_button", "pin_present", "load_label")}
FAIL_BUTTON = {**ALL_PASS, "call_button": False}


class TestBase(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.seed_info = seed_mod.seed(self.store, None)
        self.shift_id = self.seed_info["shift_id"]
        self.gate = GateAdapter()
        self.term = TerminalAdapter()

    def release(self, trip_id, who="S-C01"):
        return self.store.release(trip_id=trip_id, dispatcher_id=who,
                                  gate_adapter=self.gate,
                                  terminal_adapter=self.term)


class TestShiftUniqueInspection(TestBase):
    """同一车辆同一班次只能有一份有效点检。"""

    def test_second_active_inspection_rejected(self):
        # V-104 在种子数据中已有一份合格初检
        with self.assertRaises(BizError) as cm:
            self.store.submit_inspection(
                shift_id=self.shift_id, vehicle_id="V-104",
                driver_id="S-D04", trip_id="TR-004", items=ALL_PASS)
        self.assertEqual(cm.exception.code, "DUPLICATE_INSPECTION")
        self.assertEqual(cm.exception.status, 409)

    def test_only_one_active_row(self):
        with self.assertRaises(BizError):
            self.store.submit_inspection(
                shift_id=self.shift_id, vehicle_id="V-104",
                driver_id="S-D04", trip_id="TR-004", items=FAIL_BUTTON)
        rows = self.store.conn.execute(
            "SELECT COUNT(*) c FROM inspections WHERE shift_id=? AND vehicle_id=? AND active=1",
            (self.shift_id, "V-104")).fetchone()
        self.assertEqual(rows["c"], 1)

    def test_reinspection_supersedes_failed_only(self):
        # V-102 初检按钮不合格 → 维修完工 → 复检：旧单作废，新单成为唯一有效单
        repairs = {r["id"]: r for r in self.store.board_payload()["repairs"]}
        # 种子中 V-102 的维修单已完工且复检失败；再走一次完整流程
        flt = self.store.report_fault(
            vehicle_id="V-101", fault_type=F_BUTTON_DEAD, reported_by="S-D01",
            consequence=CONSEQUENCE_FULL, trip_id="TR-001")
        rpr = self.store.open_repair(fault_id=flt["fault_id"], mechanic_id="S-M01")
        self.store.complete_repair(repair_id=rpr["repair_id"], mechanic_id="S-M01")
        # 初检不合格
        first = self.store.submit_inspection(
            shift_id=self.shift_id, vehicle_id="V-101", driver_id="S-D01",
            trip_id="TR-001", items=FAIL_BUTTON)
        with self.assertRaises(BizError):  # 无维修关联的重复提交仍被拒
            self.store.submit_inspection(
                shift_id=self.shift_id, vehicle_id="V-101", driver_id="S-D01",
                trip_id="TR-001", items=FAIL_BUTTON)
        # 维修后复检允许作废旧不合格单
        out = self.store.reinspect(repair_id=rpr["repair_id"], driver_id="S-D01",
                                   items=ALL_PASS)
        self.assertEqual(out["result"], "PASS")
        self.assertEqual(out["superseded"], first["inspection_id"])
        active = self.store.conn.execute(
            "SELECT COUNT(*) c, SUM(active) s FROM inspections "
            "WHERE shift_id=? AND vehicle_id=?",
            (self.shift_id, "V-101")).fetchone()
        self.assertEqual(active["c"], 2)       # 历史保留
        self.assertEqual(active["s"], 1)       # 仅一份有效


class TestFaultBlocksRelease(TestBase):
    """发现故障后阻断出场；全部合格才可放行。"""

    def test_no_inspection_blocks(self):
        # TR-001 / V-101 未点检
        with self.assertRaises(BizError) as cm:
            self.release("TR-001")
        self.assertEqual(cm.exception.code, "RELEASE_BLOCKED")

    def test_failed_inspection_and_down_status_block(self):
        # TR-002 / V-102 按钮无响应、整车待修、复检失败
        with self.assertRaises(BizError) as cm:
            self.release("TR-002")
        self.assertEqual(cm.exception.code, "RELEASE_BLOCKED")
        self.assertIn("阻断", str(cm.exception))
        veh = self.store.conn.execute(
            "SELECT status FROM vehicles WHERE id='V-102'").fetchone()
        self.assertEqual(veh["status"], VEH_UNDER_REPAIR)
        # 阻断事件留痕
        flagged = self.store.conn.execute(
            "SELECT COUNT(*) c FROM events WHERE trip_id='TR-002' "
            "AND kind='RELEASE_BLOCKED'").fetchone()
        self.assertGreaterEqual(flagged["c"], 1)

    def test_ramp_disabled_status_blocks(self):
        # 新场景：在役车报坡板卡滞 → 无障碍停用 → 放行被阻断
        self.store.submit_inspection(
            shift_id=self.shift_id, vehicle_id="V-101", driver_id="S-D01",
            trip_id="TR-001", items={**ALL_PASS, "deploy_retract": False})
        self.store.report_fault(
            vehicle_id="V-101", fault_type=F_RAMP_JAM, reported_by="S-D01",
            consequence=CONSEQUENCE_RAMP, trip_id="TR-001")
        with self.assertRaises(BizError) as cm:
            self.release("TR-001")
        self.assertEqual(cm.exception.code, "RELEASE_BLOCKED")
        veh = self.store.conn.execute(
            "SELECT status FROM vehicles WHERE id='V-101'").fetchone()
        self.assertEqual(veh["status"], VEH_RAMP_DISABLED)

    def test_all_pass_then_release_ok(self):
        # TR-004 / V-104 种子里是合格在役车
        out = self.release("TR-004")
        self.assertFalse(out["duplicate"])
        self.assertIn("GATE:OPEN", out["gate_signal"])

    def test_repaired_vehicle_must_pass_reinspection(self):
        # 维修完工但未复检/复检不合格，不得恢复
        v102 = self.store.conn.execute(
            "SELECT status FROM vehicles WHERE id='V-102'").fetchone()
        self.assertEqual(v102["status"], VEH_UNDER_REPAIR)  # 复检失败维持待修
        # V-103 走完 故障→维修→复检合格 后恢复在役
        v103 = self.store.conn.execute(
            "SELECT status FROM vehicles WHERE id='V-103'").fetchone()
        self.assertEqual(v103["status"], VEH_IN_SERVICE)


class TestSpareCapacity(TestBase):
    """备用车容量：逐台占用，耗尽后拒绝换车。"""

    def test_seed_exhausts_pool(self):
        # 种子样例已占用 备901/902/903
        self.assertEqual(self.store.available_spares(self.shift_id), [])
        with self.assertRaises(BizError) as cm:
            self.store.swap_vehicle(trip_id="TR-002", dispatcher_id="S-C01",
                                    reason="容量测试")
        self.assertEqual(cm.exception.code, "SPARE_NONE_AVAILABLE")

    def test_capacity_decreases_per_swap(self):
        # 释放一台备用车（把 902/903 的换车记录对应任务视为已离场不可行，
        # 改用全新最小场景精确验证容量递减）
        s = Store(":memory:")
        s.conn.execute("INSERT INTO staff(id,name,role) VALUES('d','司机甲','driver')")
        s.conn.execute("INSERT INTO staff(id,name,role) VALUES('c','调度乙','dispatcher')")
        s.conn.execute("INSERT INTO lines(id,code,name,accessible_required)"
                       " VALUES('L','99路','试',1)")
        s.conn.execute("INSERT INTO shifts(id,label,work_date,active)"
                       " VALUES('SH','测试班','2026-10-01',1)")
        s.conn.execute("INSERT INTO vehicles(id,fleet_no,plate,ramp_type,load_rating_kg,"
                       "accessible,is_spare,status) VALUES('v0','0','X','ELECTRIC',300,1,0,'IN_SERVICE')")
        for i in (1, 2):
            s.conn.execute(
                "INSERT INTO vehicles(id,fleet_no,plate,ramp_type,load_rating_kg,"
                "accessible,is_spare,status) VALUES(?,?,?,?,?,?,?,?)",
                (f"v{i}", f"备{i}", f"B{i}", "ELECTRIC", 300, 1, 1, "IN_SERVICE"))
        for i, dep in enumerate(("2026-10-01T09:00:00", "2026-10-01T10:00:00"), 1):
            s.conn.execute(
                "INSERT INTO trips(id,shift_id,line_id,vehicle_id,driver_id,seq,"
                "departure_at,status) VALUES(?,?,?,?,?,?,?,?)",
                (f"t{i}", "SH", "L", "v0", "d", i, dep, "PLANNED"))
        s.conn.commit()

        self.assertEqual(len(s.available_spares("SH")), 2)
        r1 = s.swap_vehicle(trip_id="t1", dispatcher_id="c")
        self.assertEqual(len(s.available_spares("SH")), 1)
        self.assertEqual(r1["spares_remaining"], 1)
        r2 = s.swap_vehicle(trip_id="t2", dispatcher_id="c")
        self.assertEqual(r2["spares_remaining"], 0)
        with self.assertRaises(BizError) as cm:
            # 第三班无车可换
            s.conn.execute(
                "INSERT INTO trips(id,shift_id,line_id,vehicle_id,driver_id,seq,"
                "departure_at,status) VALUES('t3','SH','L','v0','d',3,"
                "'2026-10-01T11:00:00','PLANNED')")
            s.conn.commit()
            s.swap_vehicle(trip_id="t3", dispatcher_id="c")
        self.assertEqual(cm.exception.code, "SPARE_NONE_AVAILABLE")
        # 同一备用车不能被二次调配
        with self.assertRaises(BizError) as cm2:
            s.swap_vehicle(trip_id="t3", dispatcher_id="c",
                           spare_vehicle_id=r1["spare_vehicle_id"])
        self.assertEqual(cm2.exception.code, "SPARE_NOT_AVAILABLE")


class TestSegregationOfDuty(TestBase):
    """职责分离：驾驶员检查 / 调度员换车 / 维修人员不能复检。"""

    def test_only_driver_inspects(self):
        with self.assertRaises(BizError) as cm:
            self.store.submit_inspection(
                shift_id=self.shift_id, vehicle_id="V-101",
                driver_id="S-M01", trip_id="TR-001", items=ALL_PASS)  # 维修人员
        self.assertEqual(cm.exception.code, "FORBIDDEN_ROLE")
        with self.assertRaises(BizError) as cm2:
            self.store.submit_inspection(
                shift_id=self.shift_id, vehicle_id="V-101",
                driver_id="S-C01", trip_id="TR-001", items=ALL_PASS)  # 调度员
        self.assertEqual(cm2.exception.code, "FORBIDDEN_ROLE")

    def test_only_dispatcher_swaps_and_releases(self):
        with self.assertRaises(BizError) as cm:
            self.store.swap_vehicle(trip_id="TR-002", dispatcher_id="S-D01")
        self.assertEqual(cm.exception.code, "FORBIDDEN_ROLE")
        with self.assertRaises(BizError) as cm2:
            self.release("TR-004", who="S-D01")
        self.assertEqual(cm2.exception.code, "FORBIDDEN_ROLE")

    def test_only_mechanic_repairs(self):
        open_fault = self.store.conn.execute(
            "SELECT id FROM faults WHERE repair_id IS NULL AND consequence!='NONE' "
            "LIMIT 1").fetchone()
        with self.assertRaises(BizError) as cm:
            self.store.open_repair(fault_id=open_fault["id"], mechanic_id="S-D01")
        self.assertEqual(cm.exception.code, "FORBIDDEN_ROLE")

    def test_mechanic_cannot_approve_reinspection(self):
        # V-102 的维修单已完工待复检（种子复检失败后状态为 DELAYED，
        # 取一张 DONE 单：重新维修完工 V-102）
        rid = self.store.conn.execute(
            "SELECT id FROM repairs WHERE vehicle_id='V-102' LIMIT 1").fetchone()["id"]
        self.store.complete_repair(repair_id=rid, mechanic_id="S-M02",
                                   summary="再次更换按钮控制板")
        with self.assertRaises(BizError) as cm:
            self.store.reinspect(repair_id=rid, driver_id="S-M01",  # 维修人员
                                 items=ALL_PASS)
        self.assertEqual(cm.exception.code, "FORBIDDEN_ROLE")
        # 车辆状态未被改变
        veh = self.store.conn.execute(
            "SELECT status FROM vehicles WHERE id='V-102'").fetchone()
        self.assertEqual(veh["status"], VEH_UNDER_REPAIR)

    def test_driver_reinspect_works(self):
        rid = self.store.conn.execute(
            "SELECT id FROM repairs WHERE vehicle_id='V-102' LIMIT 1").fetchone()["id"]
        self.store.complete_repair(repair_id=rid, mechanic_id="S-M02",
                                   summary="再次更换按钮控制板")
        out = self.store.reinspect(repair_id=rid, driver_id="S-D02",
                                   items=ALL_PASS)
        self.assertEqual(out["result"], "PASS")
        veh = self.store.conn.execute(
            "SELECT status FROM vehicles WHERE id='V-102'").fetchone()
        self.assertEqual(veh["status"], VEH_IN_SERVICE)


class TestReleaseIdempotent(TestBase):
    """两人并发/重复放行：只成功一次，门控只开一次。"""

    def test_duplicate_release_is_idempotent(self):
        self.release("TR-004")
        n_after_first = self.gate.open_count
        again = self.release("TR-004")
        self.assertTrue(again["duplicate"])
        self.assertEqual(self.gate.open_count, n_after_first)  # 门控未再次开启
        rows = self.store.conn.execute(
            "SELECT COUNT(*) c FROM releases WHERE trip_id='TR-004'").fetchone()
        self.assertEqual(rows["c"], 1)

    def test_concurrent_releases_only_one_wins(self):
        results, errors = [], []
        barrier = threading.Barrier(6)

        def worker(staff_id):
            barrier.wait()
            try:
                results.append(self.release("TR-004", who=staff_id))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(who,))
                   for who in ("S-C01", "S-C02") * 3]  # 两调度员并发 6 次
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertFalse(errors, f"并发请求不应抛出异常: {errors}")
        winners = [r for r in results if not r["duplicate"]]
        dupes = [r for r in results if r["duplicate"]]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(dupes), 5)
        self.assertEqual(self.gate.open_count, 1)  # 门控只动作一次
        rows = self.store.conn.execute(
            "SELECT COUNT(*) c FROM releases WHERE trip_id='TR-004'").fetchone()
        self.assertEqual(rows["c"], 1)


class TestReminders(TestBase):
    """进程内提醒：发车时限 + 维修超时。"""

    def test_reminders_present(self):
        kinds = {r["kind"] for r in self.store.scan_reminders()}
        self.assertIn("REPAIR_OVERDUE", kinds)   # V-102 维修单期限已过
        # TR-001 距发车 8 分钟且无点检
        soon = [r for r in self.store.scan_reminders()
                if r.get("trip_id") == "TR-001" and r["kind"] == "DEPARTURE_NO_CHECK"]
        self.assertTrue(soon)


if __name__ == "__main__":
    unittest.main(verbosity=2)
