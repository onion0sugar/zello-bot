"""Minimalny klient Zello Channel API (WebSocket) — tekst + głos.

Obsługuje dwa tryby:
* Zello Work .............. wss://zellowork.io/ws/{network}  (login+hasło),
* Zello Friends & Family .. wss://zello.io/ws                (auth_token JWT).

Protokół: https://github.com/zelloptt/zello-channel-api/blob/main/API.md

Zasady:
* połączenie trzymane otwarte (jedno, nie per wiadomość),
* po zerwaniu: 5 s przerwy → połącz ponownie → zaloguj ponownie → kontynuuj,
* odpowiedzi dopasowywane po ``seq``; ``websocket.send()`` samo w sobie NIE jest
  potwierdzeniem — czekamy na ``{"seq": N, "success": true}``,
* Ping/Pong obsługiwane przez bibliotekę websockets (ping_interval=20 s < 30 s,
  po czym Zello zrywa połączenie),
* głos: start_stream → ramki Opus jako binarne ramki WebSocket → stop_stream,
* F&F: przy reconnect używamy refresh_token z poprzedniego logonu; gdy przestanie
  działać — wracamy do auth_token z konfiguracji.

``ws_connect`` i ``sleep`` są wstrzykiwalne (maki w testach).
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct

try:
    import websockets
except ImportError:
    websockets = None  # type: ignore[assignment]

logger = logging.getLogger("zello")

RECONNECT_DELAY_SECONDS = 5
RESPONSE_TIMEOUT_SECONDS = 15
CHANNEL_WAIT_TIMEOUT_SECONDS = 30
VOICE_FRAME_MS = 20


class ZelloError(Exception):
    """Błąd klienta Zello."""


class ZelloSendError(ZelloError):
    """Serwer odrzucił wysyłkę."""


class Zello:
    def __init__(
        self,
        network: str,
        username: str,
        password: str,
        channel: str,
        auth_token: str = "",
        ws_connect=None,
        sleep=None,
        response_timeout: float = RESPONSE_TIMEOUT_SECONDS,
        channel_wait_timeout: float = CHANNEL_WAIT_TIMEOUT_SECONDS,
    ):
        # auth_token (F&F) ma pierwszeństwo; bez obu — błąd konfiguracji.
        if auth_token:
            self._url = "wss://zello.io/ws"
        elif network:
            self._url = f"wss://zellowork.io/ws/{network}"
        else:
            raise ValueError(
                "podaj network (Zello Work) lub auth_token (Zello Friends & Family)"
            )
        self._username = username
        self._password = password
        self._channel = channel
        self._auth_token = auth_token
        self._refresh_token: str | None = None
        self._ws_connect = ws_connect or (websockets.connect if websockets else None)
        self._sleep = sleep or asyncio.sleep
        self._response_timeout = response_timeout
        self._channel_wait_timeout = channel_wait_timeout
        self._ws = None
        self._seq = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._ready = asyncio.Event()
        self._stop = False
        self._task: asyncio.Task | None = None

    # -- cykl życia -----------------------------------------------------------

    async def start(self) -> None:
        if self._ws_connect is None:
            raise ZelloError("biblioteka websockets nie jest zainstalowana")
        self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        self._stop = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._ws = None

    async def _run(self) -> None:
        while not self._stop:
            try:
                ws = await self._ws_connect(
                    self._url, ping_interval=20, ping_timeout=20, open_timeout=30
                )
                self._ws = ws
                reader = asyncio.create_task(self._read(ws))
                try:
                    await self._logon()
                    logger.info("Connected to Zello")
                    await reader  # do momentu zerwania połączenia
                except asyncio.CancelledError:
                    reader.cancel()
                    raise
                except Exception:
                    reader.cancel()
                    raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Zello disconnected: %s", exc)
            finally:
                self._ws = None
                self._ready.clear()
                self._fail_pending()
            if self._stop:
                break
            logger.info("Reconnecting to Zello in %d seconds", RECONNECT_DELAY_SECONDS)
            await self._sleep(RECONNECT_DELAY_SECONDS)

    async def _read(self, ws) -> None:
        try:
            async for raw in ws:
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                self._dispatch(message)
        except asyncio.CancelledError:
            raise
        finally:
            try:
                await ws.close()
            except Exception:
                pass

    def _dispatch(self, message: dict) -> None:
        if message.get("command") == "on_channel_status":
            self._on_channel_status(message)
            return
        seq = message.get("seq")
        if seq is not None:
            future = self._pending.get(int(seq))
            if future is not None and not future.done():
                future.set_result(message)

    def _on_channel_status(self, message: dict) -> None:
        if message.get("channel") != self._channel:
            return
        if message.get("status") == "online" and message.get("texting_supported"):
            if not self._ready.is_set():
                logger.info("Channel %s online", self._channel)
            self._ready.set()
        else:
            self._ready.clear()

    # -- logowanie -------------------------------------------------------------

    async def _logon(self) -> None:
        payload = {
            "command": "logon",
            "seq": self._next_seq(),
            "username": self._username,
            "password": self._password,
            "channels": [self._channel],
        }
        # F&F: po pierwszym logonie używamy refresh_token (szybszy reconnect);
        # auth_token tylko dopóki nie mamy świeżego refresh_token.
        if self._refresh_token:
            payload["refresh_token"] = self._refresh_token
        elif self._auth_token:
            payload["auth_token"] = self._auth_token
        seq, future = await self._send(payload)
        response = await self._await_response(seq, future)
        if not response.get("success"):
            if "refresh_token" in payload:
                self._refresh_token = None  # wygasł → następnym razem auth_token
            raise ZelloError(f"logon rejected: {response.get('error', 'unknown error')}")
        if response.get("refresh_token"):
            self._refresh_token = response["refresh_token"]

    # -- wysyłka ---------------------------------------------------------------

    async def wait_ready(self) -> None:
        """Czeka aż kanał zgłosi online + texting_supported."""
        try:
            await asyncio.wait_for(self._ready.wait(), self._channel_wait_timeout)
        except asyncio.TimeoutError as exc:
            raise ZelloError("channel not online (or texting not supported)") from exc

    async def send_text_message(self, channel: str, text: str) -> None:
        if self._ws is None:
            raise ZelloError("not connected")
        await self.wait_ready()
        seq, future = await self._send(
            {
                "command": "send_text_message",
                "seq": self._next_seq(),
                "channel": channel,
                "text": text,
            }
        )
        response = await self._await_response(seq, future)
        if not response.get("success"):
            raise ZelloSendError(str(response.get("error", "unknown error")))

    async def send_voice(self, channel: str, packets: list[bytes], codec_header: str) -> None:
        if self._ws is None:
            raise ZelloError("not connected")
        await self.wait_ready()
        seq, future = await self._send(
            {
                "command": "start_stream",
                "seq": self._next_seq(),
                "channel": channel,
                "type": "audio",
                "codec": "opus",
                "codec_header": codec_header,
                "packet_duration": VOICE_FRAME_MS,
            }
        )
        response = await self._await_response(seq, future)
        if not response.get("success"):
            raise ZelloSendError(f"start_stream: {response.get('error', 'unknown error')}")
        stream_id = int(response["stream_id"])
        try:
            for packet in packets:
                # binarna ramka audio: {0x01, stream_id, packet_id=0} + Opus
                await self._ws.send(struct.pack("!BII", 0x01, stream_id, 0) + packet)
                await self._sleep(VOICE_FRAME_MS / 1000)  # tempo w czasie rzeczywistym
        finally:
            seq, future = await self._send(
                {
                    "command": "stop_stream",
                    "seq": self._next_seq(),
                    "channel": channel,
                    "stream_id": stream_id,
                }
            )
            try:
                response = await self._await_response(seq, future)
                if not response.get("success"):
                    logger.warning("stop_stream: %s", response.get("error"))
            except ZelloError:
                logger.warning("no stop_stream acknowledgement")

    # -- wewnętrzne -------------------------------------------------------------

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def _send(self, payload: dict) -> tuple[int, asyncio.Future]:
        seq = int(payload["seq"])
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[seq] = future
        if self._ws is None:
            self._pending.pop(seq, None)
            raise ZelloError("not connected")
        try:
            await self._ws.send(json.dumps(payload, ensure_ascii=False))
        except Exception as exc:
            self._pending.pop(seq, None)
            raise ZelloError(f"send failed: {exc}") from exc
        return seq, future

    async def _await_response(self, seq: int, future: asyncio.Future) -> dict:
        try:
            return await asyncio.wait_for(future, self._response_timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(seq, None)
            raise ZelloSendError("no response within timeout") from exc
        except ConnectionError as exc:
            raise ZelloError("connection lost") from exc

    def _fail_pending(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("Zello connection lost"))
        self._pending.clear()
