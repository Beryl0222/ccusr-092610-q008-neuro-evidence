"""从事件流重建读模型。

投影是纯函数式折叠：给定同样的事件序列必然得到同样的世界状态，
服务每次在一个事务内重建投影后再做判定，因此读到的永远是已提交事实。
历史结算记录永不被覆盖式改写，只追加重核验/影响评估事件。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class ProductState:
    product_id: str
    verified: bool = False
    authority_ref: str | None = None
    scope: str | None = None
    indications: dict[str, str] = field(default_factory=dict)
    removed_indications: set[str] = field(default_factory=set)
    # 产品范围版本：状态确认、适应症登记、范围收窄都会推进。
    scope_revision: int = 0
    latest_event_id: str | None = None


@dataclass
class MappingState:
    product_id: str
    service_item_code: str
    approved: bool = False
    submitter: str | None = None
    approver: str | None = None
    proposed_event_id: str | None = None
    approved_event_id: str | None = None
    # 用批准事件序号充当服务映射版本，天然单调递增。
    service_revision: int = 0


@dataclass
class PricePoint:
    price_revision: int
    amount: float
    effective_from: datetime
    event_id: str
    seq: int


@dataclass
class ClaimState:
    claim_id: str
    requested: bool = False
    settled: bool = False
    product_revision: int | None = None
    service_revision: int | None = None
    price_revision: int | None = None
    product_id: str | None = None
    service_item_code: str | None = None
    region: str | None = None
    hospital_id: str | None = None
    patient_ref: str | None = None
    indication_id: str | None = None
    request_event_id: str | None = None
    requested_at: str | None = None
    settled_event_id: str | None = None
    settled_at: str | None = None
    revalidations: list[dict[str, Any]] = field(default_factory=list)
    impact_open: dict[str, Any] | None = None
    impact_reviews: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ConsentState:
    patient_ref: str
    granted: bool = False
    disclosure_scope: set[str] = field(default_factory=set)
    event_id: str | None = None
    recorded_at: str | None = None


class World:
    def __init__(self) -> None:
        self.products: dict[str, ProductState] = {}
        self.services: dict[str, str] = {}  # code -> name
        self.mappings: dict[tuple[str, str], MappingState] = {}
        self.prices: dict[str, list[PricePoint]] = {}
        self.evidence: dict[str, dict[str, dict[str, Any]]] = {}
        self.admissions: set[tuple[str, str]] = set()  # (hospital, code)
        self.trials: dict[tuple[str, str], dict[str, Any]] = {}
        self.claims: dict[str, ClaimState] = {}
        self.consents: dict[str, ConsentState] = {}

    # -- 折叠 ----------------------------------------------------------

    def fold(self, events: list[dict[str, Any]]) -> "World":
        for event in events:
            self._apply(event)
        return self

    def _product(self, product_id: str) -> ProductState:
        return self.products.setdefault(product_id, ProductState(product_id))

    def _apply(self, event: dict[str, Any]) -> None:
        etype = event["event_type"]
        payload = event["payload"]
        seq = event.get("seq", 0)
        handler = getattr(self, f"_on_{etype.lower()}", None)
        if handler is not None:
            handler(event, payload, seq)

    def _on_status_verified(self, event, p, seq) -> None:
        product = self._product(event["aggregate_id"])
        product.verified = True
        product.authority_ref = p["authority_ref"]
        product.scope = p["scope"]
        product.scope_revision += 1
        product.latest_event_id = event["event_id"]

    def _on_indication_declared(self, event, p, seq) -> None:
        product = self._product(p.get("product_id", event["aggregate_id"]))
        product.indications[p["indication_id"]] = p["label"]
        product.removed_indications.discard(p["indication_id"])
        product.scope_revision += 1
        product.latest_event_id = event["event_id"]

    def _on_service_registered(self, event, p, seq) -> None:
        self.services[p["service_item_code"]] = p["name"]

    def _on_mapping_proposed(self, event, p, seq) -> None:
        key = (p["product_id"], p["service_item_code"])
        mapping = self.mappings.get(key)
        if mapping is None:
            mapping = MappingState(p["product_id"], p["service_item_code"])
            self.mappings[key] = mapping
        mapping.approved = False
        mapping.approver = None
        mapping.approved_event_id = None
        mapping.submitter = p["submitter"]
        mapping.proposed_event_id = event["event_id"]

    def _on_mapping_approved(self, event, p, seq) -> None:
        # 聚合 id 约定为 "product_id|service_item_code"。
        product_id, code = event["aggregate_id"].split("|", 1)
        mapping = self.mappings[(product_id, code)]
        mapping.approved = True
        mapping.approver = p["approver"]
        mapping.approved_event_id = event["event_id"]
        mapping.service_revision = seq

    def _on_price_version_effective(self, event, p, seq) -> None:
        points = self.prices.setdefault(p["product_id"], [])
        points.append(
            PricePoint(
                price_revision=int(p["price_revision"]),
                amount=float(p["amount"]),
                effective_from=_parse_dt(p["effective_from"]),
                event_id=event["event_id"],
                seq=seq,
            )
        )
        points.sort(key=lambda pt: (pt.effective_from, pt.price_revision))

    def _on_evidence_summarized(self, event, p, seq) -> None:
        kinds = self.evidence.setdefault(p["product_id"], {})
        kinds[p["evidence_kind"]] = {
            "summary_ref": p["summary_ref"],
            "event_id": event["event_id"],
            "occurred_at": event["occurred_at"],
        }

    def _on_scope_narrowed(self, event, p, seq) -> None:
        product = self._product(p["product_id"])
        for indication_id in p.get("removed_indication_ids", []):
            product.removed_indications.add(indication_id)
        product.scope_revision += 1
        product.latest_event_id = event["event_id"]

    def _on_hospital_admitted(self, event, p, seq) -> None:
        self.admissions.add((p["hospital_id"], p["service_item_code"]))

    def _on_trial_opened(self, event, p, seq) -> None:
        self.trials[(p["region"], p["service_item_code"])] = {
            "quota_total": int(p["quota_total"]),
            "opened_event_id": event["event_id"],
        }

    def _on_trial_quota_granted(self, event, p, seq) -> None:
        # 占位计数以存储层唯一表为准；投影仅登记已发生事实。
        trial = self.trials.setdefault(
            (p["region"], p["service_item_code"]), {"quota_total": 0}
        )
        trial.setdefault("granted", []).append(p["patient_ref"])

    def _on_consent_recorded(self, event, p, seq) -> None:
        patient = self.consents.get(p["patient_ref"])
        if patient is None:
            patient = ConsentState(p["patient_ref"])
            self.consents[p["patient_ref"]] = patient
        patient.granted = bool(p["granted"])
        scope = p.get("disclosure_scope", [])
        patient.disclosure_scope = set(scope if isinstance(scope, list) else [scope])
        patient.event_id = event["event_id"]
        patient.recorded_at = event["occurred_at"]

    def _claim(self, claim_id: str) -> ClaimState:
        claim = self.claims.get(claim_id)
        if claim is None:
            claim = ClaimState(claim_id)
            self.claims[claim_id] = claim
        return claim

    def _on_claim_requested(self, event, p, seq) -> None:
        claim = self._claim(event["aggregate_id"])
        claim.requested = True
        claim.product_id = p["product_id"]
        claim.service_item_code = p["service_item_code"]
        claim.region = p["region"]
        claim.hospital_id = p["hospital_id"]
        claim.patient_ref = p["patient_ref"]
        claim.indication_id = p["indication_id"]
        claim.request_event_id = event["event_id"]
        claim.requested_at = event["occurred_at"]

    def _on_claim_settled(self, event, p, seq) -> None:
        claim = self._claim(event["aggregate_id"])
        claim.settled = True
        claim.product_revision = p.get("product_revision")
        claim.service_revision = int(p["service_revision"])
        claim.price_revision = int(p["price_revision"])
        # 上下文以 CLAIM_REQUESTED 为准；结算载荷也允许兜底带上。
        for attr in ("product_id", "service_item_code", "region",
                     "hospital_id", "patient_ref", "indication_id"):
            if p.get(attr) is not None:
                setattr(claim, attr, p[attr])
        claim.settled_event_id = event["event_id"]
        claim.settled_at = event["occurred_at"]

    def _on_claim_revalidated(self, event, p, seq) -> None:
        claim = self._claim(event["aggregate_id"])
        claim.revalidations.append(
            {
                "still_bookable": bool(p["still_bookable"]),
                "reason": p["reason"],
                "event_id": event["event_id"],
                "occurred_at": event["occurred_at"],
            }
        )

    def _on_impact_review_opened(self, event, p, seq) -> None:
        claim = self._claim(event["aggregate_id"])
        claim.impact_open = {
            "trigger_event_id": p["trigger_event_id"],
            "reason": p["reason"],
            "event_id": event["event_id"],
            "occurred_at": event["occurred_at"],
        }

    def _on_impact_reviewed(self, event, p, seq) -> None:
        claim = self._claim(event["aggregate_id"])
        claim.impact_reviews.append(
            {
                "affected_period": p["affected_period"],
                "decision": p["decision"],
                "event_id": event["event_id"],
                "occurred_at": event["occurred_at"],
            }
        )
        claim.impact_open = None

    # -- 查询辅助 ------------------------------------------------------

    def price_at(self, product_id: str, at: datetime) -> PricePoint | None:
        """返回 at 时点有效的价格版本（生效时间 <= at 中最新者）。"""
        candidate = None
        for point in self.prices.get(product_id, []):
            if point.effective_from <= at and (
                candidate is None
                or (point.effective_from, point.price_revision)
                > (candidate.effective_from, candidate.price_revision)
            ):
                candidate = point
        return candidate
