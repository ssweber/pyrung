import asyncio
import time
from pyclickplc import ClickClient

async def test_sfc_server(host: str = "127.0.0.1", port: int = 5020):
    """Test the SFC template server by running through the sequence."""
    
    async with ClickClient(f"{host}:{port}") as plc:
        print(f"Connected to {host}:{port}")
        
        # Reset and initialize the SFC
        print("\n--- Initializing SFC ---")
        await plc.ds.write(3003, 1)  # xReset
        await asyncio.sleep(0.1)
        await plc.ds.write(3003, 0)  # Clear reset
        
        # Verify we're at step 0 (reset state)
        cur_step = await plc.ds[3012]
        print(f"CurStep after reset: {cur_step} (expected: 0)")
        
        # Start the SFC
        print("\n--- Starting SFC ---")
        await plc.ds.write(3001, 1)  # xCall
        await asyncio.sleep(0.05)
        
        # Poll step progression
        print("Monitoring step progression:")
        step_times = {}
        start_time = time.time()
        
        while True:
            # Read key registers
            cur_step = await plc.ds[3012]
            stored_step = await plc.ds[3013]
            error = await plc.ds[3006]
            one_time_flag = await plc.ds[3017]
            
            # Read timers
            step3_acc = await plc.td[303]  # Step 3 timer accumulator
            subname_t = await plc.td[301]  # Main timer
            
            # Track when we hit each step
            if cur_step not in step_times:
                step_times[cur_step] = time.time() - start_time
                print(f"  Step {cur_step} entered at t={step_times[cur_step]:.2f}s")
                
                if cur_step == 3:
                    print(f"    Step 3 timer started: {step3_acc} ms")
                elif cur_step == 5:
                    print(f"    One-time operation flag: {one_time_flag}")
            
            # Check for errors
            if error:
                error_step = await plc.ds[3007]
                print(f"  ERROR detected at step {error_step}!")
                break
            
            # Verify step 3 timer is counting (should reach 2000ms)
            if cur_step == 3 and step3_acc > 0:
                if step3_acc % 500 < 50:  # Print every ~500ms
                    print(f"    Step 3 timer: {step3_acc}/2000 ms")
            
            # Success condition: reached step 5 and one-time flag is set
            if cur_step == 5 and one_time_flag == 1:
                print(f"\n--- Success! ---")
                print(f"Reached step 5 with one-time flag set")
                print(f"Step progression times: {step_times}")
                break
                
            # Timeout safety
            if time.time() - start_time > 10:
                print("Timeout waiting for sequence")
                break
                
            await asyncio.sleep(0.05)
        
        # Test coil and other registers
        print("\n--- Reading other mapped registers ---")
        subname_x = await plc.c[1501]  # Coil 1501
        print(f"SubName_x (C1501): {subname_x}")
        
        # Read timer bits
        t301 = await plc.t[301]
        t303 = await plc.t[303]
        print(f"SubName_tmr (T301): {t301}, step3_timer (T303): {t303}")
        
        # Stop the SFC
        print("\n--- Stopping SFC ---")
        await plc.ds.write(3001, 0)  # Clear xCall
        
        # Verify shutdown state
        await asyncio.sleep(0.1)
        cur_step = await plc.ds[3012]
        print(f"CurStep after stop: {cur_step} (expected: 0)")
        
        print("\nServer test complete!")

if __name__ == "__main__":
    asyncio.run(test_sfc_server())