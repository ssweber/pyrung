import asyncio, time
from pyclickplc.client import ClickClient
async def main():
    async with ClickClient("192.168.1.238", 502) as c:
        t0=time.perf_counter()
        for _ in range(40):
            td = await c.td.read(305, 307)
            cc = await c.c.read(1831, 1832)
            print(f"{time.perf_counter()-t0:6.3f}", td['TD305'], td['TD306'], td['TD307'], int(cc['C1831']), int(cc['C1832']))
        print(await c.sd.read(10, 12))
asyncio.run(main())
