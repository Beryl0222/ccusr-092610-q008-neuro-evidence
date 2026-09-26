# 领域约定

报道提到医保部门为脑机接口新技术打通从产品获批、医保赋码到临床应用的衔接，同时多数产品尚处临床试验阶段，价格与可及性仍待解决。

聚合对象包括`product_status`、`service_mapping`、`regional_trial`、`claim_record`。事件类型包括`STATUS_VERIFIED`、`MAPPING_PROPOSED`、`HOSPITAL_ADMITTED`、`CLAIM_SETTLED`、`IMPACT_REVIEWED`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `STATUS_VERIFIED`：载荷还需包含 `authority_ref`, `scope`。
- `CLAIM_SETTLED`：载荷还需包含 `service_revision`, `price_revision`。
- `IMPACT_REVIEWED`：载荷还需包含 `affected_period`, `decision`。

相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责；本仓库只定义可稳定交换的基础事实。
