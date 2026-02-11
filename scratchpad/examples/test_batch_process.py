import asyncio
from pyclickplc import ClickClient

async def test():
    async with ClickClient("127.0.0.1:5020") as plc:
        print("=== Initializing PLC ===")
        
        # 1. Turn ON the subroutine first
        await plc.ds.write(3001, 1) # xCall
        await asyncio.sleep(0.1)
        
        # 2. Now trigger the Initialization
        await plc.ds.write(3002, 1) # xInit
        await asyncio.sleep(0.1)
        await plc.ds.write(3002, 0) # Release xInit

        last_step = -1
        while True:
            step = await plc.ds[3012]
            batch = await plc.ds[3017]
            rejects = await plc.ds[3018]
            
            if step != last_step:
                status = "IDLE"
                if step == 3: status = "FILLING"
                elif step == 5: status = "HEATING"
                elif step == 7: status = "QUALITY CHECK"
                elif step == 9: status = "COMPLETE (PASS)"
                elif step == 11: status = "REJECTING (FAIL)"
                
                print(f"Step: {step} | State: {status} | Batch: {batch} | Rejects: {rejects}")
                last_step = step

            # Trigger FastProcess on Step 5 if batch is 2
            if step == 5 and batch == 2:
                print("  >> Boosting heat for Batch 2")
                await plc.ds.write(3020, 1)

            if batch >= 6:
                print("Test sequence complete.")
                break
                
            await asyncio.sleep(0.1)

if __name__ == "__main__":
    asyncio.run(test())
