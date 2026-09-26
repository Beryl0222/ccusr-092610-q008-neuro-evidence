# 脑机创新项目医保证据接力簿

报道提到医保部门为脑机接口新技术打通从产品获批、医保赋码到临床应用的衔接，同时多数产品尚处临床试验阶段，价格与可及性仍待解决。

## 目录

- `contracts/domain.schema.json`：对象、事件和载荷字段约定。
- `contracts/service.schema.json`：证据接力服务扩展事件与载荷约定。
- `data/sample.json`：可直接校验的联调样例。
- `data/service_sample.json`：服务层结算事件样例。
- `src/neuro_evidence/`：基础契约校验、事件存储、投影与领域服务、命令行入口。
- `tests/`：信封、时间、版本和事件载荷测试，以及服务层业务规则测试。
- `docs/domain.md`：领域对象与事件语义。
- `docs/service.md`：证据接力服务的角色、规则与流程说明。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```

## 样例校验

```bash
PYTHONPATH=src python3 -m neuro_evidence.cli contracts/domain.schema.json data/sample.json
PYTHONPATH=src python3 -m neuro_evidence.cli contracts/service.schema.json data/service_sample.json
```

样例有效时输出 `valid`；发现问题时逐行给出字段、代码和中文说明，并返回非零状态。

## 证据接力服务

`src/neuro_evidence/service.py` 在契约之上提供完整服务：授权确认监管状态、
医院自主准入、收费映射提交/批准职责分离、预约时固化产品与价格版本、
结算业务键幂等与版本冲突隔离、试点名额并发不超额、证据/范围变化触发
未发生服务重验与历史结算影响评估、崩溃后续写、可用性解释和按患者授权
披露。规则与 API 说明见 `docs/service.md`，行为见 `tests/test_relay.py`。
