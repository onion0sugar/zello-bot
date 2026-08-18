"""Testy klienta Zello z zamockowanym WebSocketem (tekst, głos, reconnect, timeout)."""

import asyncio
import json
import struct
import unittest

from zello import Zello, ZelloError, ZelloSendError

CHANNEL = "Magazyn"


class FakeWebSocket:
    """Podróbka połączenia websockets: scenariusz odpowiedzi + zapis wysłanych ramek.

    Połączenie pozostaje "otwarte" (__anext__ blokuje), gdy scenariusz jest
    pusty — tak jak prawdziwy WebSocket. Zamknięcie następuje przez close().
    """

    def __init__(self, script=None, on_send=None):
        self.script = list(script or [])
        self.sent: list = []
        self.closed = False
        self._closed_event = asyncio.Event()
        self._on_send = on_send or (lambda data: None)

    async def send(self, data):
        self.sent.append(data)
        await self._on_send(data)

    async def close(self):
        self.closed = True
        self._closed_event.set()

    def __aiter__(self):
        return self

    async def __anext__(self):
        while not self.script and not self._closed_event.is_set():
            await asyncio.sleep(0.005)
        if self.script:
            return self.script.pop(0)
        raise StopAsyncIteration


CHANNEL_ONLINE = {
    "command": "on_channel_status",
    "channel": CHANNEL,
    "status": "online",
    "texting_supported": True,
}


def responsive_ws(send_success=True) -> FakeWebSocket:
    """Serwer podróbka: odpowiada na logon, tekst, start/stop_stream."""
    ws = FakeWebSocket(script=[json.dumps(CHANNEL_ONLINE)])

    async def on_send(data):
        if isinstance(data, bytes):
            return
        payload = json.loads(data)
        cmd = payload.get("command")
        if cmd == "logon":
            ws.script.append(json.dumps({"seq": payload["seq"], "success": True}))
        elif cmd == "send_text_message":
            ws.script.append(
                json.dumps(
                    {
                        "seq": payload["seq"],
                        "success": send_success,
                        "error": None if send_success else "not authorized",
                    }
                )
            )
        elif cmd == "start_stream":
            ws.script.append(json.dumps({"seq": payload["seq"], "success": True, "stream_id": 42}))
        elif cmd == "stop_stream":
            ws.script.append(json.dumps({"seq": payload["seq"], "success": True}))

    ws._on_send = on_send
    return ws


def make_zello(ws, sleeps=None, **kwargs) -> Zello:
    sleeps = [] if sleeps is None else sleeps

    async def factory(url, **kw):
        return ws

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        await asyncio.sleep(0)

    return Zello(
        "testnet", "sql_bot", "pw", CHANNEL,
        ws_connect=factory, sleep=fake_sleep, **kwargs,
    )


class ZelloTests(unittest.IsolatedAsyncioTestCase):
    async def test_connect_logon_and_channel_ready(self):
        ws = responsive_ws()
        z = make_zello(ws)
        await z.start()
        await z.wait_ready()
        logon = json.loads(ws.sent[0])
        assert logon["command"] == "logon"
        assert logon["channels"] == [CHANNEL]
        assert logon["seq"] == 1
        await z.close()

    async def test_send_text_message_waits_for_success(self):
        ws = responsive_ws()
        z = make_zello(ws)
        await z.start()
        await z.wait_ready()
        await z.send_text_message(CHANNEL, "Test wiadomości")
        payload = json.loads(ws.sent[1])  # po logonie
        assert payload["command"] == "send_text_message"
        assert payload["channel"] == CHANNEL
        assert payload["text"] == "Test wiadomości"
        assert payload["seq"] == 2
        await z.close()

    async def test_send_text_message_rejected_raises(self):
        ws = responsive_ws(send_success=False)
        z = make_zello(ws)
        await z.start()
        await z.wait_ready()
        with self.assertRaises(ZelloSendError):
            await z.send_text_message(CHANNEL, "X")
        await z.close()

    async def test_send_text_timeout_raises(self):
        ws = FakeWebSocket(script=[json.dumps(CHANNEL_ONLINE)])

        async def on_send(data):
            if isinstance(data, bytes):
                return
            payload = json.loads(data)
            if payload["command"] == "logon":
                ws.script.append(json.dumps({"seq": payload["seq"], "success": True}))
            # send_text_message celowo bez odpowiedzi

        ws._on_send = on_send
        z = make_zello(ws, response_timeout=0.05)
        await z.start()
        await z.wait_ready()
        with self.assertRaises(ZelloError):
            await z.send_text_message(CHANNEL, "X")
        await z.close()

    async def test_send_voice_streams_binary_frames_and_stops(self):
        ws = responsive_ws()
        sleeps = []
        z = make_zello(ws, sleeps=sleeps)
        await z.start()
        await z.wait_ready()
        packets = [b"\x11\x22", b"\x33\x44\x55"]
        await z.send_voice(CHANNEL, packets, "gD4BFA==")

        start = json.loads(ws.sent[1])
        assert start["command"] == "start_stream"
        assert start["codec"] == "opus"
        assert start["codec_header"] == "gD4BFA=="
        assert start["packet_duration"] == 20

        binary = [m for m in ws.sent if isinstance(m, bytes)]
        assert binary == [
            struct.pack("!BII", 0x01, 42, 0) + b"\x11\x22",
            struct.pack("!BII", 0x01, 42, 0) + b"\x33\x44\x55",
        ]
        stop = json.loads(ws.sent[-1])
        assert stop["command"] == "stop_stream"
        assert stop["stream_id"] == 42
        # tempo: sleep 0.02 s między ramkami
        assert [s for s in sleeps if s == 0.02] == [0.02, 0.02]
        await z.close()

    async def test_send_without_connection_raises_fast(self):
        z = make_zello(responsive_ws())
        with self.assertRaises(ZelloError):
            await z.send_text_message(CHANNEL, "X")  # bez start() → bez 30 s czekania

    async def test_reconnect_after_disconnect_with_constant_delay(self):
        sleeps = []
        calls = []

        class DropAfterLogon(FakeWebSocket):
            """Logon działa, potem serwer zrywa połączenie (script się kończy)."""

            async def __anext__(self):
                if self.script:
                    return self.script.pop(0)
                raise ConnectionError("server closed connection")

        def make_drop_ws() -> DropAfterLogon:
            # Celowo BEZ CHANNEL_ONLINE: kanał nie zgłosi "online" na pierwszym
            # połączeniu, więc wait_ready zwróci dopiero po re-loginie.
            ws = DropAfterLogon(script=[])

            async def on_send(data):
                if isinstance(data, bytes):
                    return
                payload = json.loads(data)
                if payload["command"] == "logon":
                    ws.script.append(json.dumps({"seq": payload["seq"], "success": True}))

            ws._on_send = on_send
            return ws

        async def factory(url, **kw):
            if not calls:
                calls.append("first")
                return make_drop_ws()
            calls.append("second")
            return responsive_ws()

        async def fake_sleep(seconds):
            sleeps.append(seconds)
            await asyncio.sleep(0)

        z = Zello("testnet", "sql_bot", "pw", CHANNEL, ws_connect=factory, sleep=fake_sleep)
        await z.start()
        await z.wait_ready()  # wymaga udanego re-loginu na drugim połączeniu
        assert len(calls) == 2    # połączenie wznowione
        assert sleeps == [5]      # stały backoff 5 s (bez exponential)
        await z.close()

    async def test_wait_ready_timeout_when_channel_offline(self):
        ws = FakeWebSocket(script=[])

        async def on_send(data):
            if isinstance(data, bytes):
                return
            payload = json.loads(data)
            if payload["command"] == "logon":
                ws.script.append(json.dumps({"seq": payload["seq"], "success": True}))

        ws._on_send = on_send
        z = make_zello(ws, channel_wait_timeout=0.05)
        await z.start()
        with self.assertRaises(ZelloError):
            await z.wait_ready()
        await z.close()

    # -- Zello Friends & Family --------------------------------------------------

    async def test_ff_uses_ff_url_and_auth_token_in_logon(self):
        urls = []
        ws = responsive_ws()

        async def factory(url, **kw):
            urls.append(url)
            return ws

        async def fake_sleep(seconds):
            await asyncio.sleep(0)

        z = Zello(
            "", "sql_bot", "pw", CHANNEL, auth_token="dev-token",
            ws_connect=factory, sleep=fake_sleep,
        )
        await z.start()
        await z.wait_ready()
        logon = json.loads(ws.sent[0])
        assert urls == ["wss://zello.io/ws"]       # endpoint F&F
        assert logon["auth_token"] == "dev-token"  # JWT z developers.zello.com
        assert "refresh_token" not in logon        # pierwszy logon — sam auth_token
        await z.close()

    async def test_work_uses_network_url_without_auth_token(self):
        urls = []
        ws = responsive_ws()

        async def factory(url, **kw):
            urls.append(url)
            return ws

        async def fake_sleep(seconds):
            await asyncio.sleep(0)

        z = Zello("testnet", "sql_bot", "pw", CHANNEL, ws_connect=factory, sleep=fake_sleep)
        await z.start()
        await z.wait_ready()
        logon = json.loads(ws.sent[0])
        assert urls == ["wss://zellowork.io/ws/testnet"]  # endpoint Work
        assert "auth_token" not in logon
        await z.close()

    async def test_ff_uses_refresh_token_on_reconnect(self):
        second_logons = []
        calls = []

        class DropAfterLogon(FakeWebSocket):
            async def __anext__(self):
                if self.script:
                    return self.script.pop(0)
                raise ConnectionError("server closed connection")

        def make_first() -> DropAfterLogon:
            # logon zwraca refresh_token, potem serwer zrywa połączenie
            ws = DropAfterLogon(script=[])

            async def on_send(data):
                if isinstance(data, bytes):
                    return
                payload = json.loads(data)
                if payload["command"] == "logon":
                    ws.script.append(
                        json.dumps(
                            {"seq": payload["seq"], "success": True, "refresh_token": "rt-123"}
                        )
                    )

            ws._on_send = on_send
            return ws

        def make_second() -> FakeWebSocket:
            ws = FakeWebSocket(script=[json.dumps(CHANNEL_ONLINE)])

            async def on_send(data):
                if isinstance(data, bytes):
                    return
                payload = json.loads(data)
                if payload["command"] == "logon":
                    second_logons.append(payload)
                    ws.script.append(json.dumps({"seq": payload["seq"], "success": True}))

            ws._on_send = on_send
            return ws

        async def factory(url, **kw):
            if not calls:
                calls.append("first")
                return make_first()
            calls.append("second")
            return make_second()

        async def fake_sleep(seconds):
            await asyncio.sleep(0)

        z = Zello(
            "", "sql_bot", "pw", CHANNEL, auth_token="dev-token",
            ws_connect=factory, sleep=fake_sleep,
        )
        await z.start()
        await z.wait_ready()
        assert len(calls) == 2                     # połączenie wznowione
        assert second_logons[0]["refresh_token"] == "rt-123"  # reconnect używa refresh_token
        assert "auth_token" not in second_logons[0]
        await z.close()

    async def test_no_network_no_token_raises(self):
        with self.assertRaises(ValueError):
            Zello("", "sql_bot", "pw", CHANNEL)


if __name__ == "__main__":
    unittest.main()
