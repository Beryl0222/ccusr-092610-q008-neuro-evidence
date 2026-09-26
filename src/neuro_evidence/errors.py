"""证据接力服务的领域错误。"""

from __future__ import annotations

from typing import Any, Mapping


class RelayError(Exception):
    """所有领域错误的基类。"""


class UnauthorizedError(RelayError):
    """操作者角色不允许执行该命令。"""

    def __init__(self, actor_id: str, role: str, action: str) -> None:
        super().__init__(f"操作者 {actor_id}(role={role}) 无权执行 {action}")
        self.actor_id = actor_id
        self.role = role
        self.action = action


class NotFoundError(RelayError):
    """引用的聚合对象尚不存在。"""


class PreconditionFailed(RelayError):
    """命令的前置条件不满足，reasons 逐项说明缺口。"""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("；".join(reasons))
        self.reasons = reasons


class IdempotencyIsolation(RelayError):
    """业务键重复，但固化的服务/价格版本与本次不同——隔离而非覆盖。

    ``original`` 中保留原结算结果，调用方必须换用新业务键或显式核查，
    系统不会按新版本静默重放旧键。
    """

    def __init__(self, biz_key: str, original: Mapping[str, Any], current: Mapping[str, Any]) -> None:
        super().__init__(f"业务键 {biz_key} 已用于不同的服务/价格版本组合，已隔离")
        self.biz_key = biz_key
        self.original = dict(original)
        self.current = dict(current)


class QuotaExhaustedError(RelayError):
    """地区试点名额已满，并发分配也不得超额。"""

    def __init__(self, trial_id: str, quota: int) -> None:
        super().__init__(f"试点 {trial_id} 名额 {quota} 已用尽")
        self.trial_id = trial_id
        self.quota = quota
