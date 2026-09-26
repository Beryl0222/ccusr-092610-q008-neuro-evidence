"""创新技术证据接力服务。

职责要点（与 docs/service.md 对应）：

- 监管状态只能由授权角色确认；医院自行决定服务准入；
  医保规则维护者可以提交收费映射，但不能批准自己提交的映射。
- 预约成立时固化产品版本、服务映射修订号与价格修订号；结算只认固化值。
- 结算按业务键幂等：同键返回原结果；同键但版本组合变化则隔离报错。
- 证据补充 / 范围收窄：未发生的服务重新核验（通过或阻断），
  已结算历史只开影响评估任务，绝不静默改写。
- 地区试点名额在文件锁内"检查—占用"原子完成，并发不超额。
- 进程重启后 :meth:`resume` 续写未完成的重验，并列出待决影响评估。
- 个人信息披露按患者授权类别裁剪。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from .errors import (
    IdempotencyIsolation,
    NotFoundError,
    PreconditionFailed,
    QuotaExhaustedError,
    UnauthorizedError,
)
from .projection import Projection, parse_ts
from .storage import EventStore, StoredEvent

AUTHORITY = "AUTHORITY"                # 监管授权确认人员
HOSPITAL_ADMIN = "HOSPITAL_ADMIN"    # 医院准入决定者
RULE_MAINTAINER = "RULE_MAINTAINER"  # 医保规则维护者
EVIDENCE_SUBMITTER = "EVIDENCE_SUBMITTER"  # 临床证据提交方
COORDINATOR = "COORDINATOR"          # 项目协调员
PATIENT = "PATIENT"                  # 患者本人

NOTICE_VERSION = "notice-v1"  # 当前患者知情说明版本


@dataclass(frozen=True)
class Actor:
    actor_id: str
    role: str
    hospital_id: str | None = None


@dataclass
class CommandResult:
    events: list[StoredEvent] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def event_ids(self) -> list[str]:
        return [e.event_id for e in self.events]


class EvidenceRelayService:
    def __init__(self, store: EventStore) -> None:
        self.store = store

    # ----- 内部工具 -------------------------------------------------------

    def _rebuild(self) -> Projection:
        return Projection.rebuild(self.store.load())

    @staticmethod
    def _require(actor: Actor, action: str, *roles: str) -> None:
        if actor.role not in roles:
            raise UnauthorizedError(actor.actor_id, actor.role, action)

    @staticmethod
    def _must_exist(condition: bool, message: str) -> None:
        if not condition:
            raise NotFoundError(message)

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:12]}"

    # ----- 产品与监管 -----------------------------------------------------

    def register_product(self, actor: Actor, product_code: str, product_name: str) -> CommandResult:
        self._require(actor, "register_product", AUTHORITY, RULE_MAINTAINER, COORDINATOR)
        with self.store.write_lock():
            proj = self._rebuild()
            if product_code in proj.products and proj.products[product_code].registered:
                return CommandResult(data={"product_code": product_code, "dup": True})
            event = self.store.append(
                f"product:{product_code}", "PRODUCT_REGISTERED",
                "product_catalog", product_code,
                {"product_code": product_code, "product_name": product_name,
                 "registered_by": actor.actor_id},
            )
            return CommandResult([event], {"product_code": product_code})

    def verify_status(
        self, actor: Actor, product_code: str, authority_ref: str,
        scope: list[str] | frozenset[str], required_evidence: list[str] | frozenset[str],
        status: str = "LICENSED",
    ) -> CommandResult:
        """监管状态确认；仅授权角色可执行。"""
        self._require(actor, "verify_status", AUTHORITY)
        if not authority_ref.strip():
            raise PreconditionFailed(["authority_ref 不能为空"])
        with self.store.write_lock():
            proj = self._rebuild()
            self._must_exist(product_code in proj.products, f"产品 {product_code} 尚未登记")
            version = proj.products[product_code].version + 1
            event = self.store.append(
                f"status:{product_code}:v{version}", "STATUS_VERIFIED",
                "product_status", product_code,
                {"authority_ref": authority_ref, "scope": sorted(scope), "status": status,
                 "required_evidence": sorted(required_evidence), "verified_by": actor.actor_id},
            )
            side_effects = self._propagate_product_change(proj, event)
            return CommandResult([event, *side_effects], {"product_version": event.version})

    def narrow_scope(
        self, actor: Actor, product_code: str, scope: list[str] | frozenset[str],
        required_evidence: list[str] | frozenset[str], reason: str,
    ) -> CommandResult:
        """适应范围收窄（也可据此调整证据要求）。"""
        self._require(actor, "narrow_scope", AUTHORITY)
        if not reason.strip():
            raise PreconditionFailed(["范围收窄必须给出 reason"])
        with self.store.write_lock():
            proj = self._rebuild()
            product = proj.products.get(product_code)
            self._must_exist(product is not None and product.status == "LICENSED",
                            f"产品 {product_code} 尚未确认许可，不能收窄范围")
            new_scope = frozenset(scope)
            if not new_scope <= product.scope:
                raise PreconditionFailed(
                    [f"收窄后的范围 {sorted(new_scope)} 超出原范围 {sorted(product.scope)}"])
            version = product.version + 1
            event = self.store.append(
                f"scope:{product_code}:v{version}", "SCOPE_NARROWED",
                "product_status", product_code,
                {"scope": sorted(new_scope), "required_evidence": sorted(required_evidence),
                 "reason": reason, "changed_by": actor.actor_id},
            )
            side_effects = self._propagate_product_change(proj, event)
            return CommandResult([event, *side_effects], {"product_version": event.version})

    # ----- 服务、证据与收费映射 -------------------------------------------

    def register_service(self, actor: Actor, service_code: str, service_name: str) -> CommandResult:
        self._require(actor, "register_service", RULE_MAINTAINER, COORDINATOR)
        with self.store.write_lock():
            proj = self._rebuild()
            if service_code in proj.services:
                return CommandResult(data={"service_code": service_code, "dup": True})
            event = self.store.append(
                f"service:{service_code}", "SERVICE_REGISTERED",
                "service_catalog", service_code,
                {"service_code": service_code, "service_name": service_name},
            )
            return CommandResult([event], {"service_code": service_code})

    def summarize_evidence(
        self, actor: Actor, product_code: str, evidence_type: str,
        summary_ref: str, supports: list[str] | frozenset[str],
    ) -> CommandResult:
        """登记临床证据摘要；supports 声明该证据满足了哪些证据类别。"""
        self._require(actor, "summarize_evidence", EVIDENCE_SUBMITTER, AUTHORITY, COORDINATOR)
        if not supports:
            raise PreconditionFailed(["证据摘要必须声明 supports 的证据类别"])
        with self.store.write_lock():
            proj = self._rebuild()
            self._must_exist(product_code in proj.products, f"产品 {product_code} 尚未登记")
            event = self.store.append(
                self._new_id("evidence"), "EVIDENCE_SUMMARIZED",
                "evidence_ledger", product_code,
                {"product_code": product_code, "evidence_type": evidence_type,
                 "summary_ref": summary_ref, "supports": sorted(supports),
                 "submitted_by": actor.actor_id},
            )
            side_effects = self._propagate_product_change(proj, event)
            return CommandResult([event, *side_effects])

    @staticmethod
    def mapping_id(product_code: str, service_code: str) -> str:
        return f"{product_code}:{service_code}"

    def propose_mapping(self, actor: Actor, product_code: str, service_code: str) -> CommandResult:
        """提交收费映射：医保规则维护者的提交动作。"""
        self._require(actor, "propose_mapping", RULE_MAINTAINER)
        mapping_id = self.mapping_id(product_code, service_code)
        with self.store.write_lock():
            proj = self._rebuild()
            self._must_exist(product_code in proj.products, f"产品 {product_code} 尚未登记")
            self._must_exist(service_code in proj.services, f"服务项目 {service_code} 尚未登记")
            existing = proj.mappings.get(mapping_id)
            if existing is not None and existing.proposed_by is not None:
                # 映射已在待批/已批状态：重复提交幂等返回，提交者事实不被覆盖。
                return CommandResult(data={
                    "mapping_id": mapping_id, "dup": True,
                    "proposed_by": existing.proposed_by, "revision": existing.revision})
            event = self.store.append(
                f"mapping-proposed:{mapping_id}", "MAPPING_PROPOSED",
                "service_mapping", mapping_id,
                {"product_code": product_code, "service_code": service_code,
                 "proposed_by": actor.actor_id},
            )
            return CommandResult([event], {"mapping_id": mapping_id})

    def approve_mapping(self, actor: Actor, product_code: str, service_code: str) -> CommandResult:
        """批准收费映射：维护者可批准，但不能批准自己提交的映射。"""
        self._require(actor, "approve_mapping", RULE_MAINTAINER)
        mapping_id = self.mapping_id(product_code, service_code)
        with self.store.write_lock():
            proj = self._rebuild()
            mapping = proj.mappings.get(mapping_id)
            self._must_exist(mapping is not None and mapping.proposed_by,
                            f"映射 {mapping_id} 尚未提交，无法批准")
            if mapping.proposed_by == actor.actor_id:
                raise PreconditionFailed(
                    [f"职责分离：{actor.actor_id} 不能批准自己提交的映射 {mapping_id}"])
            revision = mapping.revision + 1
            event = self.store.append(
                f"mapping-approved:{mapping_id}:r{revision}", "MAPPING_APPROVED",
                "service_mapping", mapping_id,
                {"mapping_id": mapping_id, "approved_by": actor.actor_id, "revision": revision},
            )
            return CommandResult([event], {"mapping_id": mapping_id, "revision": revision})

    def publish_price(
        self, actor: Actor, product_code: str, service_code: str,
        amount: float, effective_from: datetime | str,
    ) -> CommandResult:
        """发布新价格版本；版本号严格递增，生效时间带时区。"""
        self._require(actor, "publish_price", RULE_MAINTAINER, AUTHORITY)
        if isinstance(effective_from, datetime):
            if effective_from.tzinfo is None:
                raise PreconditionFailed(["effective_from 必须带时区"])
            effective_iso = effective_from.isoformat()
        else:
            effective_iso = str(effective_from)
            if parse_ts(effective_iso).tzinfo is None:
                raise PreconditionFailed(["effective_from 必须带时区"])
        if float(amount) < 0:
            raise PreconditionFailed(["价格不能为负"])
        mapping_id = self.mapping_id(product_code, service_code)
        with self.store.write_lock():
            proj = self._rebuild()
            self._must_exist(mapping_id in proj.mappings and proj.mappings[mapping_id].approved,
                            f"映射 {mapping_id} 未经批准，不能发布价格")
            revisions = proj.prices.get(mapping_id, [])
            price_revision = (max((r.revision for r in revisions), default=0)) + 1
            new_effective = parse_ts(effective_iso)
            if any(r.effective_from == new_effective for r in revisions):
                raise PreconditionFailed([f"已存在生效时间 {effective_iso} 的价格版本"])
            event = self.store.append(
                f"price:{mapping_id}:r{price_revision}", "PRICE_PUBLISHED",
                "price_book", mapping_id,
                {"product_code": product_code, "service_code": service_code,
                 "price_revision": price_revision, "amount": float(amount),
                 "effective_from": effective_iso, "published_by": actor.actor_id},
            )
            return CommandResult([event], {"price_revision": price_revision})

    # ----- 医院准入与地区试点 ---------------------------------------------

    def admit_hospital(
        self, actor: Actor, hospital_id: str, service_code: str, region: str,
    ) -> CommandResult:
        """医院决定是否准入某服务；只允许本院管理员操作自己的医院。"""
        self._require(actor, "admit_hospital", HOSPITAL_ADMIN)
        if actor.hospital_id not in (None, hospital_id):
            raise UnauthorizedError(actor.actor_id, actor.role, f"准入其他医院 {hospital_id}")
        with self.store.write_lock():
            proj = self._rebuild()
            self._must_exist(service_code in proj.services, f"服务项目 {service_code} 尚未登记")
            if (hospital_id, service_code) in proj.hospitals:
                return CommandResult(data={"hospital_id": hospital_id, "dup": True})
            event = self.store.append(
                f"admit:{hospital_id}:{service_code}", "HOSPITAL_ADMITTED",
                "hospital_access", f"{hospital_id}:{service_code}",
                {"hospital_id": hospital_id, "service_code": service_code, "region": region,
                 "admitted_by": actor.actor_id},
            )
            return CommandResult([event])

    def open_trial(
        self, actor: Actor, trial_id: str, region: str, service_code: str, quota: int,
    ) -> CommandResult:
        self._require(actor, "open_trial", AUTHORITY, COORDINATOR)
        if int(quota) <= 0:
            raise PreconditionFailed(["试点名额必须为正整数"])
        with self.store.write_lock():
            proj = self._rebuild()
            if trial_id in proj.trials:
                raise PreconditionFailed([f"试点 {trial_id} 已开设"])
            event = self.store.append(
                f"trial-open:{trial_id}", "TRIAL_OPENED",
                "regional_trial", trial_id,
                {"trial_id": trial_id, "region": region, "service_code": service_code,
                 "quota": int(quota), "opened_by": actor.actor_id},
            )
            return CommandResult([event])

    # ----- 患者授权与知情 -------------------------------------------------

    def record_consent(
        self, actor: Actor, patient_id: str, granted: bool, categories: list[str],
    ) -> CommandResult:
        # 患者本人登记自己的授权，或由协调员协助登记。
        self._require(actor, "record_consent", PATIENT, COORDINATOR)
        if actor.role == PATIENT and actor.actor_id != patient_id:
            raise UnauthorizedError(actor.actor_id, actor.role, "代为登记他人授权")
        with self.store.write_lock():
            event = self.store.append(
                self._new_id("consent"), "CONSENT_RECORDED",
                "patient_consent", patient_id,
                {"patient_id": patient_id, "granted": bool(granted),
                 "categories": sorted(categories), "recorded_by": actor.actor_id},
            )
            return CommandResult([event])

    def acknowledge_notice(self, actor: Actor, patient_id: str) -> CommandResult:
        """患者确认已知悉创新技术知情说明。"""
        self._require(actor, "acknowledge_notice", PATIENT)
        if actor.actor_id != patient_id:
            raise UnauthorizedError(actor.actor_id, actor.role, "代签患者知情说明")
        with self.store.write_lock():
            event = self.store.append(
                self._new_id("notice"), "NOTICE_ACKNOWLEDGED",
                "patient_consent", patient_id,
                {"patient_id": patient_id, "notice_version": NOTICE_VERSION},
            )
            return CommandResult([event])

    # ----- 预约（成立时固化版本 + 名额原子占用） ---------------------------

    def create_booking(
        self, actor: Actor, booking_id: str, patient_id: str, hospital_id: str,
        region: str, product_code: str, service_code: str, indication: str,
        scheduled_at: datetime | str, trial_id: str | None = None,
    ) -> CommandResult:
        self._require(actor, "create_booking", COORDINATOR, HOSPITAL_ADMIN)
        if isinstance(scheduled_at, datetime):
            scheduled_iso = scheduled_at.isoformat()
            scheduled_dt = scheduled_at
        else:
            scheduled_iso = str(scheduled_at)
            scheduled_dt = parse_ts(scheduled_iso)
        if scheduled_dt.tzinfo is None:
            raise PreconditionFailed(["scheduled_at 必须带时区"])

        with self.store.write_lock():
            proj = self._rebuild()
            if booking_id in proj.bookings:
                raise PreconditionFailed([f"预约 {booking_id} 已存在"])
            evaluation = proj.evaluate(
                product_code, service_code, region, hospital_id, scheduled_dt, indication
            )
            reasons = list(evaluation["reasons"])

            consent = proj.consents.get(patient_id)
            if consent is None or not consent["granted"]:
                reasons.append("患者尚未给出有效授权")
            if proj.notices.get(patient_id) != NOTICE_VERSION:
                reasons.append(f"患者尚未确认知情说明 {NOTICE_VERSION}")
            if reasons:
                raise PreconditionFailed(reasons)

            trial = None
            if trial_id is not None:
                trial = proj.trials.get(trial_id)
                self._must_exist(trial is not None, f"试点 {trial_id} 不存在")
                assert trial is not None
                if trial["region"] != region or trial["service_code"] != service_code:
                    raise PreconditionFailed(
                        [f"试点 {trial_id} 的地区/服务与本预约不匹配"])
                # 检查—占用在同一写锁区间内：并发分配不会超额。
                if len(trial["allocations"]) >= trial["quota"]:
                    raise QuotaExhaustedError(trial_id, trial["quota"])

            # 名额占用与预约成立在同一次落盘里完成，崩溃不会留下孤儿名额。
            records: list[Mapping[str, Any]] = []
            if trial is not None:
                records.append({
                    "event_id": f"alloc:{trial_id}:{booking_id}",
                    "event_type": "TRIAL_ALLOCATED",
                    "aggregate_type": "regional_trial",
                    "aggregate_id": trial_id,
                    "payload": {"trial_id": trial_id, "booking_id": booking_id},
                })
            records.append({
                "event_id": f"booking:{booking_id}",
                "event_type": "BOOKING_CREATED",
                "aggregate_type": "patient_booking",
                "aggregate_id": booking_id,
                "payload": {
                    "booking_id": booking_id, "patient_id": patient_id,
                    "hospital_id": hospital_id, "region": region,
                    "product_code": product_code, "service_code": service_code,
                    "indication": indication, "scheduled_at": scheduled_iso,
                    "trial_id": trial_id,
                    "product_version": evaluation["product_version"],
                    "service_revision": evaluation["service_revision"],
                    "price_revision": evaluation["price_revision"],
                    "amount": evaluation["price"].amount,
                    "created_by": actor.actor_id,
                },
            })
            events = self.store.append_many(records)
            return CommandResult(
                events,
                {"booking_id": booking_id,
                 "pinned": {"product_version": evaluation["product_version"],
                            "service_revision": evaluation["service_revision"],
                            "price_revision": evaluation["price_revision"],
                            "amount": evaluation["price"].amount}},
            )

    # ----- 结算（幂等 / 隔离 / 版本固化） ----------------------------------

    def settle_claim(self, actor: Actor, biz_key: str, booking_id: str) -> CommandResult:
        self._require(actor, "settle_claim", COORDINATOR, RULE_MAINTAINER)
        with self.store.write_lock():
            proj = self._rebuild()

            previous = proj.claims.get(biz_key)
            if previous is not None:
                # 重复请求：核对业务键背后的版本组合。
                return self._idempotent_or_isolate(proj, biz_key, booking_id, previous)

            booking = proj.bookings.get(booking_id)
            self._must_exist(booking is not None, f"预约 {booking_id} 不存在")
            if booking.status == "SETTLED":
                # 该预约已用别的业务键结算：必须人工核对，不能换键重收。
                raise PreconditionFailed(
                    [f"预约 {booking_id} 已以业务键 {proj.claims_by_booking[booking_id]} 结算"])
            if booking.status == "BLOCKED":
                raise PreconditionFailed([
                    f"预约 {booking_id} 重验未通过，暂不能结算；走复核或重新预约"])

            event = self.store.append(
                f"claim:{biz_key}", "CLAIM_SETTLED",
                "claim_record", biz_key,
                {"biz_key": biz_key, "booking_id": booking_id,
                 "patient_id": booking.patient_id, "hospital_id": booking.hospital_id,
                 "region": booking.region, "product_code": booking.product_code,
                 "service_code": booking.service_code, "amount": booking.amount,
                 # 固定采用预约成立时有效的产品与价格版本：
                 "product_version": booking.product_version,
                 "service_revision": booking.service_revision,
                 "price_revision": booking.price_revision,
                 "settled_by": actor.actor_id},
            )
            return CommandResult([event], {"biz_key": biz_key, "amount": booking.amount,
                                           "replayed": False})

    @staticmethod
    def _idempotent_or_isolate(
        proj: Projection, biz_key: str, booking_id: str, previous: Any,
    ) -> CommandResult:
        candidate = proj.bookings.get(booking_id)
        current = {
            "booking_id": booking_id,
            "service_revision": candidate.service_revision if candidate else None,
            "price_revision": candidate.price_revision if candidate else None,
            "product_version": candidate.product_version if candidate else None,
        }
        original = {
            "booking_id": previous.booking_id,
            "service_revision": previous.service_revision,
            "price_revision": previous.price_revision,
            "product_version": previous.product_version,
            "amount": previous.amount,
        }
        same_combo = (
            previous.booking_id == booking_id
            and previous.service_revision == current["service_revision"]
            and previous.price_revision == current["price_revision"]
            and previous.product_version == current["product_version"]
        )
        if same_combo:
            return CommandResult(data={"biz_key": biz_key, "amount": previous.amount,
                                       "replayed": True, "original": original})
        raise IdempotencyIsolation(biz_key, original, current)

    # ----- 证据 / 范围变化的传播 -------------------------------------------

    def _propagate_product_change(
        self, proj: Projection, trigger: StoredEvent,
    ) -> list[StoredEvent]:
        """在同一写锁事务内：未发生的服务重新核验；历史结算开影响评估。

        历史结算事件永不修改，只追加 IMPACT_TASK_OPENED。
        """
        effects: list[StoredEvent] = []
        product_code = trigger.aggregate_id
        # 先用触发事件本身刷新投影，评估依据才是最新事实（新范围/新证据）。
        proj.apply(trigger)
        changed_at = parse_ts(trigger.occurred_at)

        # 1) 尚未发生（含之前被阻断）的预约：按最新事实重新核验。
        for booking in proj.bookings.values():
            if booking.product_code != product_code or booking.status == "SETTLED":
                continue
            if booking.scheduled_at <= changed_at:
                continue  # 服务时点已过，不属于"尚未发生"
            if booking.reverifications.get(trigger.event_id) in ("PASSED", "BLOCKED"):
                continue
            evaluation = proj.evaluate(
                product_code, booking.service_code, booking.region,
                booking.hospital_id, booking.scheduled_at, booking.indication,
            )
            flag = self.store.append(
                f"reverify-flag:{booking.booking_id}:{trigger.event_id}",
                "REVERIFICATION_FLAGGED", "patient_booking", booking.booking_id,
                {"booking_id": booking.booking_id, "trigger_event_id": trigger.event_id,
                 "reason": f"产品事实变化 {trigger.event_type}"},
            )
            effects.append(flag)
            if evaluation["available"]:
                conclusion = self.store.append(
                    f"reverify-pass:{booking.booking_id}:{trigger.event_id}",
                    "REVERIFICATION_PASSED", "patient_booking", booking.booking_id,
                    {"booking_id": booking.booking_id, "trigger_event_id": trigger.event_id},
                )
            else:
                conclusion = self.store.append(
                    f"reverify-block:{booking.booking_id}:{trigger.event_id}",
                    "REVERIFICATION_BLOCKED", "patient_booking", booking.booking_id,
                    {"booking_id": booking.booking_id, "trigger_event_id": trigger.event_id,
                     "reasons": evaluation["reasons"],
                     "missing_evidence": evaluation["missing_evidence"]},
                )
            # 同步投影中的预约状态，供本次循环后续判断使用。
            proj.apply(conclusion)
            effects.append(conclusion)

        # 2) 已完成的历史结算：进入影响评估队列，绝不静默改写。
        for claim in proj.claims.values():
            if claim.product_code != product_code:
                continue
            task_id = f"impact:{claim.biz_key}:{trigger.event_id}"
            if task_id in proj.impact_tasks:
                continue
            opened = self.store.append(
                f"impact-open:{task_id}", "IMPACT_TASK_OPENED",
                "impact_task", task_id,
                {"task_id": task_id, "claim_biz_key": claim.biz_key,
                 "trigger_event_id": trigger.event_id,
                 "reason": f"{trigger.event_type} 影响历史结算 {claim.biz_key}"},
            )
            effects.append(opened)
        return effects

    def review_impact(
        self, actor: Actor, task_id: str, decision: str,
        affected_period: Mapping[str, Any], note: str | None = None,
    ) -> CommandResult:
        """登记影响评估结论（如 NO_ACTION / REBILL / RECOVER / SUPPLEMENT）。"""
        self._require(actor, "review_impact", AUTHORITY, RULE_MAINTAINER, COORDINATOR)
        allowed = {"NO_ACTION", "REBILL", "RECOVER", "SUPPLEMENT_EVIDENCE"}
        if decision not in allowed:
            raise PreconditionFailed([f"decision 必须是 {sorted(allowed)} 之一"])
        with self.store.write_lock():
            proj = self._rebuild()
            task = proj.impact_tasks.get(task_id)
            self._must_exist(task is not None, f"影响评估任务 {task_id} 不存在")
            if not task.opened:
                return CommandResult(data={"task_id": task_id, "dup": True})
            event = self.store.append(
                f"impact-reviewed:{task_id}", "IMPACT_REVIEWED",
                "impact_task", task_id,
                {"task_id": task_id, "affected_period": dict(affected_period),
                 "decision": decision, "note": note, "reviewer": actor.actor_id},
            )
            return CommandResult([event], {"task_id": task_id, "decision": decision})

    def resume(self) -> dict[str, Any]:
        """进程恢复：续写崩溃时未完成的重验，报告仍待人工处理的影响评估。

        判定一律基于磁盘事件重建出的最新投影，不按旧快照下结论。
        """
        with self.store.write_lock():
            proj = self._rebuild()
            resumed: list[str] = []
            for booking, trigger_id in proj.pending_reverifications():
                evaluation = proj.evaluate(
                    booking.product_code, booking.service_code, booking.region,
                    booking.hospital_id, booking.scheduled_at, booking.indication,
                )
                available = evaluation["available"]
                event_type = "REVERIFICATION_PASSED" if available \
                    else "REVERIFICATION_BLOCKED"
                payload: dict[str, Any] = {
                    "booking_id": booking.booking_id, "trigger_event_id": trigger_id}
                if not available:
                    payload["reasons"] = evaluation["reasons"]
                    payload["missing_evidence"] = evaluation["missing_evidence"]
                event = self.store.append(
                    f"reverify-{'pass' if available else 'block'}:"
                    f"{booking.booking_id}:{trigger_id}",
                    event_type, "patient_booking", booking.booking_id, payload,
                )
                proj.apply(event)
                resumed.append(event.event_id)
            open_tasks = [
                {"task_id": t.task_id, "claim_biz_key": t.claim_biz_key,
                 "trigger_event_id": t.trigger_event_id, "reason": t.reason}
                for t in proj.open_impact_tasks()
            ]
            return {"resumed_reverifications": resumed, "open_impact_tasks": open_tasks}

    # ----- 查询：可用性解释 -----------------------------------------------

    def explain_availability(
        self, region: str, service_code: str, hospital_id: str | None = None,
        product_code: str | None = None, indication: str | None = None,
        at: datetime | str | None = None,
    ) -> dict[str, Any]:
        """说明某地某服务为何可用 / 仍缺哪类证据。

        未指定产品时，枚举所有与该服务存在映射（无论是否批准）的产品，
        便于看清楚每条链路上的断点。
        """
        at_dt = parse_ts(at) if isinstance(at, str) else (at or datetime.now().astimezone())
        proj = self._rebuild()
        if product_code is not None:
            targets = [product_code]
        else:
            targets = sorted({
                m.product_code for m in proj.mappings.values()
                if m.service_code == service_code
            })
        results = []
        for code in targets:
            evaluation = proj.evaluate(code, service_code, region, hospital_id, at_dt, indication)
            results.append({
                "product_code": code,
                "available": evaluation["available"],
                "why": evaluation["checks"],
                "reasons": evaluation["reasons"],
                "missing_evidence": evaluation["missing_evidence"],
            })
        return {"region": region, "service_code": service_code,
                "hospital_id": hospital_id, "indication": indication,
                "products": results}

    # ----- 查询：按授权裁剪的个人信息 --------------------------------------

    REDACTION = "***未授权***"

    def patient_view(
        self, requester: Actor, patient_id: str,
    ) -> dict[str, Any]:
        """返回患者相关信息；非本人访问时按患者授权类别披露。

        授权类别：``identity``（身份）、``clinical``（适应证/产品/证据状态）、
        ``financial``（金额、结算与影响评估）。
        """
        proj = self._rebuild()
        own = requester.actor_id == patient_id
        consent = proj.consents.get(patient_id)
        granted_categories = (
            frozenset(consent["categories"]) if consent and consent["granted"] and not own
            else frozenset({"identity", "clinical", "financial"}) if own
            else frozenset()
        )

        bookings_out: list[dict[str, Any]] = []
        for booking in proj.bookings.values():
            if booking.patient_id != patient_id:
                continue
            item: dict[str, Any] = {
                "booking_id": booking.booking_id,
                "hospital_id": booking.hospital_id,
                "region": booking.region,
            }
            if "identity" in granted_categories:
                item["patient_id"] = booking.patient_id
            else:
                item["patient_id"] = self.REDACTION
            if "clinical" in granted_categories:
                item.update({
                    "product_code": booking.product_code, "service_code": booking.service_code,
                    "indication": booking.indication, "scheduled_at": booking.scheduled_at.isoformat(),
                    "status": booking.status,
                    "reverifications": dict(booking.reverifications),
                    "trial_id": booking.trial_id,
                })
            if "financial" in granted_categories:
                item["pinned_amount"] = booking.amount
            bookings_out.append(item)

        claims_out: list[dict[str, Any]] = []
        if "financial" in granted_categories:
            for claim in proj.claims.values():
                if claim.patient_id != patient_id:
                    continue
                claims_out.append({
                    "biz_key": claim.biz_key, "booking_id": claim.booking_id,
                    "amount": claim.amount, "region": claim.region,
                    "product_version": claim.product_version,
                    "service_revision": claim.service_revision,
                    "price_revision": claim.price_revision,
                    "settled_at": claim.settled_at.isoformat(),
                })

        return {
            "patient_id": patient_id if "identity" in granted_categories else self.REDACTION,
            "disclosure": "self" if own else f"consent:{sorted(granted_categories)}",
            "consent": {"granted": bool(consent and consent["granted"]),
                        "categories": sorted(consent["categories"]) if consent else []},
            "bookings": bookings_out,
            "claims": claims_out,
        }
