"""Build and verify every example in examples/ -> out/. Run: python run_examples.py"""
import glob, os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kicad_ai import build

here = os.path.dirname(os.path.abspath(__file__))
os.makedirs(os.path.join(here, "out"), exist_ok=True)
fails = 0
for ex in sorted(glob.glob(os.path.join(here, "examples", "*.json"))):
    name = os.path.splitext(os.path.basename(ex))[0]
    d = os.path.join(here, "out", name)
    os.makedirs(d, exist_ok=True)
    out = os.path.join(d, name + ".kicad_sch")
    try:
        sch, rep = build(ex, out)
    except Exception as e:  # keep going: report and move to the next example
        print("=" * 60); print(name); print("RESULT: FAIL (could not build)\n  ERROR   %s" % e)
        fails += 1
        continue
    with open(os.path.join(d, name + ".kicad_pro"), "w") as f:
        json.dump({"meta": {"filename": name + ".kicad_pro", "version": 1}}, f)
    print("=" * 60); print(name); print(rep)
    fails += not rep.ok
print("\n%d example(s) failed" % fails)
sys.exit(1 if fails else 0)
