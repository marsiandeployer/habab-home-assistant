"""Lifecycle wrapper for the canonical outbound WebSocket tunnel agent."""

import asyncio
from collections.abc import Awaitable, Callable

import aiohttp


class TunnelClient:
    """Own a credential-free relay session and one cancellable agent task."""

    def __init__(self, relay: str, home: str, token: str,
                 run_agent: Callable[..., Awaitable[None]]):
        self.relay = relay
        self.home = home
        self.token = token
        self._run_agent = run_agent
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task | None = None

    @property
    def status(self) -> str:
        return "enabled" if self._task and not self._task.done() else "stopped"

    async def async_start(self) -> None:
        if self._task and not self._task.done():
            return
        self._session = aiohttp.ClientSession(
            cookie_jar=aiohttp.DummyCookieJar(),
            trust_env=False,
            timeout=aiohttp.ClientTimeout(total=None),
        )
        self._task = asyncio.create_task(
            self._run_agent(self._session, self.relay, self.home, self.token),
            name=f"habab_product_tunnel_{self.home}",
        )

    async def async_stop(self) -> None:
        task, self._task = self._task, None
        session, self._session = self._session, None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if session and not session.closed:
            await session.close()
