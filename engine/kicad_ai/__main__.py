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
    g = sub.add_parser("ai", help="describe a circuit in words -> verified .kicad_sch")
    g.add_argument("request"); g.add_argument("out")
    g.add_argument("--provider"); g.add_argument("--model"); g.add_argument("--base-url")
    g.add_argument("--symbols", action="append")
    c = sub.add_parser("ai-config", help="show or set the AI provider (saved per user)")
    c.add_argument("--provider"); c.add_argument("--model"); c.add_argument("--base-url")
    c.add_argument("--api-key")
    sub.add_parser("ai-test", help="check that the configured AI provider answers")
    a = ap.parse_args(argv)
    if a.cmd in ("ai", "ai-config", "ai-test"):
        from . import ai
        cfg = ai.load_config()
        for k in ("provider", "model", "base_url", "api_key"):
            v = getattr(a, k, None)
            if v:
                cfg[k] = v
        if a.cmd == "ai-config":
            if any(getattr(a, k, None) for k in ("provider", "model", "base_url", "api_key")):
                print("saved to", ai.save_config(cfg))
            shown = dict(cfg)
            if shown.get("api_key"):
                shown["api_key"] = shown["api_key"][:4] + "..." + shown["api_key"][-2:]
            print(json.dumps(shown, indent=2))
            return 0
        if a.cmd == "ai-test":
            try:
                print("reply:", ai.Provider(cfg).test().strip()); return 0
            except ai.AIError as e:
                print("FAILED:", e); return 1
        lib = Library(a.symbols) if a.symbols else None
        try:
            intent, rep = ai.design(a.request, a.out, cfg, lib)
        except ai.AIError as e:
            print("FAILED:", e); return 1
        proj = os.path.splitext(a.out)[0] + ".kicad_pro"
        if not os.path.exists(proj):
            with open(proj, "w") as f:
                json.dump({"meta": {"filename": os.path.basename(proj), "version": 1}}, f)
        with open(os.path.splitext(a.out)[0] + ".intent.json", "w", encoding="utf-8") as f:
            json.dump(intent, f, indent=2, ensure_ascii=False)
        print(rep)
        if intent.get("notes"):
            print("\n" + intent["notes"])
        return 0 if rep.ok else 1
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
