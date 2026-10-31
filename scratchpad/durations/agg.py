import sys, xml.etree.ElementTree as ET
from collections import defaultdict
t = ET.parse(sys.argv[1]).getroot()
files = defaultdict(lambda: [0.0, 0]); total = 0.0
for tc in t.iter("testcase"):
    s = float(tc.get("time", 0)); total += s
    f = tc.get("classname", "").split(".")
    # classname like tests.core.test_x.TestY -> keep module path
    mod = ".".join(p for p in f if not p[:1].isupper())
    files[mod][0] += s; files[mod][1] += 1
print(f"total {total/60:.1f} min over {sum(v[1] for v in files.values())} tests")
dirs = defaultdict(float)
for m, (s, n) in files.items():
    dirs[".".join(m.split(".")[:3])] += s
print("\n-- by dir --")
for d, s in sorted(dirs.items(), key=lambda x: -x[1])[:15]: print(f"{s:8.1f}s {100*s/total:5.1f}%  {d}")
print("\n-- by file --")
for m, (s, n) in sorted(files.items(), key=lambda x: -x[1][0])[:30]: print(f"{s:8.1f}s {100*s/total:5.1f}% {n:5d}  {m}")
