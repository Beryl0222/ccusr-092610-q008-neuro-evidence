"""从事件流重建的读模型，以及资格评估规则。

投影只依赖已落盘的事件；服务每次进入写锁区间都会用磁盘事件重建，
因此多进程并发下判断依据始终是最新真相，崩溃后重建也自然恢复。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from .storage import StoredEvent


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass
class ProductState:
    product_code: str
    product_name: str = ""
    registered: bool = False
    status: str | None = None  # LICENSED 等
    authority_ref: str | None = None
    scope: frozenset[str] = frozenset()
    required_evidence: frozenset[str] = frozenset()
    version: int = 0


@dataclass
class MappingState:
    mapping_id: str
    product_code: str
    service_code: str
    proposed_by: str | None = None
    approved_by: str | None = None
    revision: int = 0  # 0 表示尚未批准
    version: int = 0

    @property
    def approved(self) -> bool:
        return self.revision > 0


@dataclass
class PriceRevision:
    revision: int
    amount: float
    effective_from: datetime


@dataclass
class BookingState:
    booking_id: str
    patient_id: str
    hospital_id: str
    region: str
    service_code: str
    product_code: str
    indication: str
    scheduled_at: datetime
    trial_id: str | None
    product_version: int
    service_revision: int
    price_revision: int
    amount: float
    status: str = "ACTIVE"  # ACTIVE / BLOCKED / SETTLED
    reverifications: dict[str, str] = field(default_factory=dict)  # trigger -> PASSED/BLOCKED


@dataclass
class ClaimRecord:
    biz_key: str
    booking_id: str
    patient_id: str
    hospital_id: str
    region: str
    product_code: str
    service_code: str
    amount: float
    product_version: int
    service_revision: int
    price_revision: int
    settled_at: datetime
    event_id: str


@dataclass
class ImpactTask:
    task_id: str
    claim_biz_key: str
    trigger_event_id: str
    reason: str
    opened: bool = True
    decision: str | None = None
    affected_period: Mapping[str, Any] | None = None
    note: str | None = None
    reviewer: str | None = None


@dataclass
class Projection:
    products: dict[str, ProductState] = field(default_factory=dict)
    services: set[str] = field(default_factory=set)
    evidence: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    mappings: dict[str, MappingState] = field(default_factory=dict)
    prices: dict[str, list[PriceRevision]] = field(default_factory=dict)
    hospitals: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    trials: dict[str, dict[str, Any]] = field(default_factory=dict)
    bookings: dict[str, BookingState] = field(default_factory=dict)
    consents: dict[str, dict[str, Any]] = field(default_factory=dict)
    notices: dict[str, str] = field(default_factory=dict)
    claims: dict[str, ClaimRecord] = field(default_factory=dict)
    claims_by_booking: dict[str, str] = field(default_factory=dict)
    impact_tasks: dict[str, ImpactTask] = field(default_factory=dict)

    # ----- 重建 -----------------------------------------------------------

    @classmethod
    def rebuild(cls, events: list[StoredEvent]) -> "Projection":
        proj = cls()
        for event in events:
            proj.apply(event)
        return proj

    def apply(self, e: StoredEvent) -> None:
        p = e.payload
        t = e.event_type
        if t == "PRODUCT_REGISTERED":
            self.products[p["product_code"]] = ProductState(
                product_code=p["product_code"], product_name=p.get("product_name", ""),
                registered=True, version=e.version,
            )
        elif t == "STATUS_VERIFIED":
            product = self.products.setdefault(e.aggregate_id, ProductState(e.aggregate_id))
            product.registered = True
            product.status = p["status"]
            product.authority_ref = p["authority_ref"]
            product.scope = frozenset(p["scope"])
            product.required_evidence = frozenset(p.get("required_evidence", []))
            product.version = e.version
        elif t == "SCOPE_NARROWED":
            product = self.products[e.aggregate_id]
            product.scope = frozenset(p["scope"])
            product.required_evidence = frozenset(p.get("required_evidence", []))
            product.version = e.version
        elif t == "SERVICE_REGISTERED":
            self.services.add(p["service_code"])
        elif t == "EVIDENCE_SUMMARIZED":
            self.evidence.setdefault(p["product_code"], []).append(
                {"evidence_type": p["evidence_type"], "summary_ref": p["summary_ref"],
                 "supports": list(p.get("supports", []))}
            )
        elif t == "MAPPING_PROPOSED":
            key = f"{p['product_code']}:{p['service_code']}"
            mapping = self.mappings.get(key)
            if mapping is None:
                mapping = MappingState(key, p["product_code"], p["service_code"])
                self.mappings[key] = mapping
            mapping.proposed_by = p["proposed_by"]
            mapping.version = e.version
        elif t == "MAPPING_APPROVED":
            mapping = self.mappings[self._mapping_key_of(p["mapping_id"])]
            mapping.approved_by = p["approved_by"]
            mapping.revision = int(p["revision"])
            mapping.version = e.version
        elif t == "PRICE_PUBLISHED":
            key = f"{p['product_code']}:{p['service_code']}"
            revisions = self.prices.setdefault(key, [])
            revisions.append(
                PriceRevision(int(p["price_revision"]), float(p["amount"]), parse_ts(p["effective_from"]))
            )
            revisions.sort(key=lambda r: r.effective_from)
        elif t == "HOSPITAL_ADMITTED":
            self.hospitals[(p["hospital_id"], p["service_code"])] = {
                "hospital_id": p["hospital_id"], "service_code": p["service_code"],
                "region": p["region"], "admitted_by": p["admitted_by"],
            }
        elif t == "TRIAL_OPENED":
            self.trials[p["trial_id"]] = {
                "trial_id": p["trial_id"], "region": p["region"],
                "service_code": p["service_code"], "quota": int(p["quota"]),
                "allocations": [],
            }
        elif t == "TRIAL_ALLOCATED":
            self.trials[p["trial_id"]]["allocations"].append(p["booking_id"])
        elif t == "TRIAL_RELEASED":
            allocations = self.trials[p["trial_id"]]["allocations"]
            if p["booking_id"] in allocations:
                allocations.remove(p["booking_id"])
        elif t == "BOOKING_CREATED":
            self.bookings[p["booking_id"]] = BookingState(
                booking_id=p["booking_id"], patient_id=p["patient_id"],
                hospital_id=p["hospital_id"], region=p["region"],
                service_code=p["service_code"], product_code=p["product_code"],
                indication=p.get("indication", ""),
                scheduled_at=parse_ts(p["scheduled_at"]),
                trial_id=p.get("trial_id"),
                product_version=int(p["product_version"]),
                service_revision=int(p["service_revision"]),
                price_revision=int(p["price_revision"]),
                amount=float(p["amount"]),
            )
        elif t == "REVERIFICATION_FLAGGED":
            booking = self.bookings[p["booking_id"]]
            booking.reverifications.setdefault(p["trigger_event_id"], "FLAGGED")
        elif t == "REVERIFICATION_PASSED":
            booking = self.bookings[p["booking_id"]]
            booking.reverifications[p["trigger_event_id"]] = "PASSED"
            booking.status = "ACTIVE"
        elif t == "REVERIFICATION_BLOCKED":
            booking = self.bookings[p["booking_id"]]
            booking.reverifications[p["trigger_event_id"]] = "BLOCKED"
            booking.status = "BLOCKED"
        elif t == "CONSENT_RECORDED":
            self.consents[p["patient_id"]] = {
                "granted": bool(p["granted"]), "categories": frozenset(p.get("categories", [])),
            }
        elif t == "NOTICE_ACKNOWLEDGED":
            self.notices[p["patient_id"]] = p["notice_version"]
        elif t == "CLAIM_SETTLED":
            claim = ClaimRecord(
                biz_key=p["biz_key"], booking_id=p["booking_id"],
                patient_id=p["patient_id"], hospital_id=p["hospital_id"],
                region=p["region"], product_code=p["product_code"],
                service_code=p["service_code"], amount=float(p["amount"]),
                product_version=int(p["product_version"]),
                service_revision=int(p["service_revision"]),
                price_revision=int(p["price_revision"]),
                settled_at=parse_ts(e.occurred_at), event_id=e.event_id,
            )
            self.claims[claim.biz_key] = claim
            self.claims_by_booking[claim.booking_id] = claim.biz_key
            self.bookings[claim.booking_id].status = "SETTLED"
        elif t == "IMPACT_TASK_OPENED":
            self.impact_tasks[p["task_id"]] = ImpactTask(
                task_id=p["task_id"], claim_biz_key=p["claim_biz_key"],
                trigger_event_id=p["trigger_event_id"], reason=p["reason"],
            )
        elif t == "IMPACT_REVIEWED":
            task = self.impact_tasks[p["task_id"]]
            task.opened = False
            task.decision = p["decision"]
            task.affected_period = dict(p.get("affected_period", {}))
            task.note = p.get("note")
            task.reviewer = p.get("reviewer")

    @staticmethod
    def _mapping_key_of(mapping_id: str) -> str:
        return mapping_id

    # ----- 派生查询 -------------------------------------------------------

    def supported_evidence(self, product_code: str) -> frozenset[str]:
        kinds: set[str] = set()
        for item in self.evidence.get(product_code, []):
            kinds.update(item["supports"])
        return frozenset(kinds)

    def price_at(self, product_code: str, service_code: str, when: datetime) -> PriceRevision | None:
        """当时有效的价格版本：生效时间不晚于 ``when`` 的最高版本。"""
        revisions = [r for r in self.prices.get(f"{product_code}:{service_code}", [])
                     if r.effective_from <= when]
        return max(revisions, key=lambda r: r.revision, default=None)

    def evaluate(
        self,
        product_code: str,
        service_code: str,
        region: str,
        hospital_id: str | None,
        at_time: datetime,
        indication: str | None = None,
    ) -> dict[str, Any]:
        """返回逐项检查结果；reasons 非空即不可用，missing_evidence 单列证据缺口。"""
        checks: dict[str, Any] = {}
        reasons: list[str] = []
        missing: list[str] = []

        product = self.products.get(product_code)
        if product is None or not product.registered:
            checks["regulatory"] = "产品未登记"
            reasons.append(f"产品 {product_code} 尚未登记")
            product = None
        elif product.status != "LICENSED":
            checks["regulatory"] = f"监管状态={product.status or '未确认'}"
            reasons.append("监管状态尚未由授权人员确认许可")
        else:
            checks["regulatory"] = f"LICENSED @v{product.version} ({product.authority_ref})"

        if product is not None:
            if indication is not None and indication not in product.scope:
                checks["scope"] = f"适应证 {indication} 不在已确认范围"
                reasons.append(f"适应证 {indication} 不在当前适应范围 {sorted(product.scope)}")
            else:
                checks["scope"] = sorted(product.scope)
            gap = sorted(product.required_evidence - self.supported_evidence(product_code))
            missing = gap
            if gap:
                checks["evidence"] = f"缺少证据: {gap}"
                reasons.append(f"仍缺临床证据: {gap}")
            else:
                checks["evidence"] = "要求的证据均已摘要支持"

        mapping = self.mappings.get(f"{product_code}:{service_code}")
        if mapping is None or mapping.proposed_by is None:
            checks["mapping"] = "收费映射尚未提交"
            reasons.append("医疗服务项目收费映射尚未提交")
        elif not mapping.approved:
            checks["mapping"] = "已提交待批准"
            reasons.append("收费映射尚未经职责分离批准")
        else:
            checks["mapping"] = f"approved revision={mapping.revision}"

        if hospital_id is not None:
            access = self.hospitals.get((hospital_id, service_code))
            if access is None:
                checks["hospital"] = "医院未准入该服务"
                reasons.append(f"医院 {hospital_id} 尚未准入服务 {service_code}")
            elif access["region"] != region:
                checks["hospital"] = f"医院归属地区 {access['region']} 与查询地区 {region} 不符"
                reasons.append("医院准入地区与结算地区不一致")
            else:
                checks["hospital"] = "已准入"

        price = self.price_at(product_code, service_code, at_time)
        if price is None:
            checks["price"] = "当时无有效价格版本"
            reasons.append("结算时点没有生效的价格版本")
        else:
            checks["price"] = f"revision={price.revision} amount={price.amount}"

        return {
            "available": not reasons,
            "reasons": reasons,
            "missing_evidence": missing,
            "checks": checks,
            "service_revision": mapping.revision if mapping and mapping.approved else None,
            "price_revision": price.revision if price else None,
            "price": price,
            "product_version": product.version if product else None,
        }

    def open_impact_tasks(self) -> list[ImpactTask]:
        return [t for t in self.impact_tasks.values() if t.opened]

    def pending_reverifications(self) -> list[tuple[BookingState, str]]:
        """已标记但尚无结论的（booking, trigger），崩溃恢复时续写。"""
        pending: list[tuple[BookingState, str]] = []
        for booking in self.bookings.values():
            if booking.status == "SETTLED":
                continue
            for trigger, result in booking.reverifications.items():
                if result == "FLAGGED":
                    pending.append((booking, trigger))
        return pending
