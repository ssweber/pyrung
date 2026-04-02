"""Test: Direct ClickClient polling (bypass ModbusService)."""

import asyncio
from pyclickplc import ClickClient

P1AM_IP = "192.168.1.221"


async def main():
    async with ClickClient(P1AM_IP, 502, timeout=3) as plc:
        print("Connected")
        for i in range(20):
            try:
                txt1 = await plc.txt[1]
                td1 = await plc.td[1]
                c1 = await plc.c[1]
                print(f"Poll #{i+1}: TXT1={txt1!r} TD1={td1} C1={c1}")
            except Exception as e:
                print(f"Poll #{i+1}: ERROR {type(e).__name__}: {e}")
            await asyncio.sleep(1.5)
    print("Done")


asyncio.run(main())
