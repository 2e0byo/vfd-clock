import secrets
import time

import network
import uasyncio as asyncio

import ntptime


def instrument(f, name=""):
    start = time.ticks_ms()
    resp = f()
    end = time.ticks_ms()
    print(f"{name} Took: {time.ticks_diff(end, start)} ms")
    return resp


def settime():
    try:
        ntptime.settime()
        print("Set time.")
    except Exception:
        pass


ap_if = network.WLAN(network.AP_IF)
ap_if.active(False)
sta_if = network.WLAN(network.STA_IF)
if not sta_if.isconnected():
    print("connecting to network...")
    sta_if.active(True)
    sta_if.connect(secrets.wifi_SSID, secrets.wifi_PSK)
    while not sta_if.isconnected():
        pass
print("network config:", sta_if.ifconfig())
settime()

import hal


def transform(old: int):
    new = 0
    mapping = (6, 5, 4, 3, 2, 1, 0, 7)
    for i in range(8):
        if old & 1:
            new |= 1 << mapping[i]
        old >>= 1
    return new


class Clock:
    def epochtime(self) -> float:
        """
        Return time since the epoch.

        This method should be used as a single source of truth for all time data.
        """
        return time.time()

    def time(self) -> tuple:
        return time.gmtime(self.epochtime())


class DSTClock(Clock):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.offset_mins = 0

    def gmtime(self) -> tuple:
        return super().time()

    def time(self) -> tuple:
        return time.localtime(self.epochtime() + self.offset_mins * 60)


class Response:
    def __init__(self, data: bytes):
        self.content = data

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")

    @property
    def json(self) -> dict:
        import ujson

        return ujson.loads(self.content)


async def get(url: str) -> Response:
    """Very basic async http/1.1 get"""
    import usocket
    import ussl

    sock = usocket.socket()
    try:
        proto, _, host, path = url.split("/", 3)
    except ValueError:
        proto, _, host = url.split("/", 3)
        path = ""
    assert proto == "https:", "Bad protocol: {}".format(proto)
    try:
        serv = usocket.getaddrinfo(host, 443)[0][-1]
        sock.connect(serv)
        sock = ussl.wrap_socket(sock)
        sreader = asyncio.StreamReader(sock)
        swriter = asyncio.StreamWriter(sock, {})
        swriter.write(b"GET /%s HTTP/1.0\r\n" % path)
        swriter.write(b"Host: %s\r\n" % host)
        await swriter.drain()
        swriter.write(b"Connection: close\r\n\r\n")
        await swriter.drain()

        _, status, reason = (await sreader.readline()).split(None, 2)
        if status != b"200":
            raise Exception(
                "Failed to fetch {}/{}:{} : {}".format(
                    host, path, "443", reason.decode("utf-8")
                )
            )
        # skip over headers
        while True:
            l = await sreader.readline()
            if not l.strip():
                break
        data = await sreader.read(-1)
    except OSError as e:
        raise Exception("Failed to connect: {}".format(e))
    finally:
        sock.close()
    return Response(data)


class AutoDSTClock(DSTClock):
    async def set(self):
        resp = await get("https://ifconfig.me")
        ip = resp.text
        resp = await get(f"https://www.timeapi.io/api/Time/current/ip?ipAddress={ip}")
        data = resp.json
        utc = self.epochtime()
        utc_timetuple = self.gmtime()
        local = time.mktime(
            (
                data["year"],
                data["month"],
                data["day"],
                data["hour"],
                data["minute"],
                data["seconds"],
                utc_timetuple[6],
                utc_timetuple[7],
            )
        )
        # granularity for timezones is 15 mins.
        self.offset_mins = round((local - utc) / 900) * 15
        print(f"Updated time offset to {self.offset_mins}")


class MatrixDisplay:
    PULSE_DURATION_MS = 3
    _SEGMENTS = bytearray(
        b"\x3F\x06\x5B\x4F\x66\x6D\x7D\x07\x7F\x6F\x77\x7C\x39\x5E\x79\x71\x3D"
        b"\x76\x06\x1E\x76\x38\x55\x54\x3F\x73\x67\x50\x6D\x78\x3E\x1C\x2A\x76"
        b"\x6E\x5B\x00\x40\x63"
    )

    def __init__(self, D: tuple, digits: tuple, inverted: "set | None" = None):
        for p in D:
            p(0)
        for p in digits:
            p(0)
        self.no_digits = len(digits)
        self._digit_pins = digits
        self._D_pins = D
        self.pulse_duration = self.PULSE_DURATION_MS
        self.inverted_digits = inverted if inverted else set()
        self.leds = [False for _ in range(self.no_digits)]

    async def _write_digit(self, val: int, digit: int):
        val = transform(val)
        if self.leds[digit]:
            val |= 1 << 7
        if digit in self.inverted_digits:
            val = 0xFF ^ val
        assert digit < self.no_digits
        for i in range(8):
            x = val & 1
            self._D_pins[i](x)
            val >>= 1  # or invert direction
        self._digit_pins[digit](1)
        await asyncio.sleep_ms(self.pulse_duration)
        self._digit_pins[digit](0)

    # originally from tm1637.py
    def _encode_char(self, char: str) -> int:
        """Convert a character 0-9, a-z, space, dash or star to a segment."""
        o = ord(char)
        if o == 32:
            return self._SEGMENTS[36]  # space
        elif o == 42:
            return self._SEGMENTS[38]  # star/degrees
        elif o == 45:
            return self._SEGMENTS[37]  # dash
        elif o >= 65 and o <= 90:
            return self._SEGMENTS[o - 55]  # uppercase A-Z
        elif o >= 97 and o <= 122:
            return self._SEGMENTS[o - 87]  # lowercase a-z
        elif o >= 48 and o <= 57:
            return self._SEGMENTS[o - 48]  # 0-9
        raise ValueError("Character out of range: {:d} '{:s}'".format(o, chr(o)))

    async def write(self, msg: str):
        if msg[2] == ":":
            self.leds[0] = True
            msg = "{}{}".format(msg[:2], msg[3:])
        else:
            self.leds[0] = False

        msg = "{:>6}".format(msg)
        # digits backwards...
        for i, c in enumerate(msg):
            await self._write_digit(self._encode_char(c), i)


display = MatrixDisplay(hal.D, hal.digits, {0, 1, 2, 3})
clock = AutoDSTClock()


async def sync():
    while True:
        # TODO make this async too
        try:
            instrument(settime, "settime")
            await clock.set()
        except Exception as e:
            print("Failed sync:", e)
        await asyncio.sleep(600)


async def tick():
    while True:
        start = time.ticks_ms()
        await display.write("{:02}:{:02}{:02}".format(*clock.time()[3:6]))
        end = time.ticks_ms()
        await asyncio.sleep_ms(1000 - time.ticks_diff(end, start))


async def main():
    asyncio.create_task(sync())
    await tick()


asyncio.run(main())
