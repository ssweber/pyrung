"""Probe: step-by-step compression of how(fill_solv_nc, avoid=HMI_fill).

For each step in the plan, try dropping it and see if the goal still holds.
Iterate until no more steps can be dropped.
"""

import sys

PROJECT = r"C:\Users\ssweb\AppData\Local\Temp\CLICK (00010C00)\pyrung_project"
sys.path.insert(0, PROJECT)

from main import logic  # noqa: E402
from tags import HMI_fill, fill_solv_nc  # noqa: E402

from pyrung import PLC  # noqa: E402

plc = PLC(logic)
plc.step()

# Get the original plan
path = plc.how(fill_solv_nc, avoid=HMI_fill, walk_seconds=120)
assert path.reachable

# Extract the action list
all_steps = [(s.action, s.scans) for s in path.steps]
print(f"Original plan: {len(all_steps)} steps\n")
for i, (action, scans) in enumerate(all_steps):
    print(f"  {i:2d}: {action or '(wait)'}  scans={scans}")


def trial_replay(candidate):
    """Replay candidate plan, return True if goal holds and avoid not hit."""
    fork = plc.fork()
    for action, scans in candidate:
        if action:
            fork.patch(action)
        for _ in range(scans):
            fork.step()
            if fork.state.tags.get("HMI_fill"):
                return False
    return bool(fork.state.tags.get("fill_solv_nc"))


# Iterative greedy drop
print("\n--- Iterative greedy drop ---\n")
current = list(all_steps)
round_num = 0
while True:
    round_num += 1
    dropped_any = False
    i = 0
    while i < len(current):
        action, scans = current[i]
        if not action:
            # Skip empty-action (timing) steps
            i += 1
            continue
        candidate = current[:i] + current[i + 1 :]
        if trial_replay(candidate):
            tag = list(action.keys())[0] if action else "(wait)"
            val = list(action.values())[0] if action else ""
            print(f"  round {round_num}: dropped step {i} ({tag}={val})")
            current = candidate
            dropped_any = True
            # don't increment i — next step shifted into this position
        else:
            i += 1
    if not dropped_any:
        break

print(f"\nCompressed plan: {len(current)} steps (dropped {len(all_steps) - len(current)})\n")
for i, (action, scans) in enumerate(current):
    print(f"  {i:2d}: {action or '(wait)'}  scans={scans}")

# Verify the compressed plan
assert trial_replay(current), "Compressed plan failed verification!"
print("\nVerified: compressed plan reaches fill_solv_nc without HMI_fill.")
