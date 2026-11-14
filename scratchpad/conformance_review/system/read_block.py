import asyncio
from pyclickplc.client import ClickClient
async def main():
    async with ClickClient("192.168.1.238", 502) as c:
        v = await c.ds.read(2701, 2800)
        vals = list(v.values()) if hasattr(v, "values") else list(v)
        print(type(v))
        for i in range(0, 90, 10):
            print(2701 + i, vals[i:i+10])
        print("SC3", await c.sc[3], "SD9", await c.sd[9])
        print("SC40/43/44", await c.sc[40], await c.sc[43], await c.sc[44])
        print("DD901..", await c.dd.read(901, 905))
        print("DD921..", await c.dd.read(921, 922))
asyncio.run(main())
