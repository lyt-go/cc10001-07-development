"""代理出口调度与限速联动的行为测试。

运行：cd backend && .venv/bin/pip install httpx && .venv/bin/python -m unittest \
      discover tests -v
"""
from __future__ import annotations

import threading
import unittest

from fastapi.testclient import TestClient

from app.main import app
from app.seed import SEED_ROWS
from app.services import dispatch
from app.store import store


class DispatchTest(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例从示例数据重新开始，避免共享的内存仓库互相污染。
        store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
        dispatch.reset_domain_state()
        self.client = TestClient(app)

    # ---- 辅助方法 -------------------------------------------------------
    def add_task(self, no: str, domain: str, status: str = "待调度", **extra: object) -> int:
        rows = store.rows("taskcrawl")
        task_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        row = {
            "id": task_id,
            "status": status,
            "pending": True,
            "abnormal": False,
            "任务编号": no,
            "采集源名称": "DATA-0001",
            "规则编号": "PARS-0001",
            "目标域名": domain,
        }
        row.update(extra)
        rows.append(row)
        return task_id

    def act(self, task_id: int, action: str) -> dict:
        return self.client.post(
            f"/api/taskcrawl/{task_id}/actions", json={"values": {"action": action}}
        ).json()

    def task_status(self, task_id: int) -> str:
        return str(store.find("taskcrawl", task_id)["status"])

    # ---- 用例 -----------------------------------------------------------
    def test_no_enabled_policy_stays_pending_without_direct_connection(self) -> None:
        """目标域名没有已启用策略时停在待调度，并且不能退化成直连。"""
        result = self.act(2, "启动任务")
        self.assertFalse(result["ok"])
        self.assertEqual(self.task_status(2), "待调度")
        self.assertIn("没有已启用的限速策略", result["message"])
        self.assertIsNone(store.find("taskcrawl", 2).get("出口节点编号"))

    def test_only_healthy_enabled_nodes_are_selected(self) -> None:
        """草稿、探测中、健康异常、已下线、带宽已满的节点都不会被选中。"""
        result = self.act(1, "启动任务")
        self.assertTrue(result["ok"])
        self.assertEqual(store.find("taskcrawl", 1)["出口节点编号"], "PROX-0003")

    def test_no_healthy_node_stays_pending(self) -> None:
        for proxy in store.rows("proxyconfig"):
            proxy["status"] = "已下线"
        task_id = self.add_task("TASK-NONODE", "news.example.com")
        result = self.act(task_id, "启动任务")
        self.assertFalse(result["ok"])
        self.assertEqual(self.task_status(task_id), "待调度")
        self.assertIsNone(store.find("taskcrawl", task_id).get("出口代理"))

    def test_daily_limit_blocks_new_requests(self) -> None:
        """api.example.com 每日上限 20、并发 1：每次完成后可继续，额度用尽即停。"""
        store.find("ratelimiter", 4)["每秒请求数"] = 100  # 排除每秒窗口的干扰，只验证每日上限
        completed = 0
        for index in range(21):
            task_id = self.add_task(f"TASK-D{index}", "api.example.com")
            result = self.act(task_id, "启动任务")
            if not result["ok"]:
                self.assertIn("每日请求上限", result["message"])
                self.assertEqual(self.task_status(task_id), "待调度")
                break
            self.assertTrue(self.act(task_id, "完成任务")["ok"])
            completed += 1
        self.assertEqual(completed, 20)

    def test_cooldown_blocks_new_requests_after_failure(self) -> None:
        task_id = self.add_task("TASK-COOL", "shop.example.com")
        self.assertTrue(self.act(task_id, "启动任务")["ok"])
        feedback = self.client.post(
            f"/api/taskcrawl/{task_id}/feedback",
            json={"values": {"failed": True, "reason": "429"}},
        ).json()
        self.assertTrue(feedback["ok"])
        other = self.add_task("TASK-COOL-2", "shop.example.com")
        blocked = self.act(other, "启动任务")
        self.assertFalse(blocked["ok"])
        self.assertIn("冷却期", blocked["message"])
        self.assertEqual(self.task_status(other), "待调度")

    def test_policy_concurrency_wins_over_node_capacity(self) -> None:
        """节点能开 10 个连接，但策略只允许 1 个，以策略为准。"""
        store.find("ratelimiter", 3)["并发连接数"] = 1
        store.find("proxyconfig", 3)["最大连接数"] = 10
        first = self.add_task("TASK-C1", "shop.example.com")
        second = self.add_task("TASK-C2", "shop.example.com")
        self.assertTrue(self.act(first, "启动任务")["ok"])
        blocked = self.act(second, "启动任务")
        self.assertFalse(blocked["ok"])
        self.assertIn("并发连接数已达上限", blocked["message"])

    def test_node_bandwidth_cap_limits_connections(self) -> None:
        """所有可用节点带宽用尽时不再分配，即使策略仍有并发额度。"""
        for proxy_id in (1, 2, 4, 5, 6):
            store.find("proxyconfig", proxy_id)["status"] = "已下线"
        # 种子数据里 PROX-0003 已被执行中的 TASK-0003 占用一个连接，这里再留一个空位。
        store.find("proxyconfig", 3)["最大连接数"] = 2
        first = self.add_task("TASK-BW1", "news.example.com")
        second = self.add_task("TASK-BW2", "news.example.com")
        self.assertTrue(self.act(first, "启动任务")["ok"])
        blocked = self.act(second, "启动任务")
        self.assertFalse(blocked["ok"])
        self.assertIn("带宽上限", blocked["message"])

    def test_concurrent_start_only_one_gets_last_connection(self) -> None:
        """两个任务并发争用最后一个连接时，只允许一个成功。"""
        store.rows("taskcrawl").append(
            {
                "id": 50,
                "status": "执行中",
                "pending": False,
                "abnormal": False,
                "任务编号": "TASK-HOLD",
                "采集源名称": "DATA-0001",
                "规则编号": "PARS-0001",
                "目标域名": "shop.example.com",
                "出口节点编号": "PROX-0003",
            }
        )
        task_ids = [self.add_task(f"TASK-R{index}", "shop.example.com") for index in range(6)]
        results: list[bool] = []
        lock = threading.Lock()

        def start(task_id: int) -> None:
            outcome = self.act(task_id, "启动任务")["ok"]
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=start, args=(task_id,)) for task_id in task_ids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sum(1 for item in results if item), 1)
        self.assertEqual(len(dispatch._running_tasks("shop.example.com")), 2)

    def test_list_reflects_availability_after_config_change(self) -> None:
        task_id = self.add_task("TASK-SYNC", "news.example.com")
        before = self.client.get("/api/taskcrawl").json()["items"]
        before_row = next(item for item in before if item["id"] == task_id)
        self.assertEqual(before_row["出口可用性"], "可调度")

        self.client.post("/api/proxyconfig/3/actions", json={"values": {"action": "下线节点"}})
        self.client.post("/api/proxyconfig/6/actions", json={"values": {"action": "发起探测"}})
        after = self.client.get("/api/taskcrawl").json()["items"]
        after_row = next(item for item in after if item["id"] == task_id)
        self.assertEqual(after_row["出口可用性"], "不可调度")
        self.assertIn("代理节点", after_row["调度原因"])

    def test_completed_logs_keep_original_exit_record(self) -> None:
        task_id = self.add_task("TASK-LOG", "shop.example.com")
        self.assertTrue(self.act(task_id, "启动任务")["ok"])
        node_key = store.find("taskcrawl", task_id)["出口节点编号"]
        node_address = store.find("taskcrawl", task_id)["出口代理地址"]
        self.assertTrue(self.act(task_id, "完成任务")["ok"])

        log = store.rows("storagelog")[-1]
        self.assertEqual(log["出口节点编号"], node_key)
        self.assertEqual(log["出口代理地址"], node_address)

        store.find("proxyconfig", 3)["代理地址"] = "10.9.9.9"
        store.find("ratelimiter", 3)["并发连接数"] = 5
        self.assertEqual(log["出口代理地址"], node_address)


if __name__ == "__main__":
    unittest.main()
