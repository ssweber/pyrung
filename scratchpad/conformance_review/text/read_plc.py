"""READ-ONLY dump of the text group's memory on the CLICK."""

import asyncio

from pyclickplc.client import ClickClient


async def main():
    async with ClickClient("192.168.1.238", 502) as c:
        ds = await c.ds.read(1931, 2040)
        print("slots", ds)
        print("DS3901..3925", await c.ds.read(3901, 3925))
        print("DD301..303", await c.dd.read(301, 303))
        print("DH301..304", await c.dh.read(301, 304))
        try:
            t = await c.txt.read(1, 200)
            print("TXT", t)
        except Exception as e:  # noqa: BLE001
            print("TXT read failed", e)
        print("SC43", await c.sc[43])


asyncio.run(main())
