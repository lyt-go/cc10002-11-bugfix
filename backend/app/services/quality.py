"""质量校验业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services.auditlog import record_change
from app.services.cleanrule import CleanruleService
from app.store import store

MODULE = "quality"
REQUIRED_FIELDS = ["规则编号", "校验表名", "目标字段"]
STATUS_ORDER = ["草稿", "规则评审中", "已启用", "已废弃"]
ACTION_RULES = {"提交规则评审": "规则评审中", "启用规则": "已启用", "废弃规则": "已废弃"}
NEGATIVE_ACTIONS = []
# 创建时只允许业务字段进入存储；准入快照由启用动作写入，不接受界面临时状态。
RESERVED_FIELDS = {"id", "status", "pending", "abnormal", "当前清洗版本", "准入清洗规则", "准入清洗版本"}

cleanrule_service = CleanruleService()


class QualityService:
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
        # 业务字段全量保留（校验表达式、阈值、依赖清洗版本等），状态与准入快照忽略。
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
            return None, f"质量规则 {entry_id} 不存在或已归档", False
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于质量校验可执行范围", False
        target = ACTION_RULES[action]
        current = str(entry.get("status") or STATUS_ORDER[0])
        blocked = self._transition_block_reason(current, target, action)
        if blocked:
            return None, blocked, False
        if target == "已启用":
            return self._enable_with_gate(entry)
        entry["status"] = target
        entry["pending"] = target in ("草稿", "规则评审中")
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        record_change(
            table=str(entry.get("校验表名") or ""),
            change_type=f"质量规则{action}",
            before=current,
            after=target,
            reason=f"质量规则 {entry.get('规则编号')}：{current} → {target}",
        )
        return entry, f"质量规则已{action}", True

    def _enable_with_gate(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str, bool]:
        """质量门禁：启用前必须存在已启用且输入表匹配的清洗规则当前版本。

        任一条件不满足时规则停在「规则评审中」，不留下已启用标记，并同步审计拦截原因。
        """
        table = str(entry.get("校验表名") or "").strip()
        declared = str(entry.get("依赖清洗版本") or "").strip()
        with CleanruleService.VERSION_LOCK:
            current_clean = cleanrule_service.current_entry_for_table(table)
            if current_clean is None:
                return self._block_enable(
                    entry,
                    f"质量门禁拦截：校验表「{table}」没有已启用且输入表匹配的清洗规则版本，"
                    f"质量规则保持「规则评审中」",
                )
            version = str(current_clean.get("规则版本") or "").strip()
            if not version:
                return self._block_enable(
                    entry,
                    f"质量门禁拦截：清洗规则 {current_clean.get('规则编号')} 未填写规则版本，"
                    f"质量门禁读取不到当前规则版本，质量规则保持「规则评审中」",
                )
            if declared and declared != version:
                dependency = cleanrule_service.find_version(table, declared)
                dependency_status = str(dependency.get("status")) if dependency else "未登记"
                dependency_code = dependency.get("规则编号") if dependency else "未登记"
                return self._block_enable(
                    entry,
                    f"质量门禁拦截：清洗规则 {dependency_code} 的 {declared} 仍在{dependency_status}，"
                    f"当前版本为 {version}，质量规则保持「规则评审中」",
                )
            entry["status"] = "已启用"
            entry["pending"] = False
            entry["abnormal"] = False
            # 准入快照：启用当下通过的清洗版本，后续换版不回写，已产生的结果仍保留当时版本。
            entry["准入清洗规则"] = current_clean.get("规则编号")
            entry["准入清洗版本"] = version
        record_change(
            table=table,
            change_type="质量规则启用规则",
            before="规则评审中",
            after="已启用",
            reason=(
                f"质量规则 {entry.get('规则编号')} 通过版本准入："
                f"清洗规则 {entry['准入清洗规则']} 版本 {version}"
            ),
        )
        return entry, f"质量规则已启用规则，准入清洗版本 {version}", True

    def _block_enable(
        self, entry: dict[str, Any], reason: str
    ) -> tuple[dict[str, Any] | None, str, bool]:
        """拦截启用：状态停在评审中、清掉可能残留的准入快照，并同步审计。"""
        entry["status"] = "规则评审中"
        entry["pending"] = True
        entry.pop("准入清洗规则", None)
        entry.pop("准入清洗版本", None)
        record_change(
            table=str(entry.get("校验表名") or ""),
            change_type="质量门禁拦截",
            before="规则评审中",
            after="规则评审中",
            reason=f"质量规则 {entry.get('规则编号')}：{reason}",
        )
        return entry, reason, False

    def _with_current_version(self, row: dict[str, Any]) -> dict[str, Any]:
        """列表与详情读取时标注校验表的当前清洗版本，与清洗详情读同一个结论。"""
        annotated = dict(row)
        annotated["当前清洗版本"] = cleanrule_service.current_version_for_table(
            str(row.get("校验表名") or "")
        )
        return annotated

    def _transition_block_reason(self, current: str, target: str, action: str) -> str | None:
        """只能按 草稿→规则评审中→已启用→已废弃 逐级推进，其余一律拦截。"""
        if current not in STATUS_ORDER:
            return f"质量规则当前状态「{current}」不在允许的状态序列里"
        if current == target:
            return f"质量规则已处于「{current}」，不能重复{action}"
        next_index = STATUS_ORDER.index(current) + 1
        if next_index >= len(STATUS_ORDER) or STATUS_ORDER[next_index] != target:
            return (
                f"质量规则当前为「{current}」，只能按 {'→'.join(STATUS_ORDER)} 逐级推进，"
                f"不能执行「{action}」"
            )
        return None
