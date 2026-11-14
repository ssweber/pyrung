import asyncio
from pyclickplc.client import ClickClient
async def main():
    async with ClickClient("192.168.1.238", 502) as c:
        for n in range(1, 11):
            b = 501 + 10 * n
            v = await c.ds.read(b, b + 8)
            print(n, list(v.values()) if hasattr(v, "values") else v)
        for base in (1951, 1961, 1971, 1931):
            vals = []
            for i in range(0, 7):
                vals.append(await c.c[base + i])
            print("C", base, vals)
asyncio.run(main())
