import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from neuro_evidence.service import (  # noqa: E402
    Actor,
    EvidenceRelayService,
    PermissionDenied,
    PreconditionFailed,
    QuotaExhausted,
    VersionConflict,
)
from neuro_evidence.store import EventStore  # noqa: E402

PRODUCT = "bci-rehab-001"
SVC = "MED-REHAB-BCI"
REGION = "330000"
HOSP = "h-1"
IND = "stroke-upper-limb"

AUTH = Actor("auth-1", "authority")
HOSP_ACTOR = Actor("hosp-1", "hospital")
RM_A = Actor("rm-a", "rule_maintainer")
RM_B = Actor("rm-b", "rule_maintainer")
COORD = Actor("coord-1", "coordinator")


def bootstrap(service: EvidenceRelayService, *, quota: int = 5) -> None:
    """搭建一条全部闸门通过的产品→结算链路。"""
    service.verify_status(AUTH, product_id=PRODUCT, authority_ref="NMPA-2026-BCI", scope="上肢康复")
    service.declare_indication(COORD, product_id=PRODUCT, indication_id=IND, label="脑卒中上肢功能障碍")
    service.summarize_evidence(COORD, product_id=PRODUCT, evidence_kind="pivotal_trial", summary_ref="trial/A-307")
    service.register_service(COORD, service_item_code=SVC, name="脑机接口上肢康复训练")
    service.propose_mapping(RM_A, product_id=PRODUCT, service_item_code=SVC)
    service.approve_mapping(RM_B, product_id=PRODUCT, service_item_code=SVC)
    past = (datetime.now().astimezone() - timedelta(days=30)).isoformat(timespec="seconds")
    service.add_price_version(COORD, product_id=PRODUCT, price_revision=1, amount=1200.0, effective_from=past)
    service.hospital_admit(HOSP_ACTOR, hospital_id=HOSP, service_item_code=SVC)
    service.open_trial(COORD, region=REGION, service_item_code=SVC, quota_total=quota)


def consent_and_grant(service: EvidenceRelayService, patient: str) -> None:
    service.record_consent(COORD, patient_ref=patient, disclosure_scope=["claim_detail"], granted=True)
    service.grant_quota(COORD, region=REGION, service_item_code=SVC, patient_ref=patient)


def request_and_settle(service: EvidenceRelayService, patient: str):
    service.request_service(
        COORD,
        region=REGION,
        hospital_id=HOSP,
        patient_ref=patient,
        product_id=PRODUCT,
        service_item_code=SVC,
        indication_id=IND,
    )
    return service.settle(
        COORD, region=REGION, hospital_id=HOSP, patient_ref=patient, service_item_code=SVC
    )


class RoleAndDutyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = EvidenceRelayService(EventStore.connect(":memory:"))

    def test_status_only_authority(self) -> None:
        with self.assertRaises(PermissionDenied):
            self.svc.verify_status(COORD, product_id=PRODUCT, authority_ref="x", scope="s")
        with self.assertRaises(PermissionDenied):
            self.svc.verify_status(RM_A, product_id=PRODUCT, authority_ref="x", scope="s")

    def test_mapping_only_rule_maintainer(self) -> None:
        bootstrap(self.svc)
        # register_service 已在 bootstrap 中用过 coordinator；这里验证提案角色。
        with self.assertRaises(PermissionDenied):
            self.svc.propose_mapping(COORD, product_id=PRODUCT, service_item_code="OTHER")

    def test_self_approval_is_forbidden_but_peer_approval_ok(self) -> None:
        bootstrap(self.svc)
        self.svc.register_service(COORD, service_item_code="SVC2", name="第二项目")
        self.svc.propose_mapping(RM_A, product_id=PRODUCT, service_item_code="SVC2")
        with self.assertRaises(PermissionDenied):
            self.svc.approve_mapping(RM_A, product_id=PRODUCT, service_item_code="SVC2")
        # 另一名规则维护者可以批准。
        self.svc.approve_mapping(RM_B, product_id=PRODUCT, service_item_code="SVC2")

    def test_hospital_alone_decides_admission(self) -> None:
        self.svc.verify_status(AUTH, product_id=PRODUCT, authority_ref="r", scope="s")
        self.svc.register_service(COORD, service_item_code=SVC, name="n")
        with self.assertRaises(PermissionDenied):
            self.svc.hospital_admit(COORD, hospital_id=HOSP, service_item_code=SVC)
        self.svc.hospital_admit(HOSP_ACTOR, hospital_id=HOSP, service_item_code=SVC)


class SettlementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = EvidenceRelayService(EventStore.connect(":memory:"))
        bootstrap(self.svc)
        consent_and_grant(self.svc, "p-1")

    def test_settlement_pins_versions_and_replays(self) -> None:
        first = request_and_settle(self.svc, "p-1")
        self.assertFalse(first["replayed"])
        self.assertEqual(first["result"]["service_revision"], 6)  # 第 6 个事件即映射批准
        self.assertEqual(first["result"]["price_revision"], 1)

        again = self.svc.settle(
            COORD, region=REGION, hospital_id=HOSP, patient_ref="p-1", service_item_code=SVC
        )
        self.assertTrue(again["replayed"])
        self.assertEqual(again["result"], first["result"])

        # 原结算事件只有一条，重复请求不产生第二张结算单。
        settled = self.svc._world().claims[first["claim_id"]]
        self.assertEqual(settled.price_revision, 1)

    def test_same_key_version_change_is_isolated_conflict(self) -> None:
        first = request_and_settle(self.svc, "p-1")
        # 新版本价格生效（在当前时点之前），服务/价格版本随之变化。
        self.svc.add_price_version(
            COORD,
            product_id=PRODUCT,
            price_revision=2,
            amount=980.0,
            effective_from=datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        with self.assertRaises(VersionConflict):
            self.svc.settle(
                COORD, region=REGION, hospital_id=HOSP, patient_ref="p-1", service_item_code=SVC
            )
        # 历史结算未被改写，仍固定在价格版本 1。
        claim = self.svc._world().claims[first["claim_id"]]
        self.assertEqual(claim.price_revision, 1)
        self.assertEqual(len(claim.impact_reviews), 0)

    def test_request_requires_quota_slot(self) -> None:
        # p-2 未取得名额，申请被拒。
        self.svc.record_consent(COORD, patient_ref="p-2", disclosure_scope=["claim_detail"], granted=True)
        with self.assertRaises(PreconditionFailed):
            self.svc.request_service(
                COORD,
                region=REGION,
                hospital_id=HOSP,
                patient_ref="p-2",
                product_id=PRODUCT,
                service_item_code=SVC,
                indication_id=IND,
            )


class QuotaConcurrencyTests(unittest.TestCase):
    def test_concurrent_grant_never_overruns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "relay.db")
            seeder = EvidenceRelayService(EventStore.connect(path))
            bootstrap(seeder, quota=5)

            outcomes: list[bool] = []
            lock = threading.Lock()

            def attempt(patient: str) -> None:
                svc = EvidenceRelayService(EventStore.connect(path))
                try:
                    svc.grant_quota(
                        COORD, region=REGION, service_item_code=SVC, patient_ref=patient
                    )
                    ok = True
                except QuotaExhausted:
                    ok = False
                with lock:
                    outcomes.append(ok)

            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(attempt, [f"p-{i:02d}" for i in range(20)]))

            self.assertEqual(sum(outcomes), 5)
            self.assertEqual(len(outcomes), 20)
            checker = EvidenceRelayService(EventStore.connect(path))
            self.assertEqual(checker.store.quota_used(region=REGION, service_item_code=SVC), 5)
            granted = checker._world().trials[(REGION, SVC)]["granted"]
            self.assertEqual(len(granted), len(set(granted)))

    def test_duplicate_patient_grant_is_idempotent(self) -> None:
        svc = EvidenceRelayService(EventStore.connect(":memory:"))
        bootstrap(svc, quota=5)
        first = svc.grant_quota(COORD, region=REGION, service_item_code=SVC, patient_ref="px")
        second = svc.grant_quota(COORD, region=REGION, service_item_code=SVC, patient_ref="px")
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(svc.store.quota_used(region=REGION, service_item_code=SVC), 1)


class PropagationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = EvidenceRelayService(EventStore.connect(":memory:"))
        bootstrap(self.svc)

    def test_scope_narrow_revalidates_pending_and_reviews_history(self) -> None:
        consent_and_grant(self.svc, "hist")
        consent_and_grant(self.svc, "pend")
        history = request_and_settle(self.svc, "hist")
        # pend 仅申请、未结算。
        self.svc.request_service(
            COORD, region=REGION, hospital_id=HOSP, patient_ref="pend",
            product_id=PRODUCT, service_item_code=SVC, indication_id=IND,
        )

        out = self.svc.narrow_scope(
            COORD, product_id=PRODUCT, removed_indication_ids=[IND], reason="监管修订适用范围"
        )
        self.assertIsNotNone(out["impact_task_id"])

        world = self.svc._world()
        pending = world.claims["claim-330000-h-1-pend-MED-REHAB-BCI"]
        self.assertFalse(pending.revalidations[-1]["still_bookable"])
        # 重新核验未通过的服务不得继续结算。
        with self.assertRaises(PreconditionFailed):
            self.svc.settle(COORD, region=REGION, hospital_id=HOSP, patient_ref="pend", service_item_code=SVC)

        historical = world.claims[history["claim_id"]]
        self.assertIsNotNone(historical.impact_open)
        # 历史结算金额/版本未被静默改写。
        self.assertEqual(historical.price_revision, 1)
        self.assertTrue(historical.settled)

        # 完成影响评估（进程恢复续跑同一入口）。
        reports = self.svc.resume_impact_reviews(
            COORD, decide=lambda ctx: "recover_fund"
        )
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["decision"], "recover_fund")
        self.assertIn(history["claim_id"], reports[0]["affected_claims"])

        historical = self.svc._world().claims[history["claim_id"]]
        self.assertIsNone(historical.impact_open)
        self.assertEqual(historical.impact_reviews[-1]["decision"], "recover_fund")
        self.assertEqual(historical.price_revision, 1)  # 仍不改写历史

        # 再次恢复：没有未完成任务。
        self.assertEqual(self.svc.resume_impact_reviews(COORD), [])

    def test_evidence_added_revalidates_pending_as_still_bookable(self) -> None:
        consent_and_grant(self.svc, "pend")
        self.svc.request_service(
            COORD, region=REGION, hospital_id=HOSP, patient_ref="pend",
            product_id=PRODUCT, service_item_code=SVC, indication_id=IND,
        )
        self.svc.summarize_evidence(
            COORD, product_id=PRODUCT, evidence_kind="real_world", summary_ref="rw/2026-q3"
        )
        claim = self.svc._world().claims["claim-330000-h-1-pend-MED-REHAB-BCI"]
        self.assertTrue(claim.revalidations[-1]["still_bookable"])


class RecoveryTests(unittest.TestCase):
    def test_open_impact_task_survives_new_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "relay.db")
            svc = EvidenceRelayService(EventStore.connect(path))
            bootstrap(svc)
            consent_and_grant(svc, "hist")
            request_and_settle(svc, "hist")
            svc.narrow_scope(COORD, product_id=PRODUCT, removed_indication_ids=[IND], reason="x")

            # 模拟进程重启：全新存储连接与服务实例。
            restarted = EvidenceRelayService(EventStore.connect(path))
            open_tasks = restarted.store.list_open_impact_tasks()
            self.assertEqual(len(open_tasks), 1)
            reports = restarted.resume_impact_reviews(COORD)
            self.assertEqual(len(reports), 1)
            self.assertEqual(reports[0]["decision"], "keep_as_settled")
            self.assertEqual(restarted.store.list_open_impact_tasks(), [])


class ExplainAndConsentTests(unittest.TestCase):
    def test_explain_lists_blockers_and_missing_evidence(self) -> None:
        svc = EvidenceRelayService(EventStore.connect(":memory:"))
        svc.verify_status(AUTH, product_id=PRODUCT, authority_ref="r", scope="s")
        view = svc.explain_availability(
            product_id=PRODUCT, service_item_code=SVC, region=REGION,
            hospital_id=HOSP, indication_id=IND,
        )
        self.assertFalse(view["available"])
        self.assertIn("pivotal_trial", view["missing_evidence"])
        codes = {b["code"] for b in view["blockers"]}
        self.assertIn("evidence_incomplete", codes)
        self.assertIn("mapping_unapproved", codes)
        self.assertIn("hospital_not_admitted", codes)
        self.assertIn("trial_not_open", codes)

    def test_explain_available_after_full_chain(self) -> None:
        svc = EvidenceRelayService(EventStore.connect(":memory:"))
        bootstrap(svc)
        view = svc.explain_availability(
            product_id=PRODUCT, service_item_code=SVC, region=REGION,
            hospital_id=HOSP, indication_id=IND,
        )
        self.assertTrue(view["available"])
        self.assertEqual(view["blockers"], [])
        self.assertEqual(view["price_revision"], 1)

    def test_consent_scope_gates_personal_disclosure(self) -> None:
        svc = EvidenceRelayService(EventStore.connect(":memory:"))
        bootstrap(svc)
        # 未授权。
        svc.grant_quota(COORD, region=REGION, service_item_code=SVC, patient_ref="p")
        # 没有知情记录不能申请结算，直接构造受限视图场景：先授权结算，再改成拒绝披露。
        svc.record_consent(COORD, patient_ref="p", disclosure_scope=["claim_detail"], granted=True)
        settled = request_and_settle(svc, "p")
        claim_id = settled["claim_id"]

        full = svc.get_claim_view(COORD, claim_id=claim_id)
        self.assertEqual(full["disclosure"], "full")
        self.assertEqual(full["patient_ref"], "p")

        # 患者撤回授权：后续查询去标识化。
        svc.record_consent(COORD, patient_ref="p", disclosure_scope=[], granted=False)
        masked = svc.get_claim_view(COORD, claim_id=claim_id)
        self.assertEqual(masked["disclosure"], "deidentified")
        self.assertNotIn("patient_ref", masked)
        self.assertNotIn("hospital_id", masked)


if __name__ == "__main__":
    unittest.main()
