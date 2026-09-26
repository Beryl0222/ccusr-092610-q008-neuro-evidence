"""端到端走查：创新技术证据接力服务。

运行：
    PYTHONPATH=src python3 examples/walkthrough.py

覆盖：登记九类事实 → 可及性解释 → 名额并发 → 结算版本固定与幂等/冲突隔离
→ 证据/范围传播（未发生重核验、历史进影响评估）→ 进程恢复续跑 → 知情披露裁剪。
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from neuro_evidence.contracts import validate_event  # noqa: E402
from neuro_evidence.service import (  # noqa: E402
    Actor,
    EvidenceRelayService,
    PermissionDenied,
    PreconditionFailed,
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


def check_contracts(service: EvidenceRelayService) -> None:
    schema = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))
    events = service.store.list_events()
    bad = [(e["event_id"], validate_event({k: v for k, v in e.items() if k != "seq"}, schema))
           for e in events]
    bad = [(eid, issues) for eid, issues in bad if issues]
    if bad:
        for eid, issues in bad:
            print(f"  [契约失败] {eid}: {issues}")
        raise SystemExit(1)
    print(f"  契约校验：全部 {len(events)} 个事件符合 domain.schema.json")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "relay.db")
        svc = EvidenceRelayService(EventStore.connect(db))

        print("== 1. 产品获批 ≠ 全员可报销：先看缺什么 ==")
        svc.verify_status(AUTH, product_id=PRODUCT, authority_ref="NMPA-2026-BCI", scope="上肢康复")
        view = svc.explain_availability(
            product_id=PRODUCT, service_item_code=SVC, region=REGION,
            hospital_id=HOSP, indication_id=IND,
        )
        print("  available =", view["available"], "| 缺失证据 =", view["missing_evidence"])
        print("  阻断：", "；".join(b["message"] for b in view["blockers"]))

        print("== 2. 协调员补齐适应症/证据/项目/价格；医院准入；规则维护者映射（他人批准）==")
        svc.declare_indication(COORD, product_id=PRODUCT, indication_id=IND, label="脑卒中上肢功能障碍")
        svc.summarize_evidence(COORD, product_id=PRODUCT, evidence_kind="pivotal_trial", summary_ref="trial/A-307")
        svc.register_service(COORD, service_item_code=SVC, name="脑机接口上肢康复训练")
        svc.propose_mapping(RM_A, product_id=PRODUCT, service_item_code=SVC)
        try:
            svc.approve_mapping(RM_A, product_id=PRODUCT, service_item_code=SVC)
        except PermissionDenied as exc:  # 职责分离：提交人不能批准
            print("  自批被拒：", exc.message)
        svc.approve_mapping(RM_B, product_id=PRODUCT, service_item_code=SVC)
        past = (datetime.now().astimezone() - timedelta(days=30)).isoformat(timespec="seconds")
        svc.add_price_version(COORD, product_id=PRODUCT, price_revision=1, amount=1200.0, effective_from=past)
        svc.hospital_admit(HOSP_ACTOR, hospital_id=HOSP, service_item_code=SVC)
        svc.open_trial(COORD, region=REGION, service_item_code=SVC, quota_total=2)

        view = svc.explain_availability(
            product_id=PRODUCT, service_item_code=SVC, region=REGION,
            hospital_id=HOSP, indication_id=IND,
        )
        print("  补齐后 available =", view["available"],
              "| 服务版本 =", view["service_revision"], "价格版本 =", view["price_revision"])

        print("== 3. 知情 + 名额 + 申请 + 结算（版本时点固定）==")
        for patient in ("p-hist", "p-pend"):
            svc.record_consent(COORD, patient_ref=patient, disclosure_scope=["claim_detail"], granted=True)
            svc.grant_quota(COORD, region=REGION, service_item_code=SVC, patient_ref=patient)

        for patient in ("p-hist", "p-pend"):
            svc.request_service(COORD, region=REGION, hospital_id=HOSP, patient_ref=patient,
                                product_id=PRODUCT, service_item_code=SVC, indication_id=IND)
        settled = svc.settle(COORD, region=REGION, hospital_id=HOSP, patient_ref="p-hist", service_item_code=SVC)
        print("  首次结算：", json.dumps(settled["result"], ensure_ascii=False))
        replay = svc.settle(COORD, region=REGION, hospital_id=HOSP, patient_ref="p-hist", service_item_code=SVC)
        print("  重复请求按业务键回放：replayed =", replay["replayed"])

        print("== 4. 价格新版本生效：同业务键版本变化被隔离，历史结算不改写 ==")
        svc.add_price_version(COORD, product_id=PRODUCT, price_revision=2, amount=980.0,
                              effective_from=datetime.now().astimezone().isoformat(timespec="seconds"))
        try:
            svc.settle(COORD, region=REGION, hospital_id=HOSP, patient_ref="p-hist", service_item_code=SVC)
        except VersionConflict as exc:
            print("  冲突隔离：", exc.message)

        print("== 5. 范围收窄：未发生服务重新核验，历史结算进入影响评估 ==")
        out = svc.narrow_scope(COORD, product_id=PRODUCT, removed_indication_ids=[IND], reason="监管修订适用范围")
        print("  开立影响评估任务 #", out["impact_task_id"])
        pending = svc._world().claims["claim-330000-h-1-p-pend-MED-REHAB-BCI"]
        print("  未发生服务重核验 still_bookable =", pending.revalidations[-1]["still_bookable"])
        try:
            svc.settle(COORD, region=REGION, hospital_id=HOSP, patient_ref="p-pend", service_item_code=SVC)
        except PreconditionFailed as exc:
            print("  未通过核验的服务被拦在结算前：", exc.message[:48], "…")

        print("== 6. 模拟进程重启，恢复续跑影响评估 ==")
        restarted = EvidenceRelayService(EventStore.connect(db))
        reports = restarted.resume_impact_reviews(COORD, decide=lambda ctx: "recover_fund")
        print("  恢复后完成：", json.dumps(reports, ensure_ascii=False))

        print("== 7. 患者撤回知情授权：个人信息去标识化 ==")
        svc.record_consent(COORD, patient_ref="p-hist", disclosure_scope=[], granted=False)
        masked = svc.get_claim_view(COORD, claim_id=settled["claim_id"])
        print("  披露级别 =", masked["disclosure"], "| 含 patient_ref ?", "patient_ref" in masked)

        check_contracts(svc)
        print("走查完成。")


if __name__ == "__main__":
    main()
