from __future__ import annotations
import asyncio
from pathlib import Path

from pyclickplc import DataviewFile, DataviewRow, write_cdv
from pyclickplc.server import ClickServer
from pyrung.click import ClickDataProvider, TagMap, c, ds, t, td
from pyrung.core import (
    Bool, Int, PLCRunner, Program, Rung,
    branch, TimeMode, TimeUnit, any_of, call, copy,
    latch, math, on_delay, reset, return_, subroutine,
    named_array,
)

@named_array(Int, count=1, stride=20)
class SubNameDs:
    xCall = 0
    xInit = 0
    xReset = 0
    xPause = 0
    init = 0
    Error = 0
    ErrorStep = 0
    EnableLimit = 0
    Limit_Ts = 30
    ResetTmr = 0
    Trans = 0
    CurStep = 0
    StoredStep = 0
    _x = 0
    _init = 0
    _ValStepIsOdd = 0
    Batch_Counter = 0
    Reject_Counter = 0
    Quality_Mode = 0
    FastProcess = 0

sub = SubNameDs[1]
# Temp tag for batch parity calculation
BatchIsOdd = Int("BatchIsOdd")

FillValve, Heater, Complete, Reject = Bool("FillV"), Bool("Heat"), Bool("Comp"), Bool("Rej")
Fill_Acc, Heat_Acc, Check_Acc, Comp_Acc, Rej_Acc = Int("FA"), Int("HA"), Int("CA"), Int("COA"), Int("RA")
Fill_T, Heat_T, Check_T, Comp_T, Rej_T = Bool("FT"), Bool("HT"), Bool("CT"), Bool("COT"), Bool("RT")

def main() -> Program:
    with Program() as logic:
        with Rung(sub.xCall == 1, sub.xPause != 1): copy(1, sub._x)
        with Rung(sub._x == 1): call("SubName")
        with subroutine("SubName"):
            
            # --- PRE-CALCULATIONS ---
            # We must calculate Modulo here, because we can't do % inside a branch contact
            with Rung():
                math(sub.Batch_Counter % 2, BatchIsOdd)

            # --- INIT ---
            with Rung(any_of(sub.xInit == 1, sub.xReset == 1)):
                copy(1, sub.CurStep); reset(FillValve); reset(Heater)
                copy(0, sub.Batch_Counter); copy(0, sub.Reject_Counter)
                copy(0, sub.Quality_Mode); copy(0, sub.FastProcess)
            
            # --- STEPS ---
            with Rung(sub.CurStep == 1): copy(1, sub.Trans)

            with Rung(sub.CurStep == 3):
                latch(FillValve)
                on_delay(Fill_T, Fill_Acc, setpoint=2000, time_unit=TimeUnit.Tms)
            with Rung(sub.CurStep == 3, Fill_T):
                reset(FillValve); copy(1, sub.Trans)

            with Rung(sub.CurStep == 5):
                latch(Heater)
                on_delay(Heat_T, Heat_Acc, setpoint=3000, time_unit=TimeUnit.Tms)
            with Rung(sub.CurStep == 5):
                with branch(Heat_T): reset(Heater); copy(1, sub.Trans)
                with branch(sub.FastProcess == 1): reset(Heater); copy(0, sub.FastProcess); copy(1, sub.Trans)

            with Rung(sub.CurStep == 7):
                on_delay(Check_T, Check_Acc, setpoint=500, time_unit=TimeUnit.Tms)
            
            # --- CRITICAL FIX IN STEP 7 ---
            with Rung(sub.CurStep == 7, Check_T):
                # Pass logic (Step 9) -> Uses pre-calculated BatchIsOdd
                with branch(sub.Quality_Mode == 1): copy(1, sub.Trans)
                with branch(sub.Quality_Mode == 0, BatchIsOdd == 0): copy(1, sub.Trans)
                
                # Fail logic (Step 11)
                with branch(sub.Quality_Mode == 2, sub.Trans == 0): copy(3, sub.Trans)
                with branch(sub.Quality_Mode == 0, BatchIsOdd == 1, sub.Trans == 0): copy(3, sub.Trans)

            with Rung(sub.CurStep == 9):
                latch(Complete)
                copy(sub.Batch_Counter + 1, sub.Batch_Counter, oneshot=True)
                on_delay(Comp_T, Comp_Acc, setpoint=500, time_unit=TimeUnit.Tms)
            with Rung(sub.CurStep == 9, Comp_T):
                reset(Complete)
                # FIX: Loop to 1, not 0
                math(1, sub.CurStep) 

            with Rung(sub.CurStep == 11):
                latch(Reject)
                copy(sub.Reject_Counter + 1, sub.Reject_Counter, oneshot=True)
                copy(sub.Batch_Counter + 1, sub.Batch_Counter, oneshot=True)
                on_delay(Rej_T, Rej_Acc, setpoint=1000, time_unit=TimeUnit.Tms)
            with Rung(sub.CurStep == 11, Rej_T):
                reset(Reject); math(2, sub.CurStep)

            # --- ENGINE ---
            with Rung(): math(sub.CurStep % 2, sub._ValStepIsOdd)
            with Rung(sub._ValStepIsOdd == 0, sub.CurStep > 0):
                math(sub.CurStep + 1, sub.CurStep)
            with Rung(sub.Trans > 0):
                copy(0, Fill_Acc); copy(0, Heat_Acc); copy(0, Check_Acc); copy(0, Comp_Acc); copy(0, Rej_Acc)
                math(sub.CurStep + sub.Trans, sub.CurStep)
                copy(0, sub.Trans)
            
            with Rung(sub.xCall == 0): copy(0, sub.CurStep); return_()
            with Rung(): return_()
    return logic

def build_mapping() -> TagMap:
    return TagMap([
        *SubNameDs.map_to(ds.select(3001, 3020)),
        # Map the temp variable safely outside the struct range
        BatchIsOdd.map_to(ds[3022]),
        
        FillValve.map_to(c[1502]), Heater.map_to(c[1503]), Complete.map_to(c[1504]), Reject.map_to(c[1505]),
        Fill_T.map_to(t[302]), Heat_T.map_to(t[303]), Check_T.map_to(t[304]), Comp_T.map_to(t[305]), Rej_T.map_to(t[306]),
        Fill_Acc.map_to(td[302]), Heat_Acc.map_to(td[303]), Check_Acc.map_to(td[304]), Comp_Acc.map_to(td[305]), Rej_Acc.map_to(td[306]),
    ])


def export_click_addresses(tag_map: TagMap) -> tuple[Path, Path]:
    base_dir = Path(__file__).parent
    csv_path = base_dir / "batch_process_click_addresses.csv"
    cdv_path = base_dir / "batch_process_click_addresses.cdv"

    tag_map.to_nickname_file(csv_path)

    rows = [
        DataviewRow(address=slot.hardware_address)
        for slot in tag_map.mapped_slots()
        if slot.source == "user"
    ]
    for row in rows:
        row.update_data_type()

    write_cdv(cdv_path, DataviewFile(rows=rows))
    return csv_path, cdv_path


async def run_server():
    runner = PLCRunner(logic=main())
    runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.01)
    tag_map = build_mapping()
    csv_path, cdv_path = export_click_addresses(tag_map)
    print(f"Exported Click addresses: {csv_path}")
    print(f"Exported Click DataView: {cdv_path}")
    server = ClickServer(ClickDataProvider(runner, tag_map), host="127.0.0.1", port=5020)
    await server.start()
    while True:
        runner.step()
        await asyncio.sleep(0.01)

if __name__ == "__main__":
    asyncio.run(run_server())
