# 领域约定

报道提到医保部门为脑机接口新技术打通从产品获批、医保赋码到临床应用的衔接，同时多数产品尚处临床试验阶段，价格与可及性仍待解决。

聚合对象包括 `product_status`、`service_catalog`、`service_mapping`、`price_version`、`clinical_evidence`、`hospital_access`、`regional_trial`、`patient_consent`、`claim_record`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 角色

- `authority`：监管授权人员，唯一可以确认产品监管状态（`STATUS_VERIFIED`）的角色。
- `hospital`：医院，决定本院对具体收费项目的准入（`HOSPITAL_ADMITTED`）。
- `rule_maintainer`：医保规则维护者，可提交产品到收费项目的映射，但**不得批准自己提交的映射**；`MAPPING_APPROVED` 的批准人必须与 `MAPPING_PROPOSED.submitter` 不同。
- `coordinator`：医保项目协调员，登记证据、价格版本、地区试点、知情说明并驱动结算与影响评估。

## 事件与载荷

| 事件 | 聚合 | 载荷要点 | 语义 |
| --- | --- | --- | --- |
| `STATUS_VERIFIED` | product_status | `authority_ref`, `scope` | 监管状态确认；产品获批只代表监管范围，不等于所有患者可报销 |
| `INDICATION_DECLARED` | product_status | `indication_id`, `label` | 登记产品适应范围（逐版追加） |
| `SERVICE_REGISTERED` | service_catalog | `service_item_code`, `name` | 医保医疗服务价格项目登记 |
| `MAPPING_PROPOSED` | service_mapping | `product_id`, `service_item_code`, `submitter` | 产品→收费项目映射提案 |
| `MAPPING_APPROVED` | service_mapping | `approver` | 映射批准；批准人不得是提交人 |
| `PRICE_VERSION_EFFECTIVE` | price_version | `product_id`, `price_revision`, `amount`, `effective_from` | 价格版本，按生效时间形成时点有效版本 |
| `EVIDENCE_SUMMARIZED` | clinical_evidence | `product_id`, `evidence_kind`, `summary_ref` | 临床证据摘要；`evidence_kind` 如 `pivotal_trial`、`real_world`、`indication_subgroup` |
| `SCOPE_NARROWED` | product_status | `product_id`, `removed_indication_ids` | 适应范围收窄（只增不改写历史） |
| `HOSPITAL_ADMITTED` | hospital_access | `hospital_id`, `service_item_code` | 医院对项目的本院准入 |
| `TRIAL_OPENED` | regional_trial | `region`, `service_item_code`, `quota_total` | 地区试点开闸与名额 |
| `TRIAL_QUOTA_GRANTED` | regional_trial | `region`, `service_item_code`, `patient_ref` | 试点名额分配，并发下不得超额 |
| `CONSENT_RECORDED` | patient_consent | `patient_ref`, `disclosure_scope`, `granted` | 患者知情说明及个人信息披露授权 |
| `CLAIM_REQUESTED` | claim_record | `product_id`, `service_item_code`, `region`, `hospital_id`, `patient_ref`, `indication_id` | 尚未发生的服务申请；逐闸门核验并要求已持有试点名额，是后续重核验对象 |
| `CLAIM_SETTLED` | claim_record | `product_revision`, `service_revision`, `price_revision` | 结算，固定结算时点有效的产品范围、服务映射与价格版本 |
| `CLAIM_REVALIDATED` | claim_record | `still_bookable`, `reason` | 证据补充/范围收窄后，尚未发生的服务重新核验 |
| `IMPACT_REVIEW_OPENED` | claim_record | `trigger_event_id`, `reason` | 历史结算进入影响评估流程（不静默改写） |
| `IMPACT_REVIEWED` | claim_record | `affected_period`, `decision` | 影响评估结论 |

顶层可选字段 `actor` 记录操作人，供上层服务做角色校验；基础契约不强制角色语义。

## 上层服务规则

相同事件标识的业务幂等、冲突隔离、角色授权、版本时点固定、名额并发控制、影响评估的恢复续跑与按知情授权限制披露，由 `src/neuro_evidence/service.py` 实现；本契约只定义可稳定交换的基础事实。
