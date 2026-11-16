from fastapi.testclient import TestClient

from carbon_trace.api import create_app
from carbon_trace.errors import DomainError
from common import ASSAY, LOT, ORDER, Scenario


RESTRICTION = {"restriction_id": "X1", "source": "factory",
               "reason": "Compliance hold on upstream source",
               "effective_at": "2026-02-10T00:00:00Z"}
LIFT = {"lifted_at": "2026-02-12T00:00:00Z"}


class SourceRestrictionTests(Scenario):
    def _passed_material(self):
        self.catalog()
        self.ingest(LOT, ASSAY)
        self.app.create_order(ORDER)
        self.app.create_order({**ORDER, "order_id": "O2"})

    def _open(self, **overrides):
        payload = {**RESTRICTION, **overrides}
        return self.app.open_restriction(payload)

    # ---- 生命周期与审计 -------------------------------------------------

    def test_open_restriction_records_effective_time_and_revision(self):
        self.catalog()
        base = self.app.status()["revision"]
        result = self._open()
        self.assertEqual(result["status"], "active")
        self.assertTrue(result["currently_active"])
        self.assertEqual(result["created_revision"], base + 1)
        stored = self.app.get_restriction("X1")
        self.assertIsNone(stored["lifted_at"])
        self.assertEqual(stored["reason"], RESTRICTION["reason"])

    def test_future_dated_restriction_is_not_currently_active(self):
        self.catalog()
        result = self._open(restriction_id="XF", effective_at="2099-01-01T00:00:00Z")
        self.assertFalse(result["currently_active"])
        self.assertEqual(result["status"], "active")

    def test_cannot_open_two_active_restrictions_for_source(self):
        self.catalog()
        self._open()
        self.expect_domain(lambda: self._open(restriction_id="X2"), "business_invariant")

    def test_lift_records_lift_and_allows_new_restriction(self):
        self.catalog()
        opened = self._open()
        result = self.app.lift_restriction("X1", {**LIFT,
                                                   "expected_revision": self.app.status()["revision"]})
        self.assertEqual(result["status"], "lifted")
        self.assertFalse(result["currently_active"])
        self.assertEqual(result["lifted_revision"], opened["created_revision"] + 1)
        again = self._open(restriction_id="X2")
        self.assertEqual(again["restriction_id"], "X2")

    def test_lift_before_effective_time_rejected(self):
        self.catalog()
        self._open()
        self.expect_domain(lambda: self.app.lift_restriction(
            "X1", {"lifted_at": "2026-02-09T00:00:00Z",
                   "expected_revision": self.app.status()["revision"]}), "invalid_input")

    def test_double_lift_conflicts(self):
        self.catalog()
        self._open()
        self.app.lift_restriction("X1", {**LIFT, "expected_revision": self.app.status()["revision"]})
        self.expect_domain(lambda: self.app.lift_restriction(
            "X1", {**LIFT, "expected_revision": self.app.status()["revision"]}), "conflict")

    def test_lift_uses_optimistic_revision(self):
        self.catalog()
        self._open()
        self.expect_domain(lambda: self.app.lift_restriction(
            "X1", {**LIFT, "expected_revision": 0}), "stale_revision")

    # ---- 库存传播 -------------------------------------------------------

    def test_inventory_marks_restricted_lots_and_splits_tons(self):
        self._passed_material()
        self._open()
        inventory = self.app.inventory("P1", "2026-02-15")
        self.assertEqual(inventory["remaining_tons"], "40")
        self.assertEqual(inventory["available_tons"], "0")
        self.assertEqual(inventory["restricted_tons"], "40")
        entry = inventory["lots"][0]
        self.assertTrue(entry["restricted"])
        self.assertFalse(entry["available"])
        self.assertEqual(entry["source"], "factory")
        self.assertEqual(entry["restrictions"]["restriction_ids"], ["X1"])

    def test_inventory_before_effective_time_is_unrestricted(self):
        self._passed_material()
        self._open()
        inventory = self.app.inventory("P1", "2026-02-09")
        self.assertEqual(inventory["restricted_tons"], "0")
        self.assertEqual(inventory["available_tons"], "40")
        self.assertFalse(inventory["lots"][0]["restricted"])

    def test_inventory_recovers_after_lift(self):
        self._passed_material()
        self._open()
        self.app.lift_restriction("X1", {**LIFT, "expected_revision": self.app.status()["revision"]})
        inventory = self.app.inventory("P1", "2026-02-15")
        self.assertEqual(inventory["available_tons"], "40")
        self.assertEqual(inventory["restricted_tons"], "0")
        self.assertFalse(inventory["lots"][0]["restricted"])

    # ---- 预约决策 -------------------------------------------------------

    def test_reservation_blocked_while_restricted(self):
        self._passed_material()
        self._open()
        with self.assertRaises(DomainError) as raised:
            self.app.reserve({"reservation_id": "R1", "order_id": "O1", "lot_id": "LOT1",
                              "quantity": "10", "expected_revision": self.app.status()["revision"]})
        self.assertEqual(raised.exception.code, "business_invariant")
        self.assertEqual(raised.exception.detail["restriction_ids"], ["X1"])
        self.assertEqual(self.app.orders.detail("O1")["reservations"], [])

    def test_existing_reservation_survives_restriction(self):
        self._passed_material()
        self.app.reserve({"reservation_id": "R1", "order_id": "O1", "lot_id": "LOT1",
                          "quantity": "15", "expected_revision": self.app.status()["revision"]})
        self._open()
        detail = self.app.orders.detail("O1")
        self.assertEqual(detail["reserved_tons"], "15")
        self.assertEqual(detail["reservations"][0]["status"], "active")

    def test_future_dated_restriction_does_not_block_now(self):
        self._passed_material()
        self._open(restriction_id="XF", effective_at="2099-01-01T00:00:00Z")
        result = self.app.reserve({"reservation_id": "R1", "order_id": "O1", "lot_id": "LOT1",
                                   "quantity": "10", "expected_revision": self.app.status()["revision"]})
        self.assertEqual(result["quantity"], "10")

    def test_reservation_allowed_again_after_lift(self):
        self._passed_material()
        self._open()
        self.app.lift_restriction("X1", {**LIFT, "expected_revision": self.app.status()["revision"]})
        result = self.app.reserve({"reservation_id": "R1", "order_id": "O1", "lot_id": "LOT1",
                                   "quantity": "10", "expected_revision": self.app.status()["revision"]})
        self.assertEqual(result["quantity"], "10")

    # ---- 对外质量声明 ---------------------------------------------------

    def test_quality_statement_carries_compliance_block(self):
        self._passed_material()
        self._open()
        quality = self.app.quality_of_lot("LOT1")
        self.assertEqual(quality["status"], "passed")
        self.assertTrue(quality["compliance"]["restricted"])
        self.assertEqual(quality["compliance"]["restrictions"][0]["restriction_id"], "X1")
        self.app.lift_restriction("X1", {**LIFT, "expected_revision": self.app.status()["revision"]})
        self.assertFalse(self.app.quality_of_lot("LOT1")["compliance"]["restricted"])

    # ---- 可用性解释 -----------------------------------------------------

    def test_availability_explains_during_and_before_restriction(self):
        self._passed_material()
        self._open()
        during = self.app.lot_availability("LOT1", "2026-02-11T00:00:00Z")
        self.assertFalse(during["available"])
        self.assertEqual(during["availability_reasons"], ["source_restricted"])
        self.assertFalse(during["commitment_allowed"])
        self.assertIn("source_restricted", during["commitment_reasons"])
        before = self.app.lot_availability("LOT1", "2026-02-01T12:00:00Z")
        self.assertTrue(before["available"])
        self.assertEqual(before["availability_reasons"], [])
        self.assertTrue(before["commitment_allowed"])

    def test_availability_explains_quality_reason_independently(self):
        self.catalog()
        self.ingest(LOT)  # 未检验
        result = self.app.lot_availability("LOT1", "2026-02-03T00:00:00Z")
        self.assertTrue(result["available"])
        self.assertFalse(result["commitment_allowed"])
        self.assertEqual(result["commitment_reasons"], ["quality_unmeasured"])

    def test_availability_defaults_to_current_time(self):
        self._passed_material()
        self._open()
        result = self.app.lot_availability("LOT1")
        self.assertIn("at", result)
        self.assertFalse(result["available"])

    # ---- 历史不可变 -----------------------------------------------------

    def test_historical_shipments_and_reports_remain_untouched(self):
        self.populated()  # 含 SHIP 发运 10 吨
        self.app.create_order(ORDER)
        self._open()
        self.assertEqual(self.app.delivery.shipment("SH1")["shipment"]["quantity"], "10")
        self.assertEqual(len(self.app.trace.lot("LOT1")["shipments"]), 1)
        # 限制不阻断既有来源事件的取证查询
        receipt = self.app.intake.receipt("factory", "ship-1")
        self.assertEqual(receipt["event_kind"], "shipment")

    def test_published_report_is_not_rewritten_by_restriction(self):
        self._passed_material()
        published = self._publish("V1")
        self._open()
        frozen = self.app.publication.version(published["version"])
        self.assertEqual(frozen["rows_digest"], published["rows_digest"])
        self.assertNotIn("restricted", frozen["rows"][0])

    # ---- 解除后的既有承诺复核 -------------------------------------------

    def test_review_queue_distinguishes_legacy_from_new_commitments(self):
        self._passed_material()
        self.app.reserve({"reservation_id": "R1", "order_id": "O1", "lot_id": "LOT1",
                          "quantity": "15", "expected_revision": self.app.status()["revision"]})
        self._open()
        self.app.lift_restriction("X1", {**LIFT, "expected_revision": self.app.status()["revision"]})
        queue = self.app.restriction_review_queue("X1")
        self.assertEqual(queue["pending_count"], 1)
        self.assertEqual(queue["pending"][0]["reservation_id"], "R1")
        # 解除后产生的新预约不入队
        self.app.reserve({"reservation_id": "R2", "order_id": "O2", "lot_id": "LOT1",
                          "quantity": "5", "expected_revision": self.app.status()["revision"]})
        self.assertEqual([p["reservation_id"] for p in
                          self.app.restriction_review_queue("X1")["pending"]], ["R1"])

    def test_review_clears_queue_item_and_is_idempotent_guard(self):
        self._passed_material()
        self.app.reserve({"reservation_id": "R1", "order_id": "O1", "lot_id": "LOT1",
                          "quantity": "15", "expected_revision": self.app.status()["revision"]})
        self._open()
        self.app.lift_restriction("X1", {**LIFT, "expected_revision": self.app.status()["revision"]})
        result = self.app.review_restriction({"restriction_id": "X1", "reservation_id": "R1",
                                              "note": "Manual disposition: retain"})
        self.assertTrue(result["reviewed_revision"] > 0)
        self.assertEqual(self.app.restriction_review_queue("X1")["pending_count"], 0)
        self.expect_domain(lambda: self.app.review_restriction(
            {"restriction_id": "X1", "reservation_id": "R1", "note": "again"}), "conflict")

    def test_review_requires_lifted_restriction(self):
        self._passed_material()
        self.app.reserve({"reservation_id": "R1", "order_id": "O1", "lot_id": "LOT1",
                          "quantity": "15", "expected_revision": self.app.status()["revision"]})
        self._open()
        self.expect_domain(lambda: self.app.review_restriction(
            {"restriction_id": "X1", "reservation_id": "R1", "note": "early"}), "invalid_input")

    def test_reconciliation_flags_restriction_and_legacy_commitment(self):
        self._passed_material()
        self.app.reserve({"reservation_id": "R1", "order_id": "O1", "lot_id": "LOT1",
                          "quantity": "15", "expected_revision": self.app.status()["revision"]})
        self._open()
        active_report = self.app.reconciliation.plant("P1", "2026-02-01", "2026-02-28")
        self.assertIn("source_restricted", active_report["issue_counts"])
        self.app.lift_restriction("X1", {**LIFT, "expected_revision": self.app.status()["revision"]})
        lifted_report = self.app.reconciliation.plant("P1", "2026-02-01", "2026-02-28")
        lot_issues = next(e["issues"] for e in lifted_report["exceptions"] if e["lot_id"] == "LOT1")
        self.assertIn("legacy_commitment_pending_review", lot_issues)
        self.app.review_restriction({"restriction_id": "X1", "reservation_id": "R1", "note": "done"})
        cleared = self.app.reconciliation.plant("P1", "2026-02-01", "2026-02-28")
        self.assertEqual(cleared["issue_counts"].get("legacy_commitment_pending_review", 0), 0)
        remaining = [issue for exc in cleared["exceptions"] for issue in exc["issues"]]
        self.assertNotIn("legacy_commitment_pending_review", remaining)

    # ---- 溯源与审计 -----------------------------------------------------

    def test_trace_includes_restriction_history_and_revision_entities(self):
        self._passed_material()
        opened = self._open()
        lifted = self.app.lift_restriction("X1", {**LIFT,
                                                  "expected_revision": self.app.status()["revision"]})
        trace = self.app.trace.lot("LOT1")
        self.assertEqual([r["restriction_id"] for r in trace["source_restrictions"]], ["X1"])
        open_rev = self.app.trace.revision(opened["created_revision"])
        self.assertEqual(open_rev["audit"]["cause"], "restriction_open")
        self.assertEqual(open_rev["entity"]["restriction_id"], "X1")
        lift_rev = self.app.trace.revision(lifted["lifted_revision"])
        self.assertEqual(lift_rev["audit"]["cause"], "restriction_lift")
        self.assertEqual(lift_rev["entity"]["lifted_at"], LIFT["lifted_at"])

    def test_restriction_revision_is_audited(self):
        self.catalog()
        self._open()
        timeline = self.app.trace.timeline(1, self.app.status()["revision"])
        self.assertIn("restriction_open", [row["cause"] for row in timeline])

    # ---- HTTP 接口 ------------------------------------------------------

    def test_api_restriction_workflow(self):
        client = TestClient(create_app(self.path))
        self.catalog()
        self.ingest(LOT, ASSAY)
        response = client.post("/restrictions", json=RESTRICTION)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(client.get("/restrictions/X1").json()["status"], "active")
        listed = client.get("/restrictions", params={"source": "factory"})
        self.assertEqual(len(listed.json()), 1)
        inventory = client.get("/plants/P1/inventory", params={"as_of": "2026-02-15"})
        self.assertEqual(inventory.json()["restricted_tons"], "40")
        availability = client.get("/lots/LOT1/availability", params={"at": "2026-02-11T00:00:00Z"})
        self.assertFalse(availability.json()["available"])
        lift = client.post("/restrictions/X1/lift",
                           json={**LIFT, "expected_revision": self.app.status()["revision"]})
        self.assertEqual(lift.status_code, 201)
        self.assertEqual(client.get("/restrictions/X1/review-queue").json()["pending_count"], 0)

    def test_api_unknown_restriction_returns_404(self):
        client = TestClient(create_app(self.path))
        response = client.get("/restrictions/nope")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"], "not_found")

    def _publish(self, candidate_id):
        revision = self.app.status()["revision"]
        self.app.prepare({"candidate_id": candidate_id, "plant_id": "P1", "from_day": "2026-02-01",
                          "to_day": "2026-02-01", "expected_head": self.app.status()["head"],
                          "expected_revision": revision})
        return self.app.publish(candidate_id, {"expected_head": self.app.status()["head"],
                                               "expected_revision": revision})
