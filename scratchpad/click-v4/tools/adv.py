"""Adversarial harness: prover verdict vs brute force, plus strict trace replay."""
import itertools, sys, time
from pyrung.core import PLC
from pyrung.core.analysis.prove import Counterexample, Proven, Intractable, always

SYS_PREFIX = ("sys", "rtc", "fault", "storage", "firmware")


def brute(logic, inputs, hit_name, depth, dt=0.01):
    names = sorted(inputs)
    combos = [dict(zip(names, vals)) for vals in itertools.product(*(inputs[n] for n in names))] or [{}]
    plc0 = PLC(logic, dt=dt)
    frontier = [(plc0, [])]
    for d in range(depth):
        nxt = []
        seen = set()
        for plc, seq in frontier:
            for c in combos:
                p = plc.fork()
                if c:
                    p.patch(c)
                p.step()
                tags = p.current_state.tags
                if tags.get(hit_name) is True:
                    return seq + [c]
                key = tuple(sorted((k, repr(v)) for k, v in tags.items() if not k.startswith("sys.") and not k.startswith("rtc.")))
                key = (key, tuple(sorted((k, repr(v)) for k, v in getattr(p.current_state, "memory", {}).items())))
                if key in seen:
                    continue
                seen.add(key)
                nxt.append((p, seq + [c]))
        frontier = nxt
        if not frontier:
            break
    return None


def replay(logic, v, hit_name, dt=0.01):
    """(hit_at_end, hit_anywhere, total_scans)."""
    plc = PLC(logic, dt=dt)
    anywhere = False
    total = 0
    for st in v.trace:
        ins = {k: x for k, x in st.inputs.items() if k.split(".")[0] not in SYS_PREFIX}
        if ins:
            plc.patch(ins)
        for _ in range(st.scans):
            plc.step()
            total += 1
            if plc.current_state.tags.get(hit_name) is True:
                anywhere = True
    return plc.current_state.tags.get(hit_name) is True, anywhere, total


def prove(logic, hit, **kw):
    kw.setdefault("max_states", 50_000)
    kw.setdefault("depth_budget", 60)
    try:
        return always(logic, ~hit, **kw)
    except Exception as ex:  # noqa: BLE001
        return f"ERROR {type(ex).__name__}: {ex}"


def describe(logic, hit, v):
    if isinstance(v, str):
        return v[:60], None
    name = type(v).__name__
    if isinstance(v, Counterexample):
        end, anyw, scans = replay(logic, v, hit.name)
        return f"{name}(end={end},any={anyw},scans={scans},steps={len(v.trace)},cav={len(v.caveats)})", (end, anyw, v.caveats)
    if isinstance(v, Intractable):
        return f"{name}({v.reason[:50]})", None
    return name, None


def check(name, build, depth=8, quiet=False, **kw):
    logic, hit, inputs = build()
    t = time.time()
    v = prove(logic, hit, **kw)
    t1 = time.time() - t
    gt = brute(logic, inputs, hit.name, depth)
    pv, rp = describe(logic, hit, v)
    truth = f"CEX@{len(gt)}" if gt is not None else f"none<={depth}"
    flag = "ok"
    if gt is not None and isinstance(v, Proven):
        flag = "HOLE(wrong Proven)"
    elif rp is not None and not rp[2] and not rp[0]:
        flag = "BADTRACE(no caveat, end not hit)" if not rp[1] else "TRACE-OVERRUN(hit mid, not end)"
    elif isinstance(v, Counterexample) and gt is None:
        flag = "SPURIOUS?(truth none within depth)"
    print(f"{name:34s} prover={pv:60s} truth={truth:10s} {flag} ({t1:.1f}s)")
    sys.stdout.flush()
    return v, gt, flag
