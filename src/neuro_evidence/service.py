"""创新技术证据接力服务。

在只追加事件存储之上承载全部业务规则：

- 角色授权：监管状态仅 authority 可确认；准入仅 hospital 可决定；
  医保规则维护者不能批准自己提交的收费映射（职责分离）。
- 版本时点固定：每次结算固定采用结算时点有效的产品范围、服务映射
  版本（service_revision）与价格版本（price_revision），事后价格
  调整不影响已固定版本。
- 业务键幂等：重复结算请求按业务键返回原结果；业务键相同但服务或
  价格版本变化时，视为冲突并隔离（ConflictingRetry），绝不覆盖原结果。
- 证据/范围传播：证据补充或范围收窄时，尚未发生（已申请未结算）的
  服务立即重新核验；历史结算不静默改写，而是开立影响评估任务，进程
  恢复后可继续处理。
- 名额：地区试点名额并发分配以唯一占位 + 余量闸门保证不超额。
- 知情：按患者授权范围限制个人信息披露。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping

from . import catalog
from .store import EventIdCollision, EventStore, utcnow_iso
from .world import World, _parse_dt

# ---------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------


class ServiceError(RuntimeError):
    """所有可预期业务拒绝的基类，携带稳定错误码。"""

    code = "service_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class PermissionDenied(ServiceError):
    code = "permission_denied"


class ValidationFailed(ServiceError):
    code = "validation_failed"


class VersionConflict(ServiceError):
    """业务键已存在，但版本指纹不同——隔离而非覆盖。"""

    code = "version_conflict"


class PreconditionFailed(ServiceError):
    code = "precondition_failed"


class QuotaExhausted(ServiceError):
    code = "quota_exhausted"


# ---------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class Actor:
    user_id: str
    role: str  # authority | hospital | rule_maintainer | coordinator


class EvidenceRelayService:
    def __init__(self, store: EventStore) -> None:
        self.store = store

    # -- 内部工具 ------------------------------------------------------

    def _world(self) -> World:
        return World().fold(self.store.list_events())

    @staticmethod
    def _now() -> datetime:
        return datetime.now().astimezone()

    def _append(
        self,
        *,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: Mapping[str, Any],
        actor: Actor,
        event_id: str | None = None,
        occurred_at: datetime | None = None,
        version: int | None = None,
    ) -> dict[str, Any]:
        """组装并追加事件；version 默认取该聚合已有事件数 + 1。"""
        if version is None:
            version = (
                self.store.count_events(
                    aggregate_type=aggregate_type, aggregate_id=aggregate_id
                )
                + 1
            )
        event = {
            "event_id": event_id or f"evt-{uuid.uuid4().hex[:16]}",
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": (occurred_at or self._now()).isoformat(timespec="seconds"),
            "version": version,
            "payload": dict(payload),
        }
        seq = self.store.append(event, actor=actor.user_id)
        event["seq"] = seq
        return event

    @staticmethod
    def _require_role(actor: Actor, role: str) -> None:
        if actor.role != role:
            raise PermissionDenied(f"该操作仅 {role} 可执行，当前角色为 {actor.role}")

    # -- 监管状态与适应范围 -------------------------------------------

    def verify_status(
        self,
        actor: Actor,
        *,
        product_id: str,
        authority_ref: str,
        scope: str,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_role(actor, "authority")
        with self.store.transaction():
            return self._append(
                event_type="STATUS_VERIFIED",
                aggregate_type="product_status",
                aggregate_id=product_id,
                payload={"authority_ref": authority_ref, "scope": scope},
                actor=actor,
                event_id=event_id,
            )

    def declare_indication(
        self,
        actor: Actor,
        *,
        product_id: str,
        indication_id: str,
        label: str,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        # 适应范围由协调员依据监管批件登记；至少要求产品已建档。
        self._require_role(actor, "coordinator")
        with self.store.transaction():
            return self._append(
                event_type="INDICATION_DECLARED",
                aggregate_type="product_status",
                aggregate_id=product_id,
                payload={
                    "product_id": product_id,
                    "indication_id": indication_id,
                    "label": label,
                },
                actor=actor,
                event_id=event_id,
            )

    def narrow_scope(
        self,
        actor: Actor,
        *,
        product_id: str,
        removed_indication_ids: list[str],
        reason: str,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """范围收窄：追加事件并同步传播（重核验未发生服务 + 历史进影响评估）。"""
        self._require_role(actor, "coordinator")
        with self.store.transaction():
            world = self._world()
            product = world.products.get(product_id)
            if product is None:
                raise PreconditionFailed("产品尚未建档，无法收窄范围")
            removed = [
                i for i in dict.fromkeys(removed_indication_ids)
                if i in product.indications and i not in product.removed_indications
            ]
            if not removed:
                raise PreconditionFailed("没有可收窄的现行适应症")
            event = self._append(
                event_type="SCOPE_NARROWED",
                aggregate_type="product_status",
                aggregate_id=product_id,
                payload={
                    "product_id": product_id,
                    "removed_indication_ids": removed,
                    "reason": reason,
                },
                actor=actor,
                event_id=event_id,
            )
            task_id = self._propagate(
                trigger=event,
                trigger_kind="scope_narrowed",
                product_id=product_id,
                affected_indication_ids=set(removed),
            )
        return {"event": event, "impact_task_id": task_id}

    # -- 服务项目与收费映射（职责分离） --------------------------------

    def register_service(
        self,
        actor: Actor,
        *,
        service_item_code: str,
        name: str,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_role(actor, "coordinator")
        with self.store.transaction():
            world = self._world()
            if service_item_code in world.services:
                raise PreconditionFailed("收费项目已登记")
            return self._append(
                event_type="SERVICE_REGISTERED",
                aggregate_type="service_catalog",
                aggregate_id=service_item_code,
                payload={"service_item_code": service_item_code, "name": name},
                actor=actor,
                event_id=event_id,
            )

    def propose_mapping(
        self,
        actor: Actor,
        *,
        product_id: str,
        service_item_code: str,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_role(actor, "rule_maintainer")
        with self.store.transaction():
            return self._append(
                event_type="MAPPING_PROPOSED",
                aggregate_type="service_mapping",
                aggregate_id=f"{product_id}|{service_item_code}",
                payload={
                    "product_id": product_id,
                    "service_item_code": service_item_code,
                    "submitter": actor.user_id,
                },
                actor=actor,
                event_id=event_id,
            )

    def approve_mapping(
        self,
        actor: Actor,
        *,
        product_id: str,
        service_item_code: str,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_role(actor, "rule_maintainer")
        with self.store.transaction():
            world = self._world()
            mapping = world.mappings.get((product_id, service_item_code))
            if mapping is None or mapping.submitter is None:
                raise PreconditionFailed("映射尚未被提交，无法批准")
            if mapping.submitter == actor.user_id:
                raise PermissionDenied("医保规则维护者不能批准自己提交的收费映射")
            if mapping.approved:
                raise PreconditionFailed("映射已批准")
            return self._append(
                event_type="MAPPING_APPROVED",
                aggregate_type="service_mapping",
                aggregate_id=f"{product_id}|{service_item_code}",
                payload={"approver": actor.user_id},
                actor=actor,
                event_id=event_id,
            )

    # -- 价格版本 ------------------------------------------------------

    def add_price_version(
        self,
        actor: Actor,
        *,
        product_id: str,
        price_revision: int,
        amount: float,
        effective_from: datetime | str,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_role(actor, "coordinator")
        effective_dt = (
            _parse_dt(effective_from) if isinstance(effective_from, str) else effective_from
        )
        with self.store.transaction():
            return self._append(
                event_type="PRICE_VERSION_EFFECTIVE",
                aggregate_type="price_version",
                aggregate_id=product_id,
                payload={
                    "product_id": product_id,
                    "price_revision": int(price_revision),
                    "amount": float(amount),
                    "effective_from": effective_dt.isoformat(timespec="seconds"),
                },
                actor=actor,
                event_id=event_id,
            )

    # -- 临床证据 ------------------------------------------------------

    def summarize_evidence(
        self,
        actor: Actor,
        *,
        product_id: str,
        evidence_kind: str,
        summary_ref: str,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """证据补充：登记摘要并传播（未发生服务重新核验）。"""
        self._require_role(actor, "coordinator")
        with self.store.transaction():
            world = self._world()
            if product_id not in world.products:
                raise PreconditionFailed("产品尚未建档")
            already = evidence_kind in world.evidence.get(product_id, {})
            if already:
                raise PreconditionFailed(f"证据类别 {evidence_kind} 已存在摘要")
            event = self._append(
                event_type="EVIDENCE_SUMMARIZED",
                aggregate_type="clinical_evidence",
                aggregate_id=product_id,
                payload={
                    "product_id": product_id,
                    "evidence_kind": evidence_kind,
                    "summary_ref": summary_ref,
                },
                actor=actor,
                event_id=event_id,
            )
            task_id = self._propagate(
                trigger=event, trigger_kind="evidence_added", product_id=product_id
            )
        return {"event": event, "impact_task_id": task_id}

    # -- 医院准入 ------------------------------------------------------

    def hospital_admit(
        self,
        actor: Actor,
        *,
        hospital_id: str,
        service_item_code: str,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_role(actor, "hospital")
        with self.store.transaction():
            world = self._world()
            if service_item_code not in world.services:
                raise PreconditionFailed("收费项目尚未登记，医院不能准入")
            if (hospital_id, service_item_code) in world.admissions:
                raise PreconditionFailed("该院已准入该项目")
            return self._append(
                event_type="HOSPITAL_ADMITTED",
                aggregate_type="hospital_access",
                aggregate_id=hospital_id,
                payload={
                    "hospital_id": hospital_id,
                    "service_item_code": service_item_code,
                },
                actor=actor,
                event_id=event_id,
            )

    # -- 地区试点与名额 ------------------------------------------------

    def open_trial(
        self,
        actor: Actor,
        *,
        region: str,
        service_item_code: str,
        quota_total: int,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_role(actor, "coordinator")
        if quota_total < 0:
            raise ValidationFailed("名额不能为负")
        with self.store.transaction():
            world = self._world()
            if (region, service_item_code) in world.trials:
                raise PreconditionFailed("该地区项目试点已开闸")
            return self._append(
                event_type="TRIAL_OPENED",
                aggregate_type="regional_trial",
                aggregate_id=f"{region}|{service_item_code}",
                payload={
                    "region": region,
                    "service_item_code": service_item_code,
                    "quota_total": int(quota_total),
                },
                actor=actor,
                event_id=event_id,
            )

    def grant_quota(
        self,
        actor: Actor,
        *,
        region: str,
        service_item_code: str,
        patient_ref: str,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """并发安全的名额分配：唯一占位 + 余量闸门，超额抛 QuotaExhausted。

        必须在 BEGIN IMMEDIATE 事务内执行，写锁把并发分配串行化，
        book_quota 的唯一约束与 quota_total 闸门共同保证不超额。
        """
        self._require_role(actor, "coordinator")
        with self.store.transaction():
            world = self._world()
            trial = world.trials.get((region, service_item_code))
            if trial is None:
                raise PreconditionFailed("该地区项目试点尚未开闸")
            used = self.store.quota_used(region=region, service_item_code=service_item_code)
            if used >= trial["quota_total"]:
                raise QuotaExhausted(
                    f"地区试点名额已用尽（{used}/{trial['quota_total']}）"
                )
            granted_event_id = event_id or f"evt-{uuid.uuid4().hex[:16]}"
            booked = self.store.book_quota(
                region=region,
                service_item_code=service_item_code,
                patient_ref=patient_ref,
                granted_event_id=granted_event_id,
            )
            if not booked:
                # 同一患者重复申请，幂等返回既有占位，不重复计数。
                return {"event": None, "duplicate": True, "patient_ref": patient_ref}
            event = self._append(
                event_type="TRIAL_QUOTA_GRANTED",
                aggregate_type="regional_trial",
                aggregate_id=f"{region}|{service_item_code}",
                payload={
                    "region": region,
                    "service_item_code": service_item_code,
                    "patient_ref": patient_ref,
                },
                actor=actor,
                event_id=granted_event_id,
            )
            return {"event": event, "duplicate": False}

    # -- 患者知情 ------------------------------------------------------

    def record_consent(
        self,
        actor: Actor,
        *,
        patient_ref: str,
        disclosure_scope: list[str],
        granted: bool,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_role(actor, "coordinator")
        with self.store.transaction():
            return self._append(
                event_type="CONSENT_RECORDED",
                aggregate_type="patient_consent",
                aggregate_id=patient_ref,
                payload={
                    "patient_ref": patient_ref,
                    "disclosure_scope": list(disclosure_scope),
                    "granted": bool(granted),
                },
                actor=actor,
                event_id=event_id,
            )

    # -- 可及性解释 ----------------------------------------------------

    def explain_availability(
        self,
        *,
        product_id: str,
        service_item_code: str,
        region: str,
        hospital_id: str,
        indication_id: str,
        at: datetime | str | None = None,
    ) -> dict[str, Any]:
        """说明某地某服务为何可用、仍缺哪类证据（只读，不检查名额余量）。"""
        world = self._world()
        result = catalog.evaluate(
            world,
            product_id=product_id,
            service_item_code=service_item_code,
            region=region,
            hospital_id=hospital_id,
            indication_id=indication_id,
            at=at,
        )
        return result.as_dict()

    # -- 服务申请与结算（版本固定 + 业务键幂等 + 冲突隔离） ------------

    @staticmethod
    def _settlement_key(
        *, region: str, hospital_id: str, patient_ref: str, service_item_code: str
    ) -> str:
        return "|".join(
            ["settle", region, hospital_id, patient_ref, service_item_code]
        )

    def request_service(
        self,
        actor: Actor,
        *,
        region: str,
        hospital_id: str,
        patient_ref: str,
        product_id: str,
        service_item_code: str,
        indication_id: str,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """登记尚未发生的服务申请；申请时逐闸门核验，含名额余量。"""
        self._require_role(actor, "coordinator")
        with self.store.transaction():
            world = self._world()
            used = self.store.quota_used(region=region, service_item_code=service_item_code)
            trial = world.trials.get((region, service_item_code)) or {}
            self_held = patient_ref in trial.get("granted", [])
            result = catalog.evaluate(
                world,
                product_id=product_id,
                service_item_code=service_item_code,
                region=region,
                hospital_id=hospital_id,
                indication_id=indication_id,
                quota_used=used,
                quota_self_held=self_held,
            )
            if not result.available:
                raise PreconditionFailed(
                    "服务申请未通过可及性闸门："
                    + "；".join(b.message for b in result.blockers)
                )
            if not self_held:
                raise PreconditionFailed("患者尚未取得该地区试点名额，不能登记服务")
            claim_id = f"claim-{region}-{hospital_id}-{patient_ref}-{service_item_code}"
            if claim_id in world.claims:
                raise PreconditionFailed("该服务申请已登记，结算前不可重复申请")
            event = self._append(
                event_type="CLAIM_REQUESTED",
                aggregate_type="claim_record",
                aggregate_id=claim_id,
                payload={
                    "product_id": product_id,
                    "service_item_code": service_item_code,
                    "region": region,
                    "hospital_id": hospital_id,
                    "patient_ref": patient_ref,
                    "indication_id": indication_id,
                },
                actor=actor,
                event_id=event_id,
            )
            return {"event": event, "claim_id": claim_id}

    def settle(
        self,
        actor: Actor,
        *,
        region: str,
        hospital_id: str,
        patient_ref: str,
        service_item_code: str,
        event_id: str | None = None,
        at: datetime | None = None,
    ) -> dict[str, Any]:
        """结算。版本时点固定、业务键幂等、版本变化冲突隔离。"""
        self._require_role(actor, "coordinator")
        business_key = self._settlement_key(
            region=region,
            hospital_id=hospital_id,
            patient_ref=patient_ref,
            service_item_code=service_item_code,
        )
        with self.store.transaction():
            world = self._world()
            claim_id = f"claim-{region}-{hospital_id}-{patient_ref}-{service_item_code}"
            claim = world.claims.get(claim_id)

            existing = self.store.get_idempotent(business_key)

            if existing is not None:
                # 重算当前时点应有版本，与首次结算固定的指纹比较。
                current = self._current_versions(world, claim, at)
                if existing["fingerprint"] != current["fingerprint"]:
                    raise VersionConflict(
                        "业务键已结算，但服务或价格版本已变化；"
                        f"原结果固定于 {existing['fingerprint']}，当前为 {current['fingerprint']}"
                    )
                # 键相同、版本相同：返回原结果，绝不重复结算。
                return {
                    "replayed": True,
                    "claim_id": existing["claim_id"],
                    "result": existing["result"],
                }

            if claim is None or not claim.requested:
                raise PreconditionFailed("尚未登记服务申请，不能结算")
            if claim.settled:
                raise PreconditionFailed("该服务已结算")
            latest = claim.revalidations[-1] if claim.revalidations else None
            if latest is not None and not latest["still_bookable"]:
                raise PreconditionFailed(
                    "范围/证据更新后的重新核验未通过，服务不得继续结算：" + latest["reason"]
                )

            versions = self._current_versions(world, claim, at)
            settled_event = self._append(
                event_type="CLAIM_SETTLED",
                aggregate_type="claim_record",
                aggregate_id=claim_id,
                payload={
                    "product_revision": versions["product_revision"],
                    "service_revision": versions["service_revision"],
                    "price_revision": versions["price_revision"],
                    "amount": versions["amount"],
                },
                actor=actor,
                event_id=event_id,
            )
            result = {
                "claim_id": claim_id,
                "product_revision": versions["product_revision"],
                "service_revision": versions["service_revision"],
                "price_revision": versions["price_revision"],
                "amount": versions["amount"],
                "settled_event_id": settled_event["event_id"],
                "settled_at": settled_event["occurred_at"],
            }
            self.store.save_idempotent(
                business_key=business_key,
                outcome_kind="claim_settled",
                claim_id=claim_id,
                fingerprint=versions["fingerprint"],
                result=result,
                created_seq=settled_event["seq"],
                created_at=settled_event["occurred_at"],
            )
            return {"replayed": False, "claim_id": claim_id, "result": result}

    @staticmethod
    def _current_versions(
        world: World, claim: Any, at: datetime | None
    ) -> dict[str, Any]:
        """计算结算时点应固定的服务/价格版本指纹。"""
        at_dt = at or datetime.now().astimezone()
        product = world.products.get(claim.product_id)
        mapping = world.mappings.get((claim.product_id, claim.service_item_code))
        price = world.price_at(claim.product_id, at_dt)
        if product is None or not product.verified:
            raise PreconditionFailed("产品监管状态未确认，无法固定产品版本")
        if mapping is None or not mapping.approved:
            raise PreconditionFailed("映射未批准，无法固定服务版本")
        if price is None:
            raise PreconditionFailed("结算时点没有生效价格版本")
        product_revision = product.scope_revision
        service_revision = mapping.service_revision
        price_revision = price.price_revision
        fingerprint = f"prod={product_revision};svc={service_revision};price={price_revision}"
        return {
            "product_revision": product_revision,
            "service_revision": service_revision,
            "price_revision": price_revision,
            "amount": price.amount,
            "fingerprint": fingerprint,
        }

    # -- 传播：重核验未发生服务 + 历史结算进影响评估 -------------------

    def _propagate(
        self,
        *,
        trigger: dict[str, Any],
        trigger_kind: str,
        product_id: str,
        affected_indication_ids: set[str] | None = None,
    ) -> int | None:
        """在已追加触发事件的同一事务内调用。

        - 已申请、未结算：追加 CLAIM_REVALIDATED（still_bookable 按当前闸门判定）；
        - 已结算：开立影响评估任务（稍后逐单恢复处理，不静默改写）。
        仅当存在受影响的历史结算时才建任务。
        """
        world = self._world()  # 重建，包含刚追加的触发事件
        affected_indication_ids = affected_indication_ids or set()
        settled_affected: list[str] = []

        for claim_id, claim in world.claims.items():
            if claim.product_id != product_id:
                continue
            if affected_indication_ids and claim.indication_id not in affected_indication_ids:
                continue
            if claim.settled:
                settled_affected.append(claim_id)
                continue
            if not claim.requested:
                continue
            # 尚未发生（已申请未结算）：立即按当前事实重新核验。
            granted = (
                world.trials.get((claim.region, claim.service_item_code), {}).get("granted", [])
            )
            result = catalog.evaluate(
                world,
                product_id=claim.product_id,
                service_item_code=claim.service_item_code,
                region=claim.region,
                hospital_id=claim.hospital_id,
                indication_id=claim.indication_id,
                quota_used=self.store.quota_used(
                    region=claim.region, service_item_code=claim.service_item_code
                ),
                quota_self_held=claim.patient_ref in granted,
            )
            reason = (
                "证据/范围更新后仍满足全部闸门"
                if result.available
                else "证据/范围更新后不再满足："
                + "；".join(b.message for b in result.blockers)
            )
            self._append(
                event_type="CLAIM_REVALIDATED",
                aggregate_type="claim_record",
                aggregate_id=claim_id,
                payload={
                    "still_bookable": result.available,
                    "reason": reason,
                    "trigger_event_id": trigger["event_id"],
                    "missing_evidence": result.missing_evidence,
                },
                actor=Actor("system", "coordinator"),
            )

        if not settled_affected:
            return None

        task_id = self.store.create_impact_task(
            trigger_event_id=trigger["event_id"],
            trigger_kind=trigger_kind,
            product_id=product_id,
            opened_at=utcnow_iso(),
        )
        reason_text = (
            f"范围收窄移除适应症 {sorted(affected_indication_ids)}"
            if trigger_kind == "scope_narrowed"
            else "关键临床证据补充，需复核历史结算依据"
        )
        for claim_id in settled_affected:
            self._append(
                event_type="IMPACT_REVIEW_OPENED",
                aggregate_type="claim_record",
                aggregate_id=claim_id,
                payload={
                    "trigger_event_id": trigger["event_id"],
                    "reason": reason_text,
                    "impact_task_id": task_id,
                },
                actor=Actor("system", "coordinator"),
            )
        return task_id

    # -- 影响评估恢复续跑 ----------------------------------------------

    def resume_impact_reviews(
        self,
        actor: Actor,
        *,
        decide: Callable[[dict[str, Any]], str] | None = None,
    ) -> list[dict[str, Any]]:
        """进程恢复后捞出所有 open 影响评估任务继续处理。

        decide 接收任务上下文，返回决策（默认 keep_as_settled）：
        - keep_as_settled：历史结算有效，留痕但不追溯；
        - recover_fund / deny_reimbursement：标记需基金追回或拒付线索，
          具体追付仍走线下流程，本系统不改写历史结算。
        """
        self._require_role(actor, "coordinator")
        completed: list[dict[str, Any]] = []
        # 每个任务独立事务，单个失败不影响其他任务续跑。
        for task in self.store.list_open_impact_tasks():
            with self.store.transaction():
                world = self._world()
                affected = [
                    cid
                    for cid, claim in world.claims.items()
                    if claim.product_id == task["product_id"]
                    and claim.impact_open is not None
                    and claim.impact_open["trigger_event_id"] == task["trigger_event_id"]
                ]
                decision = (
                    decide({"task": task, "affected_claims": affected})
                    if decide is not None
                    else "keep_as_settled"
                )
                period = self._affected_period(
                    world, task["trigger_event_id"], affected
                )
                for claim_id in affected:
                    self._append(
                        event_type="IMPACT_REVIEWED",
                        aggregate_type="claim_record",
                        aggregate_id=claim_id,
                        payload={
                            "affected_period": period,
                            "decision": decision,
                            "trigger_event_id": task["trigger_event_id"],
                        },
                        actor=actor,
                    )
                report = {
                    "task_id": task["task_id"],
                    "trigger_event_id": task["trigger_event_id"],
                    "affected_claims": affected,
                    "decision": decision,
                    "affected_period": period,
                }
                self.store.finish_impact_task(
                    task_id=task["task_id"], finished_at=utcnow_iso(), report=report
                )
                completed.append(report)
        return completed

    def _affected_period(
        self, world: World, trigger_event_id: str, claim_ids: list[str]
    ) -> dict[str, str]:
        """受影响期间：相关历史结算最早发生时间 至 触发事件生效时间。"""
        settled_times = sorted(
            c.settled_at for c in (world.claims[i] for i in claim_ids) if c.settled_at
        )
        trigger = self.store.get_event(trigger_event_id)
        return {
            "start": settled_times[0] if settled_times else "",
            "end": trigger["occurred_at"] if trigger else "",
        }

    # -- 按知情授权披露 ------------------------------------------------

    def get_claim_view(
        self, actor: Actor, *, claim_id: str
    ) -> dict[str, Any]:
        """按患者知情授权裁剪个人信息。

        disclosure_scope 含 'claim_detail' 才返回患者标识等个人字段；
        否则只返回去标识化的结算/评估事实。无授权记录一律最小披露。
        """
        world = self._world()
        claim = world.claims.get(claim_id)
        if claim is None:
            raise PreconditionFailed("结算单不存在")

        base = {
            "claim_id": claim.claim_id,
            "product_id": claim.product_id,
            "service_item_code": claim.service_item_code,
            "region": claim.region,
            "indication_id": claim.indication_id,
            "settled": claim.settled,
            "product_revision": claim.product_revision,
            "service_revision": claim.service_revision,
            "price_revision": claim.price_revision,
        }
        consent = world.consents.get(claim.patient_ref) if claim.patient_ref else None
        allowed = bool(consent and consent.granted and "claim_detail" in consent.disclosure_scope)
        if allowed:
            base.update(
                {
                    "patient_ref": claim.patient_ref,
                    "hospital_id": claim.hospital_id,
                    "settled_at": claim.settled_at,
                    "revalidations": claim.revalidations,
                    "impact_open": claim.impact_open,
                    "impact_reviews": claim.impact_reviews,
                    "disclosure": "full",
                }
            )
        else:
            base["disclosure"] = "deidentified"
        return base
