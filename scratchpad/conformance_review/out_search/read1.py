import asyncio
from pyclickplc.client import ClickClient
async def main():
    async with ClickClient("192.168.1.238", 502) as c:
        print("out slots DS701..770", await c.ds.read(701, 770))
        print("TD301..312", await c.td.read(301, 312))
        print("T301..312", await c.t.read(301, 312))
        print("C1801..1860", {k:v for k,v in (await c.c.read(1801, 1860)).items() if v})
        print("search slot10 DS2601..2609", await c.ds.read(2601, 2609))
        print("DS4383..4394", await c.ds.read(4383, 4394))
        print("C851..877", await c.c.read(851, 877))
asyncio.run(main())
