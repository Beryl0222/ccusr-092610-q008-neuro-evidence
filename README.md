# 脑机创新项目医保证据接力簿

报道提到医保部门为脑机接口新技术打通从产品获批、医保赋码到临床应用的衔接，同时多数产品尚处临床试验阶段，价格与可及性仍待解决。本仓库在基础事件契约之上，实现一套"证据接力服务"：产品获批不被直接当作全员可报销，每一次结算都固定在当时有效的产品、服务与价格版本上；证据或范围变化只影响未发生的服务，历史结算进入可追溯的影响评估。

## 目录

- `contracts/domain.schema.json`：对象、事件和载荷字段约定。
- `data/sample.json`：可直接校验的联调样例。
- `src/neuro_evidence/contracts.py`：基础契约校验（不改写调用方输入）。
- `src/neuro_evidence/store.py`：SQLite 只追加事件存储、业务幂等表、影响评估任务表、名额占用表。
- `src/neuro_evidence/world.py`：从事件流折叠出的读模型（产品/映射/价格/证据/准入/试点/结算/知情）。
- `src/neuro_evidence/catalog.py`：纯领域策略——逐道闸门解释某地某服务为何可用、仍缺哪类证据。
- `src/neuro_evidence/service.py`：接力服务门面（角色授权、职责分离、版本固定、幂等与冲突隔离、传播、名额、恢复、知情披露）。
- `examples/walkthrough.py`：端到端走查脚本。
- `tests/`：契约与服务测试（含真实多线程名额竞争与进程恢复）。
- `docs/domain.md`：领域对象、角色与事件语义。

## 关键规则如何落地

| 要求 | 实现 |
| --- | --- |
| 监管状态由授权人员确认 | `verify_status` 仅 `authority` 角色可调用 |
| 医院决定具体准入 | `hospital_admit` 仅 `hospital` 角色，且要求项目已登记 |
| 规则维护者不能批准自己提交的映射 | `MAPPING_PROPOSED.submitter` 与 `MAPPING_APPROVED.approver` 必须不同 |
| 产品获批 ≠ 全员可报销 | 可及性需逐闸门通过：监管状态、适应症在范围、证据齐备、项目登记、映射批准、医院准入、地区试点、生效价格 |
| 结算固定当时有效的产品/价格版本 | 结算记录 `product_revision`、`service_revision`、`price_revision`，价格按 `effective_from` 取时点有效版本 |
| 重复请求按业务键返回原结果 | 结算业务键 `settle|地区|医院|患者|项目`，命中即回放原结果 |
| 键相同但版本变化时隔离 | 版本指纹不同抛 `VersionConflict`，绝不覆盖原结算 |
| 证据补充/范围收窄的传播 | 已申请未结算的服务追加 `CLAIM_REVALIDATED`；已结算的开立影响评估任务与 `IMPACT_REVIEW_OPENED`，历史不静默改写 |
| 进程恢复后续跑 | 影响评估任务独立持久化为 `open/done`，新进程调用 `resume_impact_reviews` 续跑 |
| 试点名额并发不超额 | `BEGIN IMMEDIATE` 写锁串行化 + `(地区,项目,患者)` 唯一占位 + 余量闸门 |
| 按授权限制个人信息披露 | `get_claim_view` 依据 `CONSENT_RECORDED.granted` 与 `disclosure_scope` 返回完整或去标识化视图 |
| 查询缺哪类证据 | `explain_availability` 返回每道闸门的通过/阻断原因与 `missing_evidence` |

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests examples
```

## 端到端走查

```bash
PYTHONPATH=src python3 examples/walkthrough.py
```

## 样例契约校验

```bash
PYTHONPATH=src python3 -m neuro_evidence.cli contracts/domain.schema.json data/sample.json
```

样例有效时输出 `valid`；发现问题时逐行给出字段、代码和中文说明，并返回非零状态。
