"""READ-ONLY register dump for the numeric/math review. Never writes."""
import asyncio
from pyclickplc.client import ClickClient

async def main():
    async with ClickClient("192.168.1.238", 502) as c:
        print("DS1", await c.ds[1])
        print("numeric hdr DS1701", await c.ds[1701])
        for n in range(1, 16):
            base = 1701 + 10 * n
            print(f"numeric slot {n:2d} DS{base}..", await c.ds.read(base, base + 8))
        print("DD101..104", await c.dd.read(101, 104))
        print("DD111..112", await c.dd.read(111, 112))
        print("DD121..122", await c.dd.read(121, 122))
        print("DD131..132", await c.dd.read(131, 132))
        print("DF101..102", await c.df.read(101, 102))
        print("DF111..113", await c.df.read(111, 113))
        print("DF121..123", await c.df.read(121, 123))
        print("DF131..137", await c.df.read(131, 137))
        print("math hdr DS301", await c.ds[301])
        for n in range(1, 8):
            base = 301 + 10 * n
            print(f"math slot {n} DS{base}..", await c.ds.read(base, base + 8))
        print("DS4401..4434", await c.ds.read(4401, 4434))
        print("DF1..2", await c.df.read(1, 2))
        print("DD1..3", await c.dd.read(1, 3))
        print("SC", {n: await c.sc[n] for n in (40, 43, 44, 46, 50, 11)})

asyncio.run(main())
