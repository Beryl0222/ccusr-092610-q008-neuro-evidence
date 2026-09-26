"""纯领域策略：某地某服务为何可用 / 不可用、仍缺哪类证据。

本模块不接触数据库，输入是 world.World 投影，输出结构化判定，
便于单测与复用。所有"版本"语义集中在一处：
- 服务版本 = 映射最近一次批准的事件序号（service_revision）；
- 价格版本 = 结算时点之前生效的最新 PRICE_VERSION_EFFECTIVE（price_revision）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .world import World, _parse_dt

# 产品获批后进入可报销评估，至少需要的临床证据类别。
REQUIRED_EVIDENCE_KINDS: tuple[str, ...] = ("pivotal_trial",)


@dataclass
class GateReason:
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass
class EligibilityResult:
    available: bool
    passed: list[GateReason] = field(default_factory=list)
    blockers: list[GateReason] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)
    service_revision: int | None = None
    price_revision: int | None = None
    price_amount: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "passed": [r.as_dict() for r in self.passed],
            "blockers": [r.as_dict() for r in self.blockers],
            "missing_evidence": list(self.missing_evidence),
            "service_revision": self.service_revision,
            "price_revision": self.price_revision,
            "price_amount": self.price_amount,
        }


def evaluate(
    world: World,
    *,
    product_id: str,
    service_item_code: str,
    region: str,
    hospital_id: str,
    indication_id: str,
    at: datetime | str | None = None,
    required_evidence: tuple[str, ...] = REQUIRED_EVIDENCE_KINDS,
    quota_used: int | None = None,
    quota_self_held: bool = False,
) -> EligibilityResult:
    """逐道闸门评估可及性。

    每道闸门独立给出通过或阻断原因，调用方据此解释"为何可用、缺什么"。
    quota_used 为 None 时不检查名额余量；quota_self_held 表示该患者已
    占用一个名额，其本人的占位不计为超额（续约/重核验场景）。
    """
    if at is None:
        at_dt = datetime.now().astimezone()
    elif isinstance(at, str):
        at_dt = _parse_dt(at)
    else:
        at_dt = at

    passed: list[GateReason] = []
    blockers: list[GateReason] = []
    missing_evidence: list[str] = []

    product = world.products.get(product_id)
    if product is None or not product.verified:
        blockers.append(GateReason("status_unverified", "监管状态尚未由授权人员确认"))
    else:
        passed.append(GateReason("status_verified", f"监管状态已确认（{product.authority_ref}）"))

    if product is not None:
        if indication_id not in product.indications:
            blockers.append(GateReason("indication_undeclared", "适应范围未登记该适应症"))
        elif indication_id in product.removed_indications:
            blockers.append(GateReason("indication_removed", "该适应症已在范围收窄中移除"))
        else:
            passed.append(GateReason("indication_in_scope", "适应症在现行适用范围内"))

    have_kinds = set(world.evidence.get(product_id, {}).keys())
    for kind in required_evidence:
        if kind not in have_kinds:
            missing_evidence.append(kind)
    if missing_evidence:
        blockers.append(
            GateReason("evidence_incomplete", f"缺少临床证据类别：{', '.join(missing_evidence)}")
        )
    else:
        passed.append(GateReason("evidence_sufficient", "要求的临床证据摘要已齐备"))

    if service_item_code not in world.services:
        blockers.append(GateReason("service_unregistered", "收费项目尚未在医保服务项目目录登记"))
    else:
        passed.append(GateReason("service_registered", f"收费项目 {service_item_code} 已登记"))

    mapping = world.mappings.get((product_id, service_item_code))
    if mapping is None or not mapping.approved:
        blockers.append(GateReason("mapping_unapproved", "产品到收费项目的映射尚未批准"))
    else:
        passed.append(GateReason("mapping_approved", f"映射已批准（服务版本 {mapping.service_revision}）"))

    if (hospital_id, service_item_code) not in world.admissions:
        blockers.append(GateReason("hospital_not_admitted", "该院尚未准入该收费项目"))
    else:
        passed.append(GateReason("hospital_admitted", "医院已准入该收费项目"))

    trial = world.trials.get((region, service_item_code))
    if trial is None:
        blockers.append(GateReason("trial_not_open", "该地区尚未就此项目开展试点"))
    elif quota_used is not None:
        # 患者本人已占位时，不计入"他人占用"，避免本人续约被自己的名额挡住。
        occupied_by_others = quota_used - (1 if quota_self_held else 0)
        if occupied_by_others >= trial["quota_total"]:
            blockers.append(GateReason("quota_exhausted", "地区试点名额已用尽"))
        else:
            passed.append(
                GateReason(
                    "trial_available",
                    f"地区试点已开闸，名额 {trial['quota_total']}，剩余 "
                    f"{trial['quota_total'] - occupied_by_others}",
                )
            )
    else:
        passed.append(GateReason("trial_available", f"地区试点已开闸，名额 {trial['quota_total']}"))

    price = world.price_at(product_id, at_dt)
    if price is None:
        blockers.append(GateReason("price_not_effective", "该时点没有生效的价格版本"))
    else:
        passed.append(
            GateReason(
                "price_effective",
                f"价格版本 {price.price_revision} 已生效，金额 {price.amount:g}",
            )
        )

    return EligibilityResult(
        available=not blockers,
        passed=passed,
        blockers=blockers,
        missing_evidence=missing_evidence,
        service_revision=mapping.service_revision if mapping is not None and mapping.approved else None,
        price_revision=price.price_revision if price is not None else None,
        price_amount=price.amount if price is not None else None,
    )
