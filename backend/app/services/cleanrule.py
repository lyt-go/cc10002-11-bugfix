"""清洗规则业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

import threading
from typing import Any

from app.services.auditlog import record_change
from app.store import store

MODULE = "cleanrule"
REQUIRED_FIELDS = ["规则编号", "输入表名", "空值策略"]
STATUS_ORDER = ["草稿", "规则评审中", "已启用", "已废弃"]
ACTION_RULES = {"提交规则评审": "规则评审中", "启用规则": "已启用", "废弃规则": "已废弃"}
NEGATIVE_ACTIONS = []
# 创建时只允许业务字段进入存储，状态位一律由服务端流转产生，不接受界面临时状态。
RESERVED_FIELDS = {"id", "status", "pending", "abnormal", "当前版本"}


class CleanruleService:
    # 同一输入表的换版与质量准入读取共用一把锁：任意时刻只允许一个版本成为当前版本。
    VERSION_LOCK = threading.RLock()

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._with_current_version(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        return self._with_current_version(entry)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        # 业务字段全量保留（格式表达式、异常阈值、输出字段、规则版本等），状态字段忽略。
        for field, value in values.items():
            if field in RESERVED_FIELDS:
                continue
            entry[field] = value
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str, bool]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"清洗规则 {entry_id} 不存在或已归档", False
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于清洗规则可执行范围", False
        target = ACTION_RULES[action]
        current = str(entry.get("status") or STATUS_ORDER[0])
        blocked = self._transition_block_reason(current, target, action)
        if blocked:
            return None, blocked, False
        with self.VERSION_LOCK:
            if target == "已启用":
                self._retire_siblings_locked(entry)
            entry["status"] = target
            entry["pending"] = target in ("草稿", "规则评审中")
            entry["abnormal"] = action in NEGATIVE_ACTIONS
        record_change(
            table=str(entry.get("输入表名") or ""),
            change_type=f"清洗规则{action}",
            before=current,
            after=target,
            reason=f"清洗规则 {entry.get('规则编号')} 版本 {entry.get('规则版本') or '未填'}：{current} → {target}",
        )
        return entry, f"清洗规则已{action}", True

    def current_entry_for_table(self, table: str) -> dict[str, Any] | None:
        """读取某输入表当前生效的清洗规则：已启用且未被换版退役的那一条。"""
        table = str(table or "").strip()
        if not table:
            return None
        for row in store.rows(MODULE):
            if str(row.get("输入表名") or "").strip() == table and row.get("status") == "已启用":
                return row
        return None

    def current_version_for_table(self, table: str) -> str | None:
        """质量门禁与清洗详情共用的当前版本口径，两边读同一个结论。"""
        entry = self.current_entry_for_table(table)
        if entry is None:
            return None
        version = str(entry.get("规则版本") or "").strip()
        return version or None

    def find_version(self, table: str, version: str) -> dict[str, Any] | None:
        """按输入表 + 规则版本定位一条清洗规则，用于说明声明版本卡在哪个状态。"""
        table = str(table or "").strip()
        version = str(version or "").strip()
        for row in store.rows(MODULE):
            if (
                str(row.get("输入表名") or "").strip() == table
                and str(row.get("规则版本") or "").strip() == version
            ):
                return row
        return None

    def _with_current_version(self, row: dict[str, Any]) -> dict[str, Any]:
        """列表与详情读取时标注该输入表的当前版本，不改动存储里的原始行。"""
        annotated = dict(row)
        annotated["当前版本"] = self.current_version_for_table(str(row.get("输入表名") or ""))
        return annotated

    def _transition_block_reason(self, current: str, target: str, action: str) -> str | None:
        """只能按 草稿→规则评审中→已启用→已废弃 逐级推进，其余一律拦截。"""
        if current not in STATUS_ORDER:
            return f"清洗规则当前状态「{current}」不在允许的状态序列里"
        if current == target:
            return f"清洗规则已处于「{current}」，不能重复{action}"
        next_index = STATUS_ORDER.index(current) + 1
        if next_index >= len(STATUS_ORDER) or STATUS_ORDER[next_index] != target:
            return (
                f"清洗规则当前为「{current}」，只能按 {'→'.join(STATUS_ORDER)} 逐级推进，"
                f"不能执行「{action}」"
            )
        return None

    def _retire_siblings_locked(self, entry: dict[str, Any]) -> None:
        """换版：同一输入表其它已启用版本同步退役，当前版本始终唯一。"""
        table = str(entry.get("输入表名") or "").strip()
        for row in store.rows(MODULE):
            if row is entry or row.get("status") != "已启用":
                continue
            if str(row.get("输入表名") or "").strip() != table:
                continue
            row["status"] = "已废弃"
            row["pending"] = False
            record_change(
                table=table,
                change_type="清洗规则换版退役",
                before="已启用",
                after="已废弃",
                reason=(
                    f"清洗规则 {row.get('规则编号')} 版本 {row.get('规则版本') or '未填'} "
                    f"被 {entry.get('规则编号')} 版本 {entry.get('规则版本') or '未填'} 替换"
                ),
            )
