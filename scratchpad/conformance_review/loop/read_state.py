"""READ-ONLY snapshot of the loop group's registers on the CLICK."""
import asyncio
from pyclickplc.client import ClickClient

NAMES = ["Cmd", "Scratch", "R1", "R2", "R3", "R4", "Fired", "Err", "Ready"]


async def main():
    async with ClickClient("192.168.1.238", 502) as c:
        print("DS901 hash", await c.ds[901])
        vals = {}
        for lo, hi in ((911, 1000), (1001, 1050)):
            r = await c.ds.read(lo, hi)
            vals.update(dict(r))
        keys = list(vals)[:3]
        print("key sample", keys)
        for slot in range(1, 15):
            base = 901 + 10 * slot
            row = []
            for i, n in enumerate(NAMES):
                a = base + i
                v = vals.get(f"DS{a}", vals.get(a, "?"))
                row.append(f"{n}={v}")
            print(slot, " ".join(row))
        for a in (1701, 1711, 1721, 1731):
            print(f"C{a}", await c.c[a])
        print("T201/202", await c.t.read(201, 202), "TD", await c.td.read(201, 202))
        print("CT201", await c.ct[201], "CTD201", await c.ctd[201])


asyncio.run(main())
