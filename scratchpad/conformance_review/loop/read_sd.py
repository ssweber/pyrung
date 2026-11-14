"""READ-ONLY: scan times and numeric int_calc_wraps slot."""
import asyncio
from pyclickplc.client import ClickClient

async def main():
    async with ClickClient("192.168.1.238", 502) as c:
        print("SD9..SD12", await c.sd.read(9, 12))
        print("numeric slot1 DS1711..1719", await c.ds.read(1711, 1719))
        print("F3 slot DS931..939", await c.ds.read(931, 939))
        print("F14 slot DS1041..1049", await c.ds.read(1041, 1049))

asyncio.run(main())
