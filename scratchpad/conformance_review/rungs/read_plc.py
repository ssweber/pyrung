"""READ-ONLY dump of the rungs group registers and C bits on the CLICK."""
import asyncio
from pyclickplc.client import ClickClient

async def main():
    async with ClickClient("192.168.1.238", 502) as c:
        ds = await c.ds.read(101, 200)
        vals = dict(ds) if hasattr(ds, "items") else ds
        print("DS101", vals.get("DS101") if isinstance(vals, dict) else ds)
        for base in range(111, 200, 10):
            row = [vals.get(f"DS{base+i}") for i in range(9)]
            print(f"DS{base}..: Cmd,Scr,R1..R4,Fired,Err,Ready = {row}")
        cs = await c.c.read(1601, 1660)
        cv = dict(cs) if hasattr(cs, "items") else cs
        print({k: v for k, v in cv.items() if v})
        print("SC11", await c.sc[11])

asyncio.run(main())
