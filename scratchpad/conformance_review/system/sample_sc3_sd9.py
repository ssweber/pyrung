"""Read-only: sample SD9, SC3, SD9; keep samples where SD9 did not move across the SC3 read."""
import asyncio
from collections import Counter
from pyclickplc.client import ClickClient

async def main():
    tally = Counter()
    async with ClickClient("192.168.1.238", 502) as c:
        for _ in range(200):
            a = await c.sd[9]
            b = await c.sc[3]
            d = await c.sd[9]
            if a == d:
                tally[(a % 2, bool(b))] += 1
            else:
                tally["moved"] += 1
    print(tally)
asyncio.run(main())
