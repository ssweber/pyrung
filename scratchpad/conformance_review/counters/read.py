import asyncio
from pyclickplc.client import ClickClient
NAMES = ["Cmd","Scratch","R1","R2","R3","R4","Fired","Err","Ready"]
CASES = ["level_vs_edge","survives_rung_false","preset_raised_resumes","preset_lowered_while_idle","reset_wins_over_count","reset_zero_preset","count_down_negative","up_down_same_scan","acc_clamps_at_dint_range"]
async def main():
    async with ClickClient("192.168.1.238", 502) as c:
        ds = await c.ds.read(1301, 1400)
        print("hash DS1301", ds)  if False else None
        vals = list(ds.values()) if hasattr(ds, "values") else ds
        print("header", vals[:10])
        for i, name in enumerate(CASES, 1):
            row = vals[10*i:10*i+9]
            print(f"{i} {name:28s}", dict(zip(NAMES,row)))
        for n in [1,2,11,21,31,41,51,61,71,81,82]:
            print("CT", n, await c.ct[n], "CTD", await c.ctd[n])
        print("C1301..1310", await c.c.read(1301,1310))
        print("SC11", await c.sc[11])
asyncio.run(main())
