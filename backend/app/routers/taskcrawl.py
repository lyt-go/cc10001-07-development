"""采集任务接口：维护采集任务，覆盖提交调度、启动任务、终止任务等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.taskcrawl import TaskcrawlService

router = APIRouter(prefix="/api/taskcrawl", tags=["采集任务"])

service = TaskcrawlService()

LIST_FIELDS = ["任务编号", "采集源名称", "规则编号", "目标域名", "出口可用性", "调度原因", "出口节点编号", "出口代理"]
STATUSES = ["草稿", "待调度", "执行中", "已完成", "已终止"]
DISPATCH_FIELDS = ["目标域名", "出口可用性", "调度原因", "出口节点编号", "出口代理", "可用连接数"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按任务编号检索"),
    status: str | None = Query(default=None, description="草稿、待调度、执行中、已完成、已终止"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按任务编号与状态过滤采集任务列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/dispatch/availability")
def dispatch_availability(status: str | None = Query(default=None)) -> dict[str, Any]:
    """采集任务清单的调度可用性摘要：按目标域名与可调度性汇总。"""
    items, total = service.list_entries(status=status, page=1, size=10000)
    ready = sum(1 for item in items if item.get("出口可用性") == "可调度")
    blocked = sum(1 for item in items if item.get("出口可用性") == "不可调度")
    return {
        "total": total,
        "ready": ready,
        "blocked": blocked,
        "items": [
            {
                "id": item.get("id"),
                "任务编号": item.get("任务编号"),
                "status": item.get("status"),
                "目标域名": item.get("目标域名"),
                "出口可用性": item.get("出口可用性"),
                "调度原因": item.get("调度原因"),
                "出口节点编号": item.get("出口节点编号"),
            }
            for item in items
        ],
    }


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出采集任务清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "taskcrawl", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条采集任务明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"采集任务 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条采集任务，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="采集任务已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """执行提交调度、启动任务、完成/终止任务；启动时按目标域名判定出口与限速额度。

没有健康代理节点或限速策略不可用时任务停在待调度并返回原因，不做直连降级。
"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/feedback", response_model=ActionResult)
def report_feedback(entry_id: int, payload: EntryPayload) -> ActionResult:
    """回报一次请求结果：失败信号会让目标域名进入限速冷却期。"""
    failed = bool(payload.values.get("failed"))
    reason = str(payload.values.get("reason") or "").strip()
    entry, message = service.report_feedback(entry_id, failed=failed, reason=reason)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
