"""证据接力服务的领域规则测试。"""

import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from datetime import datetime, timedelta, timezone

from neuro_evidence import (
    Actor,
    AUTHORITY,
    COORDINATOR,
    EVIDENCE_SUBMITTER,
    EventStore,
    EvidenceRelayService,
    HOSPITAL_ADMIN,
    IdempotencyIsolation,
    NotFoundError,
    PATIENT,
    PreconditionFailed,
    Projection,
    QuotaExhaustedError,
    RULE_MAINTAINER,
    UnauthorizedError,
)
from neuro_evidence.contracts import validate_event

TZ = timezone(timedelta(hours=8))
AT = datetime(2026, 9, 26, 9, 0, tzinfo=TZ)
FUTURE = datetime(2026, 10, 15, 10, 0, tzinfo=TZ)

PRODUCT = "BCI-1"
SERVICE = "SVC-REHAB"
HOSPITAL = "HOSP1"
REGION = "BJ"
INDICATION = "stroke_upper_limb"
REQUIRED = ["rct_pivot", "safety_6m"]


class RelayTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self.tmp.name) / "events.jsonl")
        self.svc = EvidenceRelayService(self.store)
        self.auth = Actor("auth-1", AUTHORITY)
        self.rule_a = Actor("rule-a", RULE_MAINTAINER)
        self.rule_b = Actor("rule-b", RULE_MAINTAINER)
        self.hospital = Actor("hos-1", HOSPITAL_ADMIN, hospital_id=HOSPITAL)
        self.evidence = Actor("ev-1", EVIDENCE_SUBMITTER)
        self.coord = Actor("coord-1", COORDINATOR)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # ----- 装配辅助 -------------------------------------------------------

    def bootstrap(self, *, price: float = 1200.0, quota: int | None = None) -> None:
        """走通：产品登记→监管确认→证据→映射批准→价格→医院准入→（试点）。"""
        self.svc.register_product(self.auth, PRODUCT, "脑机康复仪")
        self.svc.verify_status(self.auth, PRODUCT, "NMPA-2026-001", [INDICATION], REQUIRED)
        self.svc.register_service(self.rule_a, SERVICE, "脑机康复训练")
        self.svc.summarize_evidence(self.evidence, PRODUCT, "rct", "ref/rct1", ["rct_pivot"])
        self.svc.summarize_evidence(self.evidence, PRODUCT, "safety", "ref/s1", ["safety_6m"])
        self.svc.propose_mapping(self.rule_a, PRODUCT, SERVICE)
        self.svc.approve_mapping(self.rule_b, PRODUCT, SERVICE)
        self.svc.publish_price(self.rule_b, PRODUCT, SERVICE, price, AT)
        self.svc.admit_hospital(self.hospital, HOSPITAL, SERVICE, REGION)
        if quota is not None:
            self.svc.open_trial(self.coord, "T1", REGION, SERVICE, quota)

    def prepare_patient(self, patient_id: str = "P001",
                        categories=("identity", "clinical", "financial")) -> Actor:
        patient = Actor(patient_id, PATIENT)
        self.svc.record_consent(patient, patient_id, True, list(categories))
        self.svc.acknowledge_notice(patient, patient_id)
        return patient

    def book(self, booking_id: str = "B1", patient_id: str = "P001",
             when=FUTURE, trial_id=None) -> Actor:
        patient = self.prepare_patient(patient_id)
        self.svc.create_booking(
            self.coord, booking_id, patient_id, HOSPITAL, REGION,
            PRODUCT, SERVICE, INDICATION, when, trial_id,
        )
        return patient


class RoleAndSeparationTests(RelayTestCase):
    def test_only_authority_confirms_regulatory_status(self) -> None:
        self.svc.register_product(self.auth, PRODUCT, "x")
        with self.assertRaises(UnauthorizedError):
            self.svc.verify_status(self.coord, PRODUCT, "ref", [INDICATION], REQUIRED)
        with self.assertRaises(UnauthorizedError):
            self.svc.verify_status(self.rule_a, PRODUCT, "ref", [INDICATION], REQUIRED)

    def test_verify_requires_registered_product(self) -> None:
        with self.assertRaises(NotFoundError):
            self.svc.verify_status(self.auth, PRODUCT, "ref", [INDICATION], REQUIRED)

    def test_mapping_requires_separation_of_duties(self) -> None:
        self.bootstrap()
        # 第二个维护者提交新映射，不能自批。
        self.svc.register_service(self.rule_a, "SVC-2", "二")
        self.svc.propose_mapping(self.rule_a, PRODUCT, "SVC-2")
        with self.assertRaises(PreconditionFailed) as ctx:
            self.svc.approve_mapping(self.rule_a, PRODUCT, "SVC-2")
        self.assertIn("职责分离", ctx.exception.reasons[0])
        # 另一位维护者可以批准。
        ok = self.svc.approve_mapping(self.rule_b, PRODUCT, "SVC-2")
        self.assertEqual(1, ok.data["revision"])

    def test_approving_unproposed_mapping_fails(self) -> None:
        self.bootstrap()
        self.svc.register_service(self.rule_a, "SVC-X", "x")
        with self.assertRaises(NotFoundError):
            self.svc.approve_mapping(self.rule_b, PRODUCT, "SVC-X")

    def test_only_the_hospital_can_admit_itself(self) -> None:
        self.svc.register_service(self.rule_a, SERVICE, "x")
        with self.assertRaises(UnauthorizedError):
            self.svc.admit_hospital(Actor("hos-2", HOSPITAL_ADMIN, hospital_id="HOSP2"),
                                    HOSPITAL, SERVICE, REGION)
        with self.assertRaises(UnauthorizedError):
            self.svc.admit_hospital(self.coord, HOSPITAL, SERVICE, REGION)

    def test_patient_must_sign_own_consent_and_notice(self) -> None:
        with self.assertRaises(UnauthorizedError):
            self.svc.record_consent(Actor("P999", PATIENT), "P001", True, ["clinical"])
        with self.assertRaises(UnauthorizedError):
            self.svc.acknowledge_notice(self.coord, "P001")


class PinningAndSettlementTests(RelayTestCase):
    def test_booking_pins_versions_and_settlement_uses_them(self) -> None:
        self.bootstrap()
        self.book()
        # 预约后发布新版本价格：历史预约仍按 r1 结算。
        self.svc.publish_price(self.rule_b, PRODUCT, SERVICE, 1500.0,
                               AT + timedelta(days=1))
        result = self.svc.settle_claim(self.coord, "CLAIM-1", "B1")
        self.assertFalse(result.data["replayed"])
        self.assertEqual(1200.0, result.data["amount"])
        claim = Projection.rebuild(self.store.load()).claims["CLAIM-1"]
        self.assertEqual((1, 1, 1),
                         (claim.product_version, claim.service_revision, claim.price_revision))

    def test_duplicate_biz_key_returns_original(self) -> None:
        self.bootstrap()
        self.book()
        first = self.svc.settle_claim(self.coord, "K", "B1")
        second = self.svc.settle_claim(self.coord, "K", "B1")
        self.assertTrue(second.data["replayed"])
        self.assertEqual(first.data["amount"], second.data["amount"])
        # 只落了一条 CLAIM_SETTLED。
        settled = [e for e in self.store.load() if e.event_type == "CLAIM_SETTLED"]
        self.assertEqual(1, len(settled))

    def test_same_key_different_version_combo_is_isolated(self) -> None:
        self.bootstrap()
        self.book("B1", "P001")
        self.book("B2", "P002")
        self.svc.settle_claim(self.coord, "K", "B1")
        with self.assertRaises(IdempotencyIsolation) as ctx:
            self.svc.settle_claim(self.coord, "K", "B2")
        self.assertEqual("B1", ctx.exception.original["booking_id"])
        self.assertEqual("B2", ctx.exception.current["booking_id"])

    def test_settled_booking_cannot_be_rebilled_under_another_key(self) -> None:
        self.bootstrap()
        self.book()
        self.svc.settle_claim(self.coord, "K1", "B1")
        with self.assertRaises(PreconditionFailed):
            self.svc.settle_claim(self.coord, "K2", "B1")

    def test_blocked_booking_cannot_settle(self) -> None:
        self.bootstrap()
        self.book()
        # 收窄：把该适应证移出范围（范围合法缩小）。
        self.svc.narrow_scope(self.auth, PRODUCT, [], REQUIRED, "仅保留试验适应证")
        with self.assertRaises(PreconditionFailed):
            self.svc.settle_claim(self.coord, "K", "B1")

    def test_booking_requires_consent_notice_and_readiness(self) -> None:
        self.bootstrap()
        patient = Actor("P001", PATIENT)
        with self.assertRaises(PreconditionFailed) as ctx:
            self.svc.create_booking(
                self.coord, "B1", "P001", HOSPITAL, REGION,
                PRODUCT, SERVICE, INDICATION, FUTURE)
        self.assertTrue(any("授权" in r for r in ctx.exception.reasons))
        self.assertTrue(any("知情说明" in r for r in ctx.exception.reasons))
        # 补齐授权与知情后成立。
        self.svc.record_consent(patient, "P001", True, ["identity"])
        self.svc.acknowledge_notice(patient, "P001")
        self.svc.create_booking(
            self.coord, "B1", "P001", HOSPITAL, REGION,
            PRODUCT, SERVICE, INDICATION, FUTURE)


class QuotaConcurrencyTests(RelayTestCase):
    def test_concurrent_trial_allocations_never_exceed_quota(self) -> None:
        quota, contenders = 5, 20
        self.bootstrap(quota=quota)
        for i in range(contenders):
            self.prepare_patient(f"P{i:03d}")

        path = self.store.path

        def attempt(i: int) -> str:
            # 每个线程使用独立的存储实例，逼近独立进程的文件锁竞争。
            svc = EvidenceRelayService(EventStore(path))
            try:
                svc.create_booking(
                    self.coord, f"B{i:03d}", f"P{i:03d}", HOSPITAL, REGION,
                    PRODUCT, SERVICE, INDICATION, FUTURE, "T1")
                return "ok"
            except QuotaExhaustedError:
                return "full"

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(attempt, range(contenders)))
        self.assertEqual(quota, outcomes.count("ok"))
        self.assertEqual(contenders - quota, outcomes.count("full"))
        # 以磁盘为准重新打开存储核对，不信任主线程的内存视图。
        disk = Projection.rebuild(EventStore(path).load())
        self.assertEqual(quota, len(disk.trials["T1"]["allocations"]))

    def test_quota_check_uses_disk_truth_across_instances(self) -> None:
        self.bootstrap(quota=1)
        self.book("B1", "P001", trial_id="T1")
        other = EvidenceRelayService(EventStore(self.store.path))
        self.prepare_patient("P002")
        with self.assertRaises(QuotaExhaustedError):
            other.create_booking(
                self.coord, "B2", "P002", HOSPITAL, REGION,
                PRODUCT, SERVICE, INDICATION, FUTURE, "T1")


class EvidenceAndScopePropagationTests(RelayTestCase):
    def test_narrowing_blocks_future_booking_and_extra_evidence_reinstates(self) -> None:
        self.bootstrap()
        self.book()
        # 提高证据门槛：未来的预约立即重新核验并被阻断。
        self.svc.narrow_scope(
            self.auth, PRODUCT, [INDICATION], REQUIRED + ["longterm_followup"], "要求长期随访")
        proj = Projection.rebuild(self.store.load())
        self.assertEqual("BLOCKED", proj.bookings["B1"].status)
        # 补充证据：尚未发生的服务再次核验，通过后恢复可结算。
        self.svc.summarize_evidence(
            self.evidence, PRODUCT, "followup", "ref/lt1", ["longterm_followup"])
        proj = Projection.rebuild(self.store.load())
        self.assertEqual("ACTIVE", proj.bookings["B1"].status)
        result = self.svc.settle_claim(self.coord, "K", "B1")
        self.assertFalse(result.data["replayed"])

    def test_settled_history_goes_to_impact_review_and_is_never_rewritten(self) -> None:
        self.bootstrap()
        self.book()
        self.svc.settle_claim(self.coord, "CLAIM-1", "B1")
        self.svc.narrow_scope(self.auth, PRODUCT, [], REQUIRED, "适应证全部移出")

        proj = Projection.rebuild(self.store.load())
        # 历史结算原封不动：仍可查，版本与金额不变，预约仍为 SETTLED。
        claim = proj.claims["CLAIM-1"]
        self.assertEqual(1200.0, claim.amount)
        self.assertEqual("SETTLED", proj.bookings["B1"].status)
        # 进入影响评估队列，而不是被静默改写。
        tasks = proj.open_impact_tasks()
        self.assertEqual(1, len(tasks))
        task_id = tasks[0].task_id
        self.assertEqual("CLAIM-1", tasks[0].claim_biz_key)

        self.svc.review_impact(
            self.coord, task_id, "NO_ACTION",
            {"from": "2026-09-01", "to": "2026-10-01"}, "范围收窄不改变既往结算")
        proj = Projection.rebuild(self.store.load())
        self.assertEqual([], proj.open_impact_tasks())
        self.assertEqual("NO_ACTION", proj.impact_tasks[task_id].decision)
        self.assertEqual(1200.0, proj.claims["CLAIM-1"].amount)

    def test_review_rejects_unknown_decision(self) -> None:
        self.bootstrap()
        self.book()
        self.svc.settle_claim(self.coord, "CLAIM-1", "B1")
        self.svc.narrow_scope(self.auth, PRODUCT, [], REQUIRED, "x")
        task_id = Projection.rebuild(self.store.load()).open_impact_tasks()[0].task_id
        with self.assertRaises(PreconditionFailed):
            self.svc.review_impact(self.coord, task_id, "REWRITE_HISTORY", {})

    def test_past_service_is_not_reverified_only_future_is(self) -> None:
        self.bootstrap()
        # 补一个早已生效的价格，让"过去时点"的预约也能取到有效价格。
        self.svc.publish_price(
            self.rule_b, PRODUCT, SERVICE, 1200.0, datetime(2020, 1, 1, tzinfo=TZ))
        # 两笔预约都在收窄前成立：一笔服务时点已过，一笔尚未发生。
        past = datetime.now(TZ) - timedelta(hours=1)
        self.prepare_patient("P001")
        self.svc.create_booking(
            self.coord, "B1", "P001", HOSPITAL, REGION,
            PRODUCT, SERVICE, INDICATION, past)
        self.prepare_patient("P002")
        self.svc.create_booking(
            self.coord, "B2", "P002", HOSPITAL, REGION,
            PRODUCT, SERVICE, INDICATION, FUTURE)

        self.svc.narrow_scope(self.auth, PRODUCT, [], REQUIRED, "适应证全部移出")

        proj = Projection.rebuild(self.store.load())
        # 已过时点的服务不再重验；未来服务被阻断。
        self.assertEqual("ACTIVE", proj.bookings["B1"].status)
        self.assertEqual("BLOCKED", proj.bookings["B2"].status)
        reverified = {e.payload["booking_id"] for e in self.store.load()
                      if e.event_type in ("REVERIFICATION_PASSED", "REVERIFICATION_BLOCKED")}
        self.assertEqual({"B2"}, reverified)


class ResumeTests(RelayTestCase):
    def _fresh_service(self) -> EvidenceRelayService:
        return EvidenceRelayService(EventStore(self.store.path))

    def test_resume_completes_interrupted_reverification_pass(self) -> None:
        self.bootstrap()
        self.book()
        # 模拟崩溃：只落了 FLAGGED，结论事件未落盘。
        self.store.append(
            "reverify-flag:B1:fake-trigger", "REVERIFICATION_FLAGGED",
            "patient_booking", "B1",
            {"booking_id": "B1", "trigger_event_id": "fake-trigger", "reason": "crash test"},
        )
        resumed = self._fresh_service().resume()
        self.assertEqual(["reverify-pass:B1:fake-trigger"],
                         resumed["resumed_reverifications"])
        # 再恢复一次不重复处理。
        again = self._fresh_service().resume()
        self.assertEqual([], again["resumed_reverifications"])
        self.assertEqual("ACTIVE",
                         Projection.rebuild(self.store.load()).bookings["B1"].status)

    def test_resume_blocks_when_facts_no_longer_support(self) -> None:
        self.bootstrap()
        self.book()
        self.svc.narrow_scope(self.auth, PRODUCT, [], REQUIRED, "全部移出")
        # 上面的收窄自动开了影响评估；再造一个崩溃中的 FLAGGED。
        self.store.append(
            "reverify-flag:B1:orphan", "REVERIFICATION_FLAGGED",
            "patient_booking", "B1",
            {"booking_id": "B1", "trigger_event_id": "orphan", "reason": "crash"},
        )
        resumed = self._fresh_service().resume()
        self.assertIn("reverify-block:B1:orphan", resumed["resumed_reverifications"])
        self.assertEqual("BLOCKED",
                         Projection.rebuild(self.store.load()).bookings["B1"].status)

    def test_resume_reports_open_impact_tasks(self) -> None:
        self.bootstrap()
        self.book()
        self.svc.settle_claim(self.coord, "CLAIM-1", "B1")
        self.svc.narrow_scope(self.auth, PRODUCT, [], REQUIRED, "x")
        report = self._fresh_service().resume()
        self.assertEqual(1, len(report["open_impact_tasks"]))
        self.assertEqual("CLAIM-1", report["open_impact_tasks"][0]["claim_biz_key"])


class ExplanationAndDisclosureTests(RelayTestCase):
    def test_explain_lists_missing_evidence_and_breakpoints(self) -> None:
        self.svc.register_product(self.auth, PRODUCT, "x")
        self.svc.verify_status(self.auth, PRODUCT, "NMPA-1", [INDICATION], REQUIRED)
        self.svc.register_service(self.rule_a, SERVICE, "x")
        # 只补一类证据。
        self.svc.summarize_evidence(self.evidence, PRODUCT, "rct", "ref/r1", ["rct_pivot"])
        self.svc.propose_mapping(self.rule_a, PRODUCT, SERVICE)
        # 映射未批准、价格未发布、医院未准入、缺一类证据。
        explanation = self.svc.explain_availability(
            REGION, SERVICE, HOSPITAL, PRODUCT, INDICATION, FUTURE)
        row = explanation["products"][0]
        self.assertFalse(row["available"])
        self.assertEqual(["safety_6m"], row["missing_evidence"])
        joined = " ".join(row["reasons"])
        self.assertIn("临床证据", joined)
        self.assertIn("批准", joined)
        self.assertIn("价格版本", joined)
        self.assertIn("准入", joined)

    def test_explain_available_when_all_chain_ready(self) -> None:
        self.bootstrap()
        row = self.svc.explain_availability(
            REGION, SERVICE, HOSPITAL, PRODUCT, INDICATION, FUTURE)["products"][0]
        self.assertTrue(row["available"], row["reasons"])
        self.assertEqual([], row["missing_evidence"])

    def test_explain_wrong_region(self) -> None:
        self.bootstrap()
        row = self.svc.explain_availability(
            "SH", SERVICE, HOSPITAL, PRODUCT, INDICATION, FUTURE)["products"][0]
        self.assertFalse(row["available"])
        self.assertTrue(any("地区" in r for r in row["reasons"]))

    def test_patient_view_self_vs_consented_third_party(self) -> None:
        self.bootstrap()
        self.book()
        self.svc.settle_claim(self.coord, "CLAIM-1", "B1")

        own = self.svc.patient_view(Actor("P001", PATIENT), "P001")
        self.assertEqual("self", own["disclosure"])
        self.assertEqual("P001", own["patient_id"])
        self.assertEqual(1200.0, own["claims"][0]["amount"])

        # 第三方仅获 clinical 授权：身份与金额必须遮蔽。
        partial = self.svc.patient_view(Actor("coord-1", COORDINATOR), "P001")
        # setUp 的患者授予了全部类别
        self.assertIn("financial", partial["disclosure"])

        self.prepare_patient("P002", categories=["clinical"])
        self.svc.create_booking(
            self.coord, "B2", "P002", HOSPITAL, REGION,
            PRODUCT, SERVICE, INDICATION, FUTURE)
        limited = self.svc.patient_view(Actor("coord-1", COORDINATOR), "P002")
        self.assertEqual(EvidenceRelayService.REDACTION, limited["patient_id"])
        self.assertEqual([], limited["claims"])
        self.assertIn("product_code", limited["bookings"][0])
        self.assertNotIn("pinned_amount", limited["bookings"][0])

    def test_patient_view_without_consent_redacts_everything_sensitive(self) -> None:
        self.bootstrap()
        self.book()
        # 显式撤销授权。
        self.svc.record_consent(Actor("P001", PATIENT), "P001", False, [])
        view = self.svc.patient_view(Actor("coord-1", COORDINATOR), "P001")
        self.assertEqual(EvidenceRelayService.REDACTION, view["patient_id"])
        self.assertEqual([], view["claims"])
        self.assertNotIn("product_code", view["bookings"][0])
        self.assertNotIn("indication", view["bookings"][0])


class EventContractTests(RelayTestCase):
    def test_all_emitted_events_satisfy_service_schema(self) -> None:
        schema = json.loads((ROOT / "contracts/service.schema.json").read_text(encoding="utf-8"))
        self.bootstrap(quota=3)
        self.book(when=FUTURE, trial_id="T1")
        self.svc.settle_claim(self.coord, "CLAIM-1", "B1")
        self.svc.narrow_scope(
            self.auth, PRODUCT, [INDICATION], REQUIRED + ["longterm_followup"], "提高门槛")
        self.svc.summarize_evidence(
            self.evidence, PRODUCT, "lt", "ref/lt", ["longterm_followup"])
        task_id = Projection.rebuild(self.store.load()).open_impact_tasks()[0].task_id
        self.svc.review_impact(self.coord, task_id, "SUPPLEMENT_EVIDENCE",
                               {"from": "2026-09-01", "to": "2026-10-01"})
        for event in self.store.load():
            issues = validate_event(event.to_dict(), schema)
            self.assertEqual([], [(i.field, i.code) for i in issues],
                             f"{event.event_type} 不符合契约: {issues}")


if __name__ == "__main__":
    unittest.main()
