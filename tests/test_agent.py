import json
import os
import unittest
import uuid

import httpx
import redis

from cloudon_admin_integration.agent import (
    AGENT_KEY_HEADER,
    CONSUMER_GROUP,
    Agent,
    AgentSettings,
    KeyRefused,
    PanelUnavailable,
)

TEST_REDIS_PORT = int(os.getenv("TEST_REDIS_PORT") or 16399)


def _record(**overrides):
    record = {"schema_version": 1, "log_id": str(uuid.uuid4()), "ts": "2026-10-27T10:00:00Z",
              "service": "svc", "kind": "request", "status": 500}
    record.update(overrides)
    return record


class _Panel:
    """Stands in for the panel's batch endpoint."""

    def __init__(self):
        self.batches = []
        self.answers = []  # (status, body, headers) to give, in order; then 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.batches.append(body)
        assert request.headers[AGENT_KEY_HEADER] == "k"
        if self.answers:
            status, payload, headers = self.answers.pop(0)
            if callable(payload):
                payload = payload(body)
            return httpx.Response(status, json=payload, headers=headers)
        return httpx.Response(200, json={"accepted": len(body["records"]), "duplicates": 0, "rejected": []})


class AgentTests(unittest.TestCase):
    """Against a real Redis (TEST_REDIS_PORT, default 16399); skipped without one."""

    def setUp(self):
        self.redis = redis.Redis(port=TEST_REDIS_PORT, decode_responses=True, socket_connect_timeout=0.2)
        try:
            self.redis.ping()
        except redis.RedisError:
            self.skipTest("no test Redis")
        self.stream = f"test:logs:{uuid.uuid4().hex}"
        self.addCleanup(self.redis.delete, self.stream, f"{self.stream}:dead")
        self.panel = _Panel()
        cfg = AgentSettings(
            panel_url="http://panel", agent_key="k", redis_host="localhost", redis_port=TEST_REDIS_PORT,
            redis_db=0, redis_password=None, stream=self.stream, dead_stream=f"{self.stream}:dead",
            consumer="test", block_ms=10, reclaim_seconds=0, heartbeat_seconds=60, http_timeout_seconds=1,
        )
        self.agent = Agent(cfg, redis_client=self.redis, http=httpx.Client(transport=httpx.MockTransport(self.panel)))
        self.agent.ensure_group()

    def _add(self, *records):
        for record in records:
            self.redis.xadd(self.stream, {"record": record if isinstance(record, str) else json.dumps(record)})

    def test_records_are_deleted_once_the_panel_answers(self):
        self._add(_record(), _record())
        self.agent.step()
        [batch] = self.panel.batches
        self.assertEqual(len(batch["records"]), 2)
        self.assertEqual(batch["agent"]["outbox_depth"], 2)
        self.assertEqual(batch["agent"]["pending"], 2)
        self.assertEqual(self.redis.xlen(self.stream), 0)
        self.assertEqual(self.redis.xpending(self.stream, CONSUMER_GROUP)["pending"], 0)
        self.assertIsNotNone(self.agent.last_shipped_at)

    def test_records_stay_when_the_panel_is_down_and_go_on_the_next_pass(self):
        self._add(_record())
        self.panel.answers.append((503, {}, {}))
        with self.assertRaises(PanelUnavailable):
            self.agent.step()
        self.assertEqual(self.redis.xlen(self.stream), 1)
        self.agent.step()  # reclaimed, since nothing else answered for it
        self.assertEqual(len(self.panel.batches), 2)
        self.assertEqual(self.panel.batches[0]["records"], self.panel.batches[1]["records"])
        self.assertEqual(self.redis.xlen(self.stream), 0)

    def test_rejected_and_unreadable_records_are_set_aside(self):
        self._add(_record(), "not json", _record(status=None))
        self.panel.answers.append(
            (200, {"accepted": 1, "duplicates": 0, "rejected": [{"index": 1, "errors": {"kind": ["bad"]}}]}, {})
        )
        self.agent.step()
        self.assertEqual(len(self.panel.batches[0]["records"]), 2)
        self.assertEqual(self.redis.xlen(self.stream), 0)
        dead = self.redis.xrange(f"{self.stream}:dead")
        self.assertEqual(len(dead), 2)
        self.assertEqual(dead[0][1]["record"], "not json")
        self.assertIn("bad", dead[1][1]["reason"])

    def test_too_large_batches_are_split(self):
        self._add(_record(), _record(), _record())
        self.panel.answers.append((413, {}, {}))
        self.agent.step()
        self.assertEqual([len(b["records"]) for b in self.panel.batches], [3, 1, 2])
        self.assertEqual(self.redis.xlen(self.stream), 0)

    def test_429_waits_as_long_as_asked(self):
        self._add(_record())
        self.panel.answers.append((429, {}, {"Retry-After": "17"}))
        with self.assertRaises(PanelUnavailable) as caught:
            self.agent.step()
        self.assertEqual(self.agent.wait_after_failure(caught.exception), 17)

    def test_refused_key_keeps_the_records(self):
        self._add(_record())
        self.panel.answers.append((401, {}, {}))
        with self.assertRaises(KeyRefused):
            self.agent.step()
        self.assertEqual(self.redis.xlen(self.stream), 1)

    def test_heartbeat_when_idle(self):
        self.agent.step()
        [batch] = self.panel.batches
        self.assertEqual(batch["records"], [])
        self.assertEqual(batch["agent"]["outbox_depth"], 0)
        self.agent.step()
        self.assertEqual(len(self.panel.batches), 1)  # not again until the heartbeat is due

    def test_byte_limit_splits_into_batches(self):
        from cloudon_admin_integration import agent as agent_module

        big = "x" * 600_000
        self._add(_record(extra={"blob": big}), _record(extra={"blob": big}))
        self.agent.step()
        self.assertEqual([len(b["records"]) for b in self.panel.batches], [1, 1])
        self.assertLess(len(big), agent_module.MAX_BATCH_BYTES)


if __name__ == "__main__":
    unittest.main()
