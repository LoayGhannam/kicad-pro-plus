import argparse, json, os, sys
from . import build, check, Library


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m kicad_ai")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="intent.json -> verified .kicad_sch")
    b.add_argument("intent"); b.add_argument("out")
    b.add_argument("--symbols", action="append", help="symbol library dir (repeatable)")
    b.add_argument("--kicad-cli")
    v = sub.add_parser("verify", help="check an existing .kicad_sch against an intent")
    v.add_argument("sch"); v.add_argument("intent", nargs="?")
    v.add_argument("--kicad-cli")
    a = ap.parse_args(argv)
    if a.cmd == "build":
        lib = Library(a.symbols) if a.symbols else None
        sch, rep = build(a.intent, a.out, lib, a.kicad_cli)
        proj = os.path.splitext(a.out)[0] + ".kicad_pro"
        if not os.path.exists(proj):
            with open(proj, "w") as f:
                json.dump({"meta": {"filename": os.path.basename(proj), "version": 1}}, f)
    else:
        rep = check(a.sch, a.intent, kicad_cli=a.kicad_cli)
    print(rep)
    return 0 if rep.ok else 1


if __name__ == "__main__":
    sys.exit(main())
