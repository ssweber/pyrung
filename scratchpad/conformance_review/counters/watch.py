import asyncio, time
from pyclickplc.client import ClickClient
async def main():
    async with ClickClient("192.168.1.238", 502) as c:
        for _ in range(5):
            ds = list((await c.ds.read(1311, 1319)).values())
            print(time.strftime("%H:%M:%S"), ds, await c.ctd[1], await c.ctd[2], await c.sd[9] if hasattr(c,'sd') else None)
            await asyncio.sleep(1)
        print("DS1", await c.ds[1])
asyncio.run(main())
