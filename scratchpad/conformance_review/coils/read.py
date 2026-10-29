import asyncio
from pyclickplc.client import ClickClient
async def main():
    async with ClickClient("192.168.1.238", 502) as c:
        print("hdr", await c.ds[1501])
        ds = await c.ds.read(1511, 1580)
        print(ds)
        print(await c.c.read(1501, 1560))
        print(await c.sc[11])
asyncio.run(main())
