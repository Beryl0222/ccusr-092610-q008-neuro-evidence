# 创新技术证据接力服务

在领域事件契约（`contracts/domain.schema.json`、`docs/domain.md`）之上，
`neuro_evidence.service.EvidenceRelayService` 实现"产品获批 → 医保赋码 →
医院准入 → 临床证据 → 价格 → 预约 → 结算 → 影响评估"的证据接力。
所有状态由追加式事件日志（`storage.EventStore`，JSONL + 文件锁）重建，
服务本身不保存可变状态。

## 角色

| 角色常量 | 含义 | 关键权限 |
| --- | --- | --- |
| `AUTHORITY` | 监管授权确认人员 | 登记监管状态、收窄适应范围 |
| `HOSPITAL_ADMIN` | 医院管理员（绑定本院） | 决定本院是否准入某服务 |
| `RULE_MAINTAINER` | 医保规则维护者 | 提交/批准收费映射、发布价格 |
| `EVIDENCE_SUBMITTER` | 临床证据提交方 | 登记证据摘要 |
| `COORDINATOR` | 医保项目协调员 | 开试点、建预约、结算、评估 |
| `PATIENT` | 患者本人 | 登记本人授权、签署知情说明 |

职责分离硬规则：**收费映射的提交者不能批准自己提交的映射**，
必须由另一位 `RULE_MAINTAINER` 批准，否则抛 `PreconditionFailed`。

## 核心规则与对应实现

1. **产品获批 ≠ 全员可报销**。预约与解释查询要逐环通过
   监管状态（须 `LICENSED`）、适应范围、必需临床证据、收费映射批准、
   医院准入（地区一致）、当时有效价格六项检查（见 `Projection.evaluate`）。
2. **版本固化**。预约成立时把 `product_version`、`service_revision`、
   `price_revision` 与金额写入 `BOOKING_CREATED`；`CLAIM_SETTLED` 只复制
   预约固化的值。之后再发布新价格不影响已成立的预约。
3. **结算幂等与隔离**。同一业务键重复请求直接返回原结果（`replayed=True`，
   不产生新事件）；同键但背后的预约/服务/价格版本组合不同，抛
   `IdempotencyIsolation`，保留 `original` 与 `current`，不静默覆盖。
   已结算预约换键重收同样被拒。
4. **证据补充 / 范围收窄的传播**（与触发事件同一文件锁事务内完成）：
   - 服务时点**尚未发生**的预约：追加 `REVERIFICATION_FLAGGED` 后按最新事实
     重新核验，结论为 `REVERIFICATION_PASSED`（恢复 ACTIVE）或
     `REVERIFICATION_BLOCKED`（不能结算）；证据补齐后可再次核验通过。
   - **已结算**的历史：原 `CLAIM_SETTLED` 永不改写，只追加
     `IMPACT_TASK_OPENED` 进入影响评估队列，由 `review_impact` 登记
     `NO_ACTION / REBILL / RECOVER / SUPPLEMENT_EVIDENCE` 结论。
5. **试点名额并发不超额**。`create_booking` 在 `write_lock()` 内完成
   "以磁盘为准重建 → 检查 `len(allocations) < quota` → 占用名额 + 建预约
   （`append_many` 单次落盘）"。线程与多进程竞争均已测试。
6. **崩溃恢复**。`resume()` 找出只有 `FLAGGED` 没有结论的重验，按
   最新投影续写结论，并报告全部未关闭的影响评估任务；重复执行幂等。
7. **可用性解释**。`explain_availability(region, service, hospital, ...)`
   返回每个候选产品的逐项检查、阻断原因与 `missing_evidence`，回答
   "某地某服务为何可用、还缺哪类证据"。
8. **按授权披露**。`patient_view` 对本人全量披露；第三方访问按患者授权的
   `identity / clinical / financial` 三类裁剪，未授权字段输出
   `***未授权***`；撤销授权后敏感字段立即不可见。

所有时间戳必须携带时区（契约层强制）；聚合版本号从 1 开始、由存储分配。

## 事件

服务层事件信封沿用 `contracts/domain.schema.json` 的信封校验函数，
扩展事件与载荷见 `contracts/service.schema.json`：

`PRODUCT_REGISTERED`、`STATUS_VERIFIED`、`SCOPE_NARROWED`、
`SERVICE_REGISTERED`、`EVIDENCE_SUMMARIZED`、`MAPPING_PROPOSED`、
`MAPPING_APPROVED`、`PRICE_PUBLISHED`、`HOSPITAL_ADMITTED`、
`TRIAL_OPENED`、`TRIAL_ALLOCATED`、`TRIAL_RELEASED`、`BOOKING_CREATED`、
`REVERIFICATION_FLAGGED/PASSED/BLOCKED`、`CONSENT_RECORDED`、
`NOTICE_ACKNOWLEDGED`、`CLAIM_SETTLED`、`IMPACT_TASK_OPENED`、
`IMPACT_REVIEWED`。

`data/service_sample.json` 是一条可直接用服务 schema 校验的结算样例。

## 典型流程（伪代码）

```python
svc.verify_status(auth, "BCI-1", "NMPA-2026-001", ["stroke_upper_limb"], ["rct_pivot"])
svc.summarize_evidence(submitter, "BCI-1", "rct", "ref/1", ["rct_pivot"])
svc.propose_mapping(rule_a, "BCI-1", "SVC-REHAB")
svc.approve_mapping(rule_b, "BCI-1", "SVC-REHAB")          # 不能与提交者同人
svc.publish_price(rule_b, "BCI-1", "SVC-REHAB", 1200.0, effective_at)
svc.admit_hospital(hospital_admin, "HOSP1", "SVC-REHAB", "BJ")
svc.create_booking(coord, "B1", "P001", "HOSP1", "BJ", ..., scheduled_at, trial_id="T1")
svc.settle_claim(coord, biz_key="claim-001", booking_id="B1")
```
