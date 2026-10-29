"""The Admin Panel agent: one per server, beside the server's Admin Panel Redis.

It takes the log records the server's services append to `cloudon:logs` and
delivers them to the panel in batches. A record is deleted from the stream only
after the panel has answered for it — stored, already stored, or refused — so a
panel that is down, slow or restarting costs a delay, never a log.

    python -m cloudon_admin_integration agent

Environment:

    ADMIN_PANEL_AGENT_KEY   this server's agent key, issued by the panel (required)
    DJANGO_API_URL          the panel (default: the CloudOn panel)
    CLOUDON_REDIS_HOST/PORT/DB/PASSWORD   the server's Admin Panel Redis
                            (REDIS_* when none of those is set)

How it behaves when things go wrong:

- Records are read through a consumer group, so a record handed out but not
  answered for stays pending, and is taken back after AGENT_RECLAIM_SECONDS —
  by this agent after a restart, or by another if there are two.
- A record the panel refuses, or that is not readable at all, is moved to
  `cloudon:logs:dead` with the reason, and the rest carry on.
- A batch the panel finds too large is split in two until it fits.
- 429 waits as long as the panel asks; other failures back off, with jitter,
  up to five minutes. A refused key is retried every minute, since it is fixed
  on the panel without touching this server.
- Every batch carries the agent's report — how much is waiting, how old the
  oldest is, how much is dead — which is what the panel's health view shows.
  With nothing to send, the report still goes out every AGENT_HEARTBEAT_SECONDS.
"""

from __future__ import annotations

import json
import logging
import os
import random
import signal
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as package_version
from typing import Any

import httpx
import redis

from cloudon_admin_integration.config import redis_env
from cloudon_admin_integration.logs import DEFAULT_LOG_STREAM

logger = logging.getLogger("cloudon_admin_integration.agent")

DEFAULT_ADMIN_PANEL_URL = "https://devadminpanel2.cloudon.gr"
AGENT_BATCH_PATH = "/api/system-logs/agent-batch/"
AGENT_KEY_HEADER = "X-Agent-Key"
CONSUMER_GROUP = "admin_panel_agent"

#: The panel takes at most this many records per batch.
MAX_BATCH_RECORDS = 500
#: Kept well under the panel's 2.5 MB request limit.
MAX_BATCH_BYTES = 1_000_000
DEAD_STREAM_MAXLEN = 10_000
MAX_BACKOFF_SECONDS = 300
REFUSED_KEY_RETRY_SECONDS = 60


def _env(name: str, default: str | None = None) -> str | None:
    return (os.getenv(name) or "").strip() or default


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _agent_version() -> str:
    try:
        return package_version("cloudon-admin-integration")
    except PackageNotFoundError:
        return "unknown"


@dataclass(frozen=True)
class AgentSettings:
    panel_url: str
    agent_key: str
    redis_host: str
    redis_port: int
    redis_db: int
    redis_password: str | None
    stream: str
    dead_stream: str
    consumer: str
    block_ms: int
    reclaim_seconds: int
    heartbeat_seconds: int
    http_timeout_seconds: float

    @classmethod
    def from_env(cls) -> "AgentSettings":
        agent_key = _env("ADMIN_PANEL_AGENT_KEY")
        if not agent_key:
            raise SystemExit("ADMIN_PANEL_AGENT_KEY is not set; issue one for this server from the panel.")
        stream = _env("CLOUDON_LOG_STREAM", DEFAULT_LOG_STREAM)
        redis_cfg = redis_env()
        return cls(
            panel_url=(_env("DJANGO_API_URL", DEFAULT_ADMIN_PANEL_URL) or "").rstrip("/"),
            agent_key=agent_key,
            redis_host=redis_cfg["HOST"] or "localhost",
            redis_port=int(redis_cfg["PORT"] or 6379),
            redis_db=int(redis_cfg["DB"] or 0),
            redis_password=redis_cfg["PASSWORD"],
            stream=stream,
            dead_stream=f"{stream}:dead",
            consumer=_env("AGENT_CONSUMER_NAME", socket.gethostname()),
            block_ms=int(_env("AGENT_BLOCK_MS", "5000")),
            reclaim_seconds=int(_env("AGENT_RECLAIM_SECONDS", "60")),
            heartbeat_seconds=int(_env("AGENT_HEARTBEAT_SECONDS", "60")),
            http_timeout_seconds=float(_env("AGENT_HTTP_TIMEOUT_SECONDS", "30")),
        )


class PanelUnavailable(Exception):
    """The panel did not answer for the batch; try the same records again later."""

    def __init__(self, reason: str, retry_after: float | None = None):
        super().__init__(reason)
        self.retry_after = retry_after


class KeyRefused(Exception):
    """The panel does not accept this agent's key."""


@dataclass
class _Entry:
    entry_id: str
    record: dict[str, Any]
    size: int


class Agent:
    def __init__(self, cfg: AgentSettings, *, redis_client: redis.Redis | None = None,
                 http: httpx.Client | None = None) -> None:
        self.cfg = cfg
        self.redis = redis_client or redis.Redis(
            host=cfg.redis_host,
            port=cfg.redis_port,
            db=cfg.redis_db,
            password=cfg.redis_password,
            decode_responses=True,
            socket_timeout=max(cfg.block_ms / 1000 + 5, 10),
        )
        self.http = http or httpx.Client(timeout=cfg.http_timeout_seconds)
        self.version = _agent_version()
        self.last_shipped_at: str | None = None
        self.last_report_at = 0.0
        self.failures = 0
        self.stopping = False

    # -- the stream -----------------------------------------------------------

    def ensure_group(self) -> None:
        try:
            self.redis.xgroup_create(self.cfg.stream, CONSUMER_GROUP, id="0", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def _decode(self, entries) -> tuple[list[_Entry], list[tuple[str, str]]]:
        good: list[_Entry] = []
        bad: list[tuple[str, str]] = []
        for entry_id, fields in entries or ():
            if not fields:
                # Trimmed by MAXLEN while it was pending: nothing left to send.
                bad.append((entry_id, ""))
                continue
            raw = fields.get("record") or ""
            try:
                record = json.loads(raw)
                if not isinstance(record, dict):
                    raise ValueError("not an object")
            except ValueError:
                self.dead_letter(entry_id, raw, "Not a readable record.")
                bad.append((entry_id, raw))
                continue
            good.append(_Entry(entry_id, record, len(raw)))
        return good, bad

    def read(self) -> list[_Entry]:
        """Records to send: ones abandoned by a stopped consumer first, then new ones."""
        _, claimed, *_ = self.redis.xautoclaim(
            self.cfg.stream,
            CONSUMER_GROUP,
            self.cfg.consumer,
            min_idle_time=self.cfg.reclaim_seconds * 1000,
            start_id="0-0",
            count=MAX_BATCH_RECORDS,
        )
        entries, unreadable = self._decode(claimed)
        if not entries:
            response = self.redis.xreadgroup(
                CONSUMER_GROUP,
                self.cfg.consumer,
                {self.cfg.stream: ">"},
                count=MAX_BATCH_RECORDS,
                block=self.cfg.block_ms,
            )
            for _stream, stream_entries in response or ():
                more, more_unreadable = self._decode(stream_entries)
                entries.extend(more)
                unreadable.extend(more_unreadable)
        self.done([entry_id for entry_id, _ in unreadable])
        return entries

    def done(self, entry_ids: list[str]) -> None:
        if not entry_ids:
            return
        pipe = self.redis.pipeline(transaction=False)
        pipe.xack(self.cfg.stream, CONSUMER_GROUP, *entry_ids)
        pipe.xdel(self.cfg.stream, *entry_ids)
        pipe.execute()

    def dead_letter(self, entry_id: str, record: Any, reason: Any) -> None:
        self.redis.xadd(
            self.cfg.dead_stream,
            {
                "entry_id": entry_id,
                "record": record if isinstance(record, str) else json.dumps(record, default=str),
                "reason": reason if isinstance(reason, str) else json.dumps(reason, default=str),
                "at": _now_iso(),
            },
            maxlen=DEAD_STREAM_MAXLEN,
            approximate=True,
        )

    def report(self) -> dict[str, Any]:
        report: dict[str, Any] = {"version": self.version, "last_shipped_at": self.last_shipped_at}
        try:
            report["outbox_depth"] = self.redis.xlen(self.cfg.stream)
            report["dead_letter_depth"] = self.redis.xlen(self.cfg.dead_stream)
            pending = self.redis.xpending(self.cfg.stream, CONSUMER_GROUP)
            report["pending"] = pending.get("pending", 0)
            oldest = pending.get("min")
            if oldest:
                # A stream id starts with the millisecond it was added.
                added_ms = int(str(oldest).split("-", 1)[0])
                report["oldest_pending_seconds"] = max(0, int(time.time() - added_ms / 1000))
            else:
                report["oldest_pending_seconds"] = 0
        except redis.RedisError as exc:
            report["redis_error"] = str(exc)[:200]
        return report

    # -- the panel ------------------------------------------------------------

    def post(self, records: list[dict[str, Any]]) -> httpx.Response:
        try:
            return self.http.post(
                f"{self.cfg.panel_url}{AGENT_BATCH_PATH}",
                json={"records": records, "agent": self.report()},
                headers={AGENT_KEY_HEADER: self.cfg.agent_key},
            )
        except httpx.HTTPError as exc:
            raise PanelUnavailable(f"{type(exc).__name__}: {exc}") from exc

    def ship(self, entries: list[_Entry]) -> int:
        """Deliver entries, splitting where the panel finds them too large. Returns how many were stored."""
        if not entries:
            response = self.post([])
            self._check(response)
            self.last_report_at = time.monotonic()
            return 0
        response = self.post([entry.record for entry in entries])
        if response.status_code == 413:
            if len(entries) == 1:
                self.dead_letter(entries[0].entry_id, entries[0].record, "Too large for the panel to accept.")
                self.done([entries[0].entry_id])
                return 0
            middle = len(entries) // 2
            return self.ship(entries[:middle]) + self.ship(entries[middle:])
        self._check(response)
        body = response.json()
        for rejected in body.get("rejected") or ():
            index = rejected.get("index")
            if isinstance(index, int) and 0 <= index < len(entries):
                entry = entries[index]
                self.dead_letter(entry.entry_id, entry.record, rejected.get("errors"))
        self.done([entry.entry_id for entry in entries])
        self.last_shipped_at = _now_iso()
        self.last_report_at = time.monotonic()
        return int(body.get("accepted") or 0)

    @staticmethod
    def _check(response: httpx.Response) -> None:
        if response.status_code == 200:
            return
        if response.status_code in (401, 403):
            raise KeyRefused(f"The panel refused this agent's key ({response.status_code}).")
        retry_after = None
        if response.status_code == 429:
            try:
                retry_after = float(response.headers.get("Retry-After") or 0) or None
            except ValueError:
                retry_after = None
        raise PanelUnavailable(f"The panel answered {response.status_code}.", retry_after)

    def batches(self, entries: list[_Entry]) -> list[list[_Entry]]:
        out: list[list[_Entry]] = []
        current: list[_Entry] = []
        size = 0
        for entry in entries:
            if current and (len(current) >= MAX_BATCH_RECORDS or size + entry.size > MAX_BATCH_BYTES):
                out.append(current)
                current, size = [], 0
            current.append(entry)
            size += entry.size
        if current:
            out.append(current)
        return out

    # -- the loop -------------------------------------------------------------

    def step(self) -> None:
        """One pass: read what is waiting and send it, or report if it is time."""
        entries = self.read()
        if entries:
            for batch in self.batches(entries):
                self.ship(batch)
        elif time.monotonic() - self.last_report_at >= self.cfg.heartbeat_seconds:
            self.ship([])

    def wait_after_failure(self, exc: Exception) -> float:
        self.failures += 1
        if isinstance(exc, KeyRefused):
            return REFUSED_KEY_RETRY_SECONDS
        if isinstance(exc, PanelUnavailable) and exc.retry_after:
            return min(exc.retry_after, MAX_BACKOFF_SECONDS)
        ceiling = min(MAX_BACKOFF_SECONDS, 2 ** min(self.failures, 9))
        return random.uniform(ceiling / 2, ceiling)

    def run(self) -> None:
        logger.info("Agent %s shipping %s to %s as %s", self.version, self.cfg.stream,
                    self.cfg.panel_url, self.cfg.consumer)
        while not self.stopping:
            try:
                self.ensure_group()
                self.step()
                self.failures = 0
            except (PanelUnavailable, KeyRefused, redis.RedisError) as exc:
                delay = self.wait_after_failure(exc)
                logger.warning("%s Trying again in %.0f s.", exc, delay)
                self._sleep(delay)

    def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self.stopping and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))

    def stop(self, *_args) -> None:
        self.stopping = True


def main() -> None:
    logging.basicConfig(level=_env("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    agent = Agent(AgentSettings.from_env())
    signal.signal(signal.SIGTERM, agent.stop)
    signal.signal(signal.SIGINT, agent.stop)
    agent.run()
