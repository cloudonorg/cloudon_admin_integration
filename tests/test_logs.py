import asyncio
import json
import os
import unittest
from unittest.mock import patch

import redis
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from cloudon_admin_integration import logs
from cloudon_admin_integration.logs import emit_log, log_context, log_step, wire_logging

TEST_REDIS_PORT = int(os.getenv("TEST_REDIS_PORT") or 16399)


def _app():
    app = FastAPI()

    @app.get("/ok")
    async def ok():
        return {"ok": True}

    @app.get("/orders/{order_id}")
    async def order(order_id: int):
        log_step("reading ERP")
        log_context(company_id="c-1", supplier="acme")
        raise HTTPException(status_code=404, detail={"reason": "order_missing"})

    @app.get("/crash")
    async def crash():
        log_step("parsing")
        raise ValueError("bad row")

    @app.get("/sync-crash")
    def sync_crash():
        log_step("sync work")
        raise RuntimeError("from a thread")

    @app.get("/denied")
    async def denied():
        raise HTTPException(status_code=403, detail="no")

    @app.get("/docs/thing")
    async def excluded():
        raise HTTPException(status_code=500)

    @app.get("/event")
    async def event():
        await emit_log("warning", "ERP slow", "took 9 s", step="reading ERP", wait=True)
        return {"ok": True}

    wire_logging(app, excluded_paths=["/docs"])
    return app


class LoggingMiddlewareTests(unittest.TestCase):
    def setUp(self):
        self.records = []
        self.patch = patch.object(logs._writer, "write_later", self.records.append)
        self.patch.start()
        self.client = TestClient(_app(), raise_server_exceptions=False)

    def tearDown(self):
        self.patch.stop()

    def test_success_is_not_logged_but_gets_a_request_id(self):
        response = self.client.get("/ok")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["x-request-id"])
        self.assertEqual(self.records, [])

    def test_failed_request_records_route_step_and_context(self):
        response = self.client.get("/orders/42", headers={"X-Request-ID": "abc", "X-Branch-Code": "3"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.headers["x-request-id"], "abc")
        [record] = self.records
        self.assertEqual(record["kind"], "request")
        self.assertEqual(record["schema_version"], 1)
        self.assertEqual(record["status"], 404)
        self.assertEqual(record["method"], "GET")
        self.assertEqual(record["path"], "/orders/42")
        self.assertEqual(record["route"], "/orders/{order_id}")
        self.assertEqual(record["step"], "reading ERP")
        self.assertEqual(record["request_id"], "abc")
        self.assertEqual(record["company_id"], "c-1")
        self.assertEqual(record["branch_code"], 3)
        self.assertEqual(record["extra"], {"supplier": "acme"})
        self.assertEqual(record["response"], {"detail": {"reason": "order_missing"}})
        self.assertNotIn("error", record)

    def test_exception_is_recorded_with_its_traceback(self):
        response = self.client.get("/crash")
        self.assertEqual(response.status_code, 500)
        [record] = self.records
        self.assertEqual(record["status"], 500)
        self.assertEqual(record["step"], "parsing")
        self.assertEqual(record["error"]["type"], "ValueError")
        self.assertEqual(record["error"]["message"], "bad row")
        self.assertIn("raise ValueError", record["error"]["traceback"])

    def test_step_set_in_a_sync_endpoint_reaches_the_record(self):
        self.client.get("/sync-crash")
        [record] = self.records
        self.assertEqual(record["step"], "sync work")
        self.assertEqual(record["error"]["type"], "RuntimeError")

    def test_denied_and_excluded(self):
        self.client.get("/denied")
        self.client.get("/docs/thing")
        self.assertEqual([r["status"] for r in self.records], [403])

    def test_skip_statuses(self):
        app = FastAPI()

        @app.get("/x")
        async def x():
            raise HTTPException(status_code=404)

        wire_logging(app, skip_statuses=[404])
        TestClient(app).get("/x")
        self.assertEqual(self.records, [])

    def test_identity_comes_from_the_environment(self):
        with patch.object(logs, "log_settings", logs.LogSettings(
            stream="s", maxlen=10, project_id="7", docker_id="12", service="orders-api",
            container="orders_api", module="pharmacy_one",
        )):
            self.client.get("/denied")
        [record] = self.records
        self.assertEqual(
            (record["project_id"], record["docker_id"], record["service"], record["container"], record["module"]),
            ("7", "12", "orders-api", "orders_api", "pharmacy_one"),
        )


class StreamWriteTests(unittest.TestCase):
    """Against a real Redis (TEST_REDIS_PORT, default 16399); skipped without one."""

    def setUp(self):
        self.redis = redis.Redis(port=TEST_REDIS_PORT, decode_responses=True, socket_connect_timeout=0.2)
        try:
            self.redis.ping()
        except redis.RedisError:
            self.skipTest("no test Redis")
        self.stream = "test:cloudon:logs"
        self.redis.delete(self.stream)

    def _writer(self):
        writer = logs._Writer()
        writer._redis = aioredis.Redis(port=TEST_REDIS_PORT)
        return writer

    def test_event_lands_on_the_stream(self):
        writer = self._writer()
        with patch.object(logs, "_writer", writer), patch.object(
            logs, "log_settings", logs.LogSettings(self.stream, 1000, None, None, "svc", None, None)
        ):
            asyncio.run(self._emit_and_close(writer))
        [(_, fields)] = self.redis.xrange(self.stream)
        record = json.loads(fields["record"])
        self.assertEqual((record["kind"], record["level"], record["title"]), ("event", "WARNING", "ERP slow"))

    async def _emit_and_close(self, writer):
        await emit_log("warning", "ERP slow", wait=True)
        await writer.close()

    def test_unreachable_redis_is_swallowed(self):
        async def go():
            writer = logs._Writer()
            writer._redis = aioredis.Redis(port=1)
            return await writer.write({"a": 1})

        self.assertFalse(asyncio.run(go()))


if __name__ == "__main__":
    unittest.main()
