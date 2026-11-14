import asyncio
from pyclickplc.client import ClickClient
async def main():
    async with ClickClient("192.168.1.238", 502) as c:
        seen = {}
        for _ in range(100):
            t = await c.t.read(301, 312)
            for k, v in t.items():
                seen.setdefault(k, set()).add(int(v))
        print(seen)
asyncio.run(main())
