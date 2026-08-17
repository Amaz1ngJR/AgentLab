"""ApprovalBroker 的并发、超时和兼容适配测试。"""
import asyncio
import threading
import time

from app.agent.approval import ApprovalResult
from app.agent.approval_broker import ApprovalBroker, BrokerApprovalPolicy


def test_broker_publishes_stable_request_and_accepts_decision():
    broker = ApprovalBroker(default_timeout=1)
    seen = []
    broker.subscribe(seen.append)

    def approve_after_publish():
        while not seen:
            time.sleep(0.001)
        assert broker.resolve(seen[0].request_id, "approve")

    worker = threading.Thread(target=approve_after_publish)
    worker.start()
    result = broker.request("write_file", {"path": "x"})
    worker.join()

    assert result.approved
    assert seen[0].request_id.startswith("approval-")
    assert broker.pending() == []


def test_first_frontend_decision_wins():
    broker = ApprovalBroker(default_timeout=1)
    seen = []
    broker.subscribe(seen.append)
    result_box = []
    worker = threading.Thread(
        target=lambda: result_box.append(broker.request("shell", {"command": "pwd"}))
    )
    worker.start()
    while not seen:
        time.sleep(0.001)

    request_id = seen[0].request_id
    assert broker.resolve(request_id, "deny", feedback="not now")
    assert broker.resolve(request_id, "approve") is False
    worker.join()
    assert result_box[0].approved is False
    assert result_box[0].feedback == "not now"


def test_timeout_fails_closed_and_removes_pending():
    broker = ApprovalBroker(default_timeout=0.01)
    result = broker.request("write_file", {})
    assert result.approved is False
    assert result.cancelled is True
    assert "timed out" in (result.feedback or "")
    assert broker.pending() == []


def test_close_wakes_all_waiters():
    broker = ApprovalBroker(default_timeout=None)
    started = threading.Event()
    result_box = []
    broker.subscribe(lambda _: started.set())
    worker = threading.Thread(
        target=lambda: result_box.append(broker.request("write_file", {}))
    )
    worker.start()
    assert started.wait(1)
    broker.close()
    worker.join(1)
    assert not worker.is_alive()
    assert result_box[0].cancelled is True


def test_fallback_policy_keeps_cli_synchronous_behavior():
    class Fallback:
        def request(self, action, args):
            return action == "write_file" and args["path"] == "x"

    policy = BrokerApprovalPolicy(ApprovalBroker(), fallback=Fallback())
    assert policy.request("write_file", {"path": "x"}) is True


def test_async_request_does_not_block_event_loop():
    async def scenario():
        broker = ApprovalBroker(default_timeout=1)
        seen = []
        broker.subscribe(seen.append)
        task = asyncio.create_task(broker.request_async("write_file", {}))
        while not seen:
            await asyncio.sleep(0)
        broker.resolve(seen[0].request_id, result=ApprovalResult(True))
        return await task

    assert asyncio.run(scenario()).approved is True
