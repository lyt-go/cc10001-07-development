"""采集任务业务规则：状态流转、字段校验与筛选口径都收在这里。

启动任务不再是单纯的状态改写，而是交给代理调度模块统一判定出口与限速额度：
只有拿到可用出口才会进入执行中，否则停在待调度并记录原因。
"""
from __future__ import annotations

from typing import Any

from app.services import dispatch
from app.store import store

MODULE = "taskcrawl"
LOG_MODULE = "storagelog"
REQUIRED_FIELDS = ["任务编号", "采集源名称", "规则编号"]
STATUS_ORDER = ["草稿", "待调度", "执行中", "已完成", "已终止"]
ACTION_RULES = {"提交调度": "待调度", "启动任务": "执行中", "终止任务": "已终止", "完成任务": "已完成"}
NEGATIVE_ACTIONS = []


class TaskcrawlService:
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
            rows = [row for row in rows if keyword in str(row.get("任务编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [dispatch.task_snapshot(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        return None if entry is None else dispatch.task_snapshot(entry)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for extra in ("目标域名", "采集源名称"):
            if extra not in entry and str(values.get(extra) or "").strip():
                entry[extra] = values.get(extra)
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return dispatch.task_snapshot(entry), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"采集任务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集任务可执行范围"
        if action == "启动任务":
            return self._start(entry)
        if action == "完成任务":
            return self._finish(entry)
        if action == "终止任务" and str(entry.get("status")) == "已完成":
            return None, "采集任务已完成，不能再终止"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if target == "已终止" and str(entry.get("status")) == "执行中":
            dispatch.finish_task(entry, "已终止")
            entry["abnormal"] = True
        else:
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            entry["调度原因"] = ""
        verb = "终止" if action == "终止任务" else action
        return dispatch.task_snapshot(entry), f"采集任务已{verb}"

    def _start(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        current = str(entry.get("status"))
        if current == "执行中":
            return None, "采集任务已在执行中，不能重复分配出口"
        if current in {"已完成", "已终止"}:
            return None, f"采集任务{current}，不能再启动"
        started, message, _plan = dispatch.start_task(entry)
        if started is None:
            entry["status"] = "待调度"
            entry["pending"] = True
            return None, f"没有可用出口，任务停在待调度：{message}"
        started["abnormal"] = False
        return dispatch.task_snapshot(started), message

    def _finish(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        if str(entry.get("status")) != "执行中":
            return None, "只有执行中的采集任务可以完成"
        dispatch.finish_task(entry, "已完成")
        entry["pending"] = False
        logs = store.rows(LOG_MODULE)
        log_id = max((int(row.get("id", 0)) for row in logs), default=0) + 1
        stored = dispatch.build_completed_log(entry, log_id)
        logs.append(stored)
        return dispatch.task_snapshot(entry), f"采集任务已完成，出口记录已写入采集日志 {stored['日志编号']}"

    def report_feedback(
        self, entry_id: int, *, failed: bool, reason: str = ""
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"采集任务 {entry_id} 不存在或已归档"
        if str(entry.get("status")) != "执行中":
            return None, "只有执行中的采集任务可以回报请求结果"
        feedback = dispatch.record_feedback(entry, failed=failed, reason=reason)
        return dispatch.task_snapshot(entry), feedback["message"]
