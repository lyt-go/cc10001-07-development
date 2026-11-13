"""代理出口调度与限速联动：把代理节点、限速策略和采集任务串成一条判定链。

采集任务启动前，按任务的目标域名做一次完整判定：

1. 目标域名必须有当前启用的限速策略，否则任务停在待调度；
2. 同一域名处于冷却期、达到每秒请求数、达到每日请求上限或并发连接数时，
   不再分配新请求；
3. 代理节点必须健康状态可用、未达到带宽上限且未被停用；
4. 代理节点与限速规则冲突时，以目标域名当前启用的限速策略为准，
   再据此计算还能分配多少个连接；
5. 没有可用出口时明确给出原因，绝不退化成直连。

所有“判定 + 占用配额”的动作都在同一把可重入锁内完成，多个任务并发争用最后
一个可用连接时只有一个能成功。连接占用直接由“执行中”的任务行体现，所以代理
或限速配置变化后重新计算即可同步可用性；已写入采集日志的出口记录是快照，
后续配置变化不会回改。
"""
from __future__ import annotations

import re
import threading
import time
from datetime import date, datetime
from typing import Any

from app.store import store

PROXY_MODULE = "proxyconfig"
RATE_MODULE = "ratelimiter"
TASK_MODULE = "taskcrawl"
LOG_MODULE = "storagelog"

TASK_PENDING = "待调度"
TASK_RUNNING = "执行中"
TASK_DONE = "已完成"
TASK_TERMINATED = "已终止"
POLICY_ENABLED = "已启用"

TERMINAL_TASK_STATUSES = {TASK_DONE, TASK_TERMINATED}
DISABLED_PROXY_STATUSES = {"已下线", "下线", "停用", "已停用", "禁用", "disabled", "offline"}
READY_PROXY_STATUSES = {"可用", "已启用", "启用", "在线", "available"}
DISABLED_HEALTH_TOKENS = (
    "不可用",
    "unavailable",
    "停用",
    "禁用",
    "异常",
    "故障",
    "离线",
    "down",
    "unhealthy",
    "disabled",
    "failed",
    "error",
)
HEALTHY_HEALTH_TOKENS = ("可用", "健康", "正常", "在线", "available", "healthy", "up", "ok")
UNLIMITED_TOKENS = {"", "不限", "无限制", "无限", "unlimited", "none", "-", "—"}

_NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)")

_LOCK = threading.RLock()
_DOMAIN_STATE: dict[str, dict[str, Any]] = {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def normalize_domain(value: Any) -> str:
    """统一域名口径：去掉协议、路径、端口外的修饰并转小写。"""
    text = _text(value).lower()
    if not text:
        return ""
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("@")[-1]
    text = re.split(r"[/?#]", text, maxsplit=1)[0]
    return text.strip().rstrip(".")


def as_number(value: Any) -> float | None:
    """从“5”“100 Mbps”“1,000 次”这类字段里取出第一个数值；不限与空值返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = _text(value).lower().replace(",", "")
    if text in UNLIMITED_TOKENS:
        return None
    match = _NUMBER_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def resolve_task_domain(task: dict[str, Any]) -> str:
    """采集任务的目标域名：优先任务自身字段，其次按采集源名称找采集源登记。"""
    direct = normalize_domain(task.get("目标域名"))
    if direct:
        return direct
    source_name = _text(task.get("采集源名称"))
    if not source_name:
        return ""
    for source in store.rows("datasource"):
        if source_name == _text(source.get("源端编号")):
            domain = normalize_domain(source.get("目标域名"))
            if domain:
                return domain
    return normalize_domain(source_name)


def _enabled_policies(domain: str) -> list[dict[str, Any]]:
    domain = normalize_domain(domain)
    if not domain:
        return []
    return [
        policy
        for policy in store.rows(RATE_MODULE)
        if _text(policy.get("status")) == POLICY_ENABLED
        and normalize_domain(policy.get("目标域名")) == domain
    ]


def _policy_usable(policy: dict[str, Any]) -> bool:
    return any(
        as_number(policy.get(field)) is not None
        for field in ("每秒请求数", "每日请求上限", "并发连接数")
    )


def _min_limit(policies: list[dict[str, Any]], field: str) -> float | None:
    values = [value for value in (as_number(p.get(field)) for p in policies) if value is not None]
    return min(values) if values else None


def _max_limit(policies: list[dict[str, Any]], field: str) -> float | None:
    values = [value for value in (as_number(p.get(field)) for p in policies) if value is not None]
    return max(values) if values else None


def domain_limits(policies: list[dict[str, Any]]) -> dict[str, Any]:
    """多条已启用策略时取最严格的一组，避免任一策略被突破。"""
    rps = _min_limit(policies, "每秒请求数")
    daily = _min_limit(policies, "每日请求上限")
    concurrency = _min_limit(policies, "并发连接数")
    cooldown = _max_limit(policies, "冷却秒数")
    return {
        "每秒请求数": None if rps is None else int(rps),
        "每日请求上限": None if daily is None else int(daily),
        "并发连接数": None if concurrency is None else int(concurrency),
        "冷却秒数": 60 if cooldown is None else max(0, int(cooldown)),
        "策略编号": [_text(policy.get("策略编号")) for policy in policies],
    }


def _state(domain: str) -> dict[str, Any]:
    domain = normalize_domain(domain)
    return _DOMAIN_STATE.setdefault(
        domain,
        {"date": date.today().isoformat(), "daily": 0, "window": [], "cooldown_until": 0.0, "reason": ""},
    )


def _refresh_state(state: dict[str, Any], now: float) -> None:
    today = date.today().isoformat()
    if state.get("date") != today:
        state["date"] = today
        state["daily"] = 0
    state["window"] = [stamp for stamp in state.get("window", []) if now - stamp < 1.0]


def _running_tasks(domain: str) -> list[dict[str, Any]]:
    domain = normalize_domain(domain)
    return [
        task
        for task in store.rows(TASK_MODULE)
        if _text(task.get("status")) == TASK_RUNNING
        and resolve_task_domain(task) == domain
    ]


def domain_usage(domain: str, now: float | None = None) -> dict[str, Any]:
    """域名的实时用量：冷却剩余、今日已分配、最近一秒请求数、执行中连接数。"""
    now = time.monotonic() if now is None else now
    state = _state(domain)
    _refresh_state(state, now)
    remaining = max(0.0, float(state.get("cooldown_until", 0.0)) - now)
    return {
        "冷却剩余秒数": round(remaining, 2),
        "冷却原因": state.get("reason", "") if remaining > 0 else "",
        "今日已分配请求": int(state.get("daily", 0)),
        "最近一秒请求数": len(state["window"]),
        "执行中连接数": len(_running_tasks(domain)),
    }


def proxy_eligible(proxy: dict[str, Any]) -> tuple[bool, str]:
    """代理节点是否具备出口资格：未被停用、健康状态可用。"""
    status = _text(proxy.get("status"))
    if _text(status).lower() in DISABLED_PROXY_STATUSES:
        return False, f"节点已停用（状态：{status}）"
    if status.lower() not in READY_PROXY_STATUSES:
        return False, f"节点状态为{status or '空'}，尚未就绪"
    health = _text(proxy.get("健康状态"))
    if not health:
        return False, "健康状态缺失，不能作为出口"
    lowered = health.lower()
    if any(token in lowered for token in DISABLED_HEALTH_TOKENS):
        return False, f"健康状态为{health}，不可用"
    if not any(token in lowered for token in HEALTHY_HEALTH_TOKENS):
        return False, f"健康状态为{health}，未确认可用"
    return True, ""


DEFAULT_NODE_BANDWIDTH_MBPS = 25.0


def proxy_capacity(proxy: dict[str, Any]) -> int | None:
    """节点可承载的连接数：带宽上限换算出的连接数与最大连接数取更严格的那个。

    带宽上限单位是 Mbps，按节点（或默认）的单连接带宽占用换算成可承载连接数；
    两者都没有配置时视为不限，返回 None。
    """
    caps: list[int] = []
    bandwidth = as_number(proxy.get("带宽上限"))
    if bandwidth is not None:
        per_connection = as_number(proxy.get("单连接带宽")) or DEFAULT_NODE_BANDWIDTH_MBPS
        caps.append(max(0, int(bandwidth // max(per_connection, 0.001))))
    max_connections = as_number(proxy.get("最大连接数"))
    if max_connections is not None:
        caps.append(max(0, int(max_connections)))
    return min(caps) if caps else None


def _node_key(proxy: dict[str, Any]) -> str:
    return _text(proxy.get("节点编号")) or str(proxy.get("id", ""))


def _task_node_key(task: dict[str, Any]) -> str:
    return _text(task.get("出口节点编号")) or _text(task.get("出口节点"))


def proxy_load(proxy: dict[str, Any]) -> int:
    key = _node_key(proxy)
    return sum(
        1
        for task in store.rows(TASK_MODULE)
        if _text(task.get("status")) == TASK_RUNNING and _task_node_key(task) == key
    )


def proxy_probe(proxy: dict[str, Any]) -> dict[str, Any]:
    ok, reason = proxy_eligible(proxy)
    capacity = proxy_capacity(proxy)
    load = proxy_load(proxy)
    free = None if capacity is None else max(0, capacity - load)
    return {
        "可用": ok and (free is None or free > 0),
        "原因": reason if not ok else ("已达到带宽上限" if free == 0 else ""),
        "承载任务数": load,
        "剩余连接数": free,
        "带宽无限": capacity is None,
    }


def _join_reasons(reasons: list[str]) -> str:
    """去重后拼成一句可读的原因，避免同一限制被重复说明。"""
    unique = [reason for reason in reasons if reason]
    unique = list(dict.fromkeys(unique))
    return "；".join(unique)


def _plan_failed(domain: str, reason: str, **extra: Any) -> dict[str, Any]:
    plan = {
        "domain": domain,
        "available": False,
        "reason": reason,
        "出口可用性": "不可调度",
        "proxy_node": None,
        "candidates": extra.pop("candidates", []),
        "policy_ids": [],
        "limits": {},
        "usage": {},
        "available_connections": 0,
    }
    plan.update(extra)
    return plan


def evaluate_task(task: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """判定任务能否获得出口；只读，不占用任何配额。调用方需持有锁。"""
    now = time.monotonic() if now is None else now
    domain = resolve_task_domain(task)
    if not domain:
        return _plan_failed("", "未解析到目标域名，无法选择出口")

    enabled = _enabled_policies(domain)
    if not enabled:
        return _plan_failed(domain, "目标域名没有已启用的限速策略，全部策略不可用")
    usable = [policy for policy in enabled if _policy_usable(policy)]
    if not usable:
        return _plan_failed(domain, "已启用的限速策略参数不可用，无法计算请求额度")

    limits = domain_limits(usable)
    usage = domain_usage(domain, now)
    # 限速策略自身的限制先判定：冷却期、每日上限与每秒请求数都不再分配新请求，
    # 只有这些都通过后才轮到“还能占几个连接”。
    policy_reasons: list[str] = []
    if usage["冷却剩余秒数"] > 0:
        detail = usage["冷却原因"] or "上游限速信号"
        policy_reasons.append(f"目标域名处于限速冷却期（剩余 {usage['冷却剩余秒数']:.0f} 秒：{detail}）")
    daily_limit = limits["每日请求上限"]
    if daily_limit is not None and usage["今日已分配请求"] >= daily_limit:
        policy_reasons.append(f"已达到每日请求上限（{usage['今日已分配请求']}/{daily_limit}）")
    rps_limit = limits["每秒请求数"]
    if rps_limit is not None and usage["最近一秒请求数"] >= rps_limit:
        policy_reasons.append(f"每秒请求数已达上限（{usage['最近一秒请求数']}/{rps_limit}）")
    if policy_reasons:
        return _plan_failed(
            domain,
            _join_reasons(policy_reasons),
            policy_ids=limits["策略编号"],
            limits=limits,
            usage=usage,
        )

    concurrency_limit = limits["并发连接数"]
    reasons: list[str] = []
    if concurrency_limit is not None and usage["执行中连接数"] >= concurrency_limit:
        reasons.append(f"并发连接数已达上限（{usage['执行中连接数']}/{concurrency_limit}）")

    candidates: list[dict[str, Any]] = []
    healthy_nodes: list[dict[str, Any]] = []
    for proxy in store.rows(PROXY_MODULE):
        probe = proxy_probe(proxy)
        candidate = {
            "id": proxy.get("id"),
            "节点编号": _node_key(proxy),
            "代理地址": _text(proxy.get("代理地址")),
            "代理端口": _text(proxy.get("代理端口")),
            **probe,
        }
        candidates.append(candidate)
        if probe["可用"]:
            healthy_nodes.append(candidate)

    if not healthy_nodes:
        saturating = [
            proxy
            for proxy in store.rows(PROXY_MODULE)
            if proxy_eligible(proxy)[0] and proxy_probe(proxy)["剩余连接数"] == 0
        ]
        if saturating:
            reasons.append("健康代理节点都已达到带宽上限")
        else:
            reasons.append("没有健康可用且未达到带宽上限的代理节点")
        return _plan_failed(
            domain,
            _join_reasons(reasons),
            candidates=candidates,
            policy_ids=limits["策略编号"],
            limits=limits,
            usage=usage,
        )

    # 限速策略是并发连接数的判定依据；节点带宽只在其自身可用容量内起作用。
    domain_free = None if concurrency_limit is None else max(0, concurrency_limit - usage["执行中连接数"])
    node_free_values = [node["剩余连接数"] for node in healthy_nodes]
    if any(value is None for value in node_free_values):
        node_free: int | None = None
    else:
        node_free = sum(int(value) for value in node_free_values)
    if domain_free is None:
        available_connections = node_free
    elif node_free is None:
        available_connections = domain_free
    else:
        available_connections = min(domain_free, node_free)

    if available_connections == 0 and concurrency_limit is None:
        reasons.append("代理节点可用连接数已经用尽")
    elif available_connections == 0 and not reasons:
        reasons.append("可用连接数已经用尽")

    if reasons:
        return _plan_failed(
            domain,
            _join_reasons(reasons),
            candidates=candidates,
            policy_ids=limits["策略编号"],
            limits=limits,
            usage=usage,
            available_connections=available_connections,
        )

    node = min(healthy_nodes, key=lambda item: (item["承载任务数"], int(item.get("id") or 0)))
    return {
        "domain": domain,
        "available": True,
        "reason": "",
        "出口可用性": "可调度",
        "proxy_node": node,
        "candidates": candidates,
        "policy_ids": limits["策略编号"],
        "limits": limits,
        "usage": usage,
        "available_connections": available_connections,
    }


def availability_for_task(task: dict[str, Any]) -> dict[str, Any]:
    """列表展示口径：不改变任务，只补充当前出口可用性与原因。"""
    with _LOCK:
        status = _text(task.get("status"))
        domain = resolve_task_domain(task)
        snapshot: dict[str, Any] = {
            "目标域名": domain,
            "出口节点编号": _text(task.get("出口节点编号")),
            "出口代理地址": _text(task.get("出口代理地址")),
        }
        if status == TASK_RUNNING:
            node_key = _task_node_key(task)
            node = next(
                (proxy for proxy in store.rows(PROXY_MODULE) if _node_key(proxy) == node_key),
                None,
            )
            node_ok = True
            node_reason = ""
            if node is None:
                node_ok, node_reason = False, "出口节点已不存在"
            else:
                node_ok, node_reason = proxy_eligible(node)
            snapshot.update(
                {
                    "出口可用性": "执行中" if node_ok else "执行中（出口异常）",
                    "调度原因": "" if node_ok else node_reason,
                    **domain_usage(domain),
                }
            )
            return snapshot
        if status in TERMINAL_TASK_STATUSES:
            snapshot.update({"出口可用性": status, "调度原因": ""})
            return snapshot
        plan = evaluate_task(task)
        snapshot.update(
            {
                "出口可用性": plan["出口可用性"],
                "调度原因": plan["reason"],
                "可用连接数": plan["available_connections"],
                **plan.get("usage", {}),
            }
        )
        return snapshot


def task_snapshot(task: dict[str, Any]) -> dict[str, Any]:
    """任务清单里的一行：原始字段不动，叠加实时调度口径。"""
    return {**task, **availability_for_task(task)}


def start_task(task: dict[str, Any]) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
    """占用出口与限速额度；判定和占用在同一把锁内完成，避免争用穿透。"""
    with _LOCK:
        now = time.monotonic()
        plan = evaluate_task(task, now)
        if not plan["available"]:
            task["调度原因"] = plan["reason"]
            task["出口可用性"] = "不可调度"
            return None, plan["reason"], plan

        domain = plan["domain"]
        state = _state(domain)
        _refresh_state(state, now)
        state["daily"] = int(state.get("daily", 0)) + 1
        state["window"].append(now)

        node = plan["proxy_node"]
        assert node is not None
        task.update(
            {
                "目标域名": domain,
                "status": TASK_RUNNING,
                "pending": False,
                "abnormal": False,
                "出口节点编号": node["节点编号"],
                "出口代理地址": node["代理地址"],
                "出口端口": node["代理端口"],
                "出口节点id": node.get("id"),
                "出口代理": f"{node['代理地址']}:{node['代理端口']}".strip(":"),
                "出口分配时间": datetime.now().isoformat(timespec="seconds"),
                "出口状态": "已分配",
                "出口可用性": "执行中",
                "调度原因": "",
            }
        )
        return task, f"采集任务已启动，出口 {node['节点编号']}（{task['出口代理']}）", plan


def finish_task(task: dict[str, Any], status: str) -> dict[str, Any]:
    """结束任务：保留原出口字段作为历史记录，只补充结束标记。"""
    with _LOCK:
        task["status"] = status
        task["pending"] = False
        task["出口状态"] = "已释放" if _text(task.get("出口节点编号")) else ""
        task["出口结束时间"] = datetime.now().isoformat(timespec="seconds")
        task["出口可用性"] = status
        task["调度原因"] = ""
        return task


def build_completed_log(task: dict[str, Any], log_id: int) -> dict[str, Any]:
    """完成任务时生成日志快照；出口字段在这里定格，后续配置变化不回写。"""
    now = datetime.now().isoformat(timespec="seconds")
    task_no = _text(task.get("任务编号")) or f"TASK-{task.get('id')}"
    return {
        "id": log_id,
        "status": "已记录",
        "pending": True,
        "abnormal": False,
        "日志编号": f"STOR-{task_no}",
        "关联任务": task_no,
        "开始时间": _text(task.get("出口分配时间")) or now,
        "结束时间": now,
        "请求总数": 0,
        "成功条数": 0,
        "失败条数": 0,
        "平均响应毫秒": 0,
        "目标域名": _text(task.get("目标域名")),
        "出口节点编号": _text(task.get("出口节点编号")),
        "出口代理地址": _text(task.get("出口代理地址")),
        "出口记录时间": now,
        "任务状态快照": TASK_DONE,
    }


def record_feedback(
    task: dict[str, Any],
    *,
    failed: bool,
    reason: str = "",
    now: float | None = None,
) -> dict[str, Any]:
    """记录请求结果：失败信号触发该域名的限速冷却，冷却期内不再分配新请求。"""
    now = time.monotonic() if now is None else now
    with _LOCK:
        domain = resolve_task_domain(task)
        if not domain:
            return {"domain": "", "cooldown_seconds": 0, "message": "任务没有目标域名，无法记录冷却"}
        if not failed:
            return {"domain": domain, "cooldown_seconds": 0, "message": "已记录成功结果，不触发冷却"}
        policies = [policy for policy in _enabled_policies(domain) if _policy_usable(policy)]
        limits = domain_limits(policies)
        seconds = limits["冷却秒数"] if policies else 60
        state = _state(domain)
        _refresh_state(state, now)
        state["cooldown_until"] = max(float(state.get("cooldown_until", 0.0)), now + seconds)
        state["reason"] = reason or "上游返回限速或失败信号"
        task["abnormal"] = True
        return {
            "domain": domain,
            "cooldown_seconds": seconds,
            "cooldown_until": state["cooldown_until"],
            "message": f"目标域名 {domain} 进入 {seconds} 秒限速冷却期",
        }


def proxy_snapshot(proxy: dict[str, Any]) -> dict[str, Any]:
    with _LOCK:
        probe = proxy_probe(proxy)
    return {
        **proxy,
        "承载任务数": probe["承载任务数"],
        "剩余连接数": "不限" if probe["剩余连接数"] is None else probe["剩余连接数"],
        "可调度": "是" if probe["可用"] else "否",
        "不可用原因": probe["原因"],
    }


def policy_snapshot(policy: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """限速策略清单的实时口径：策略自身状态 + 域名的当前用量。"""
    now = time.monotonic() if now is None else now
    with _LOCK:
        domain = normalize_domain(policy.get("目标域名"))
        enabled = _text(policy.get("status")) == POLICY_ENABLED
        usable = _policy_usable(policy)
        usage = domain_usage(domain, now) if domain else {}
        sibling = len(_enabled_policies(domain)) if domain else 0
        reasons: list[str] = []
        if not enabled:
            reasons.append(f"策略未启用（状态：{_text(policy.get('status'))}）")
        if not usable:
            reasons.append("策略参数无法解析出请求额度")
        if usage:
            limits = domain_limits([policy])
            if limits["每日请求上限"] is not None and usage["今日已分配请求"] >= limits["每日请求上限"]:
                reasons.append("该域名已达到每日请求上限")
            if usage["冷却剩余秒数"] > 0:
                reasons.append("该域名处于限速冷却期")
        return {
            **policy,
            "调度可用性": "不可用" if reasons else "可用",
            "不可用原因": _join_reasons(reasons),
            "同域名启用策略数": sibling,
            "今日已分配请求": usage.get("今日已分配请求", 0),
            "执行中连接数": usage.get("执行中连接数", 0),
            "冷却剩余秒数": usage.get("冷却剩余秒数", 0),
        }


def reset_domain_state(domain: str | None = None) -> None:
    """测试与运维重置入口：清空指定域名的用量与冷却。"""
    with _LOCK:
        if domain is None:
            _DOMAIN_STATE.clear()
            return
        _DOMAIN_STATE.pop(normalize_domain(domain), None)
