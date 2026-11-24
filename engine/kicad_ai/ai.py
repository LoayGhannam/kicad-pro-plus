"""AI layer: turn a request in plain words (Arabic or English) into an intent JSON,
then let the engine draw it and the verifier check it.

Providers
---------
- anthropic : Claude (Anthropic Messages API)
- openai    : any OpenAI-compatible chat API. Presets: openai, deepseek, ollama,
              plus "custom" with your own base_url (OpenRouter, LM Studio, Groq, ...)

Only the standard library is used (urllib), so it runs inside KiCad's bundled Python.

The model never draws. It only writes components + nets. If the engine rejects the
intent (unknown symbol, wrong pin, ...) the exact error, including the real pin list
from your libraries, is sent back and the model gets another try.
"""
import json
import os
import re
import urllib.error
import urllib.request

PRESETS = {
    "claude":   {"kind": "anthropic", "base_url": "https://api.anthropic.com",
                 "model": "claude-sonnet-4-5", "key_env": "ANTHROPIC_API_KEY"},
    "openai":   {"kind": "openai", "base_url": "https://api.openai.com/v1",
                 "model": "gpt-4.1", "key_env": "OPENAI_API_KEY"},
    "deepseek": {"kind": "openai", "base_url": "https://api.deepseek.com/v1",
                 "model": "deepseek-chat", "key_env": "DEEPSEEK_API_KEY"},
    "ollama":   {"kind": "ollama", "base_url": "http://localhost:11434",
                 "model": "qwen2.5:7b", "key_env": None},
    "custom":   {"kind": "openai", "base_url": "", "model": "", "key_env": "AI_API_KEY"},
}


def config_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    return os.path.join(base, "kodat-pcb", "ai.json")


def load_config(path=None):
    """{"provider": "claude", "model": "...", "base_url": "...", "api_key": "..."}"""
    path = path or config_path()
    cfg = {"provider": "ollama"}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


def save_config(cfg, path=None):
    path = path or config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return path


class AIError(Exception):
    pass


class Provider:
    def __init__(self, cfg):
        name = cfg.get("provider", "ollama")
        if name not in PRESETS:
            raise AIError("unknown provider '%s' (use one of: %s)" % (name, ", ".join(PRESETS)))
        p = dict(PRESETS[name])
        self.name = name
        self.kind = p["kind"]
        self.base_url = (cfg.get("base_url") or p["base_url"]).rstrip("/")
        self.model = cfg.get("model") or p["model"]
        self.api_key = cfg.get("api_key") or (os.environ.get(p["key_env"]) if p["key_env"] else None)
        self.timeout = float(cfg.get("timeout", 900))
        self.num_ctx = cfg.get("num_ctx", 32768)        # Ollama context window
        self.think = cfg.get("think", "medium")          # gpt-oss thinking effort: low/medium/high   # big local models can be slow
        if not self.base_url:
            raise AIError("provider 'custom' needs base_url")
        if not self.model:
            raise AIError("no model set")
        if self.kind == "anthropic" and not self.api_key:
            raise AIError("Claude needs an API key (settings or ANTHROPIC_API_KEY)")

    def _post(self, url, body, headers):
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers=dict(headers, **{"content-type": "application/json"}))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            raise AIError("%s returned HTTP %d: %s" % (self.name, e.code, detail))
        except urllib.error.URLError as e:
            raise AIError("cannot reach %s at %s (%s)" % (self.name, self.base_url, e.reason))

    def chat(self, system, messages, max_tokens=16384):
        """messages: [{"role": "user"|"assistant", "content": str}] -> reply text"""
        if self.kind == "anthropic":
            data = self._post(self.base_url + "/v1/messages",
                              {"model": self.model, "max_tokens": max_tokens,
                               "system": system, "messages": messages},
                              {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"})
            return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        if self.kind == "ollama":
            # native API: lets us raise the context window (Ollama's default is small, and a
            # thinking model can fill it before writing the answer) and cap thinking effort
            root = self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url
            body = {"model": self.model, "stream": False,
                    "messages": [{"role": "system", "content": system}] + messages,
                    "options": {"num_ctx": int(self.num_ctx), "num_predict": max_tokens, "temperature": 0.2}}
            if self.model.startswith("gpt-oss"):
                body["think"] = self.think
            data = self._post(root + "/api/chat", body, {})
            text = (data.get("message") or {}).get("content") or ""
            if not text.strip():
                raise AIError("%s returned an empty answer (it used its whole budget thinking)" % self.model)
            return text
        headers = {"authorization": "Bearer " + self.api_key} if self.api_key else {}
        data = self._post(self.base_url + "/chat/completions",
                          {"model": self.model, "max_tokens": max_tokens, "temperature": 0.2,
                           "messages": [{"role": "system", "content": system}] + messages},
                          headers)
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError):
            raise AIError("unexpected reply from %s: %s" % (self.name, str(data)[:300]))
        if not text.strip():
            raise AIError("%s returned an empty answer (a thinking model may have run out of tokens)" % self.model)
        return text

    def test(self):
        return self.chat("Reply with the single word OK.", [{"role": "user", "content": "ping"}], 2048)


SYSTEM = """You design electronic schematics for KiCad. You do NOT draw: you only list parts and connections.
Reply with ONE JSON object and nothing else (no prose, no markdown fences):

{
  "title": "short title",
  "flow": ["J1", "U1", "J2"],
  "notes": "one or two sentences for the user, in the user's language: what the circuit does and key values",
  "components": [
    {"ref": "U1", "symbol": "Library:SymbolName", "value": "NE555P", "footprint": "Library:Footprint"}
  ],
  "nets": [
    {"name": "GND", "pins": ["U1.1", "C1.2"]}
  ]
}

Rules:
- "symbol" must be a real symbol from the standard KiCad 10 libraries (e.g. Device:R, Device:C, Device:C_Polarized,
  Device:LED, Device:R_Potentiometer, Timer:NE555P, Amplifier_Operational:LM358, Regulator_Linear:AMS1117-3.3,
  Connector_Generic:Conn_01x02, Connector:USB_C_Receptacle_USB2.0_16P ...).
- Pins are "REF.PIN" using the symbol's pin NUMBER (pin names are accepted when unique).
- Every pin appears in at most one net. Unused pins: leave them out (they get a no-connect flag).
- Power nets: use KiCad power names (GND, +5V, +3V3, +9V, +12V, VCC ...) so power symbols are drawn.
- Add the decoupling capacitors and pull-ups a careful engineer would add. Choose sensible footprints.
- Use values and topology from the part's datasheet typical application.
- refs are letters + number only: R1, C3, D1, U1, J1, Q1 (never "LED", "Cdec", "Rled").
- Every pin of a resistor, capacitor, LED or diode must be in a net. IC inputs (reset, enable, control)
  must be tied as the datasheet says, never left floating. Include the power input connector.
- Footprints must be exact KiCad 10 names, e.g. Resistor_SMD:R_0805_2012Metric, Capacitor_SMD:C_0805_2012Metric,
  LED_SMD:LED_0805_2012Metric, Package_DIP:DIP-8_W7.62mm, Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal.
- "flow" lists the main signal path from input to output (optional).
If you receive an ERROR message, fix exactly that problem and reply with the full corrected JSON."""


def _extract_json(text):
    t = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()   # reasoning models
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b < a:
        raise AIError("the model did not return JSON")
    return json.loads(t[a:b + 1])


def _footprint_dirs():
    import glob
    dirs = [v for k, v in os.environ.items() if k.startswith("KICAD") and k.endswith("FOOTPRINT_DIR") and v]
    dirs += ["/usr/share/kicad/footprints", r"D:\programs\kicad\share\kicad\footprints"]
    dirs += sorted(glob.glob(r"C:\Program Files\KiCad\*\share\kicad\footprints"), reverse=True)
    return [d for d in dirs if os.path.isdir(d)]


def _prefix(lib_id):
    lib, name = lib_id.split(":", 1)
    n = name.upper()
    if lib == "Device":
        for pre, keys in (("D", ("LED", "D_", "D")), ("R", ("R",)), ("C", ("C",)), ("L", ("L",)),
                          ("Q", ("Q_",)), ("Y", ("CRYSTAL", "RESONATOR")), ("F", ("FUSE",)),
                          ("LS", ("SPEAKER",)), ("BT", ("BATTERY",))):
            if any(n == k or n.startswith(k) for k in keys):
                return pre
    if lib.startswith("Connector"):
        return "J"
    if lib.startswith("Switch"):
        return "SW"
    return "U"


def _find_fp(fps, fp):
    lib, name = fp.split(":", 1)
    return any(os.path.exists(os.path.join(d, lib + ".pretty", name + ".kicad_mod")) for d in fps)


def autofix(intent, library):
    """Fix the mechanical mistakes ourselves instead of spending a whole model round trip.
    Returns a list of what was changed."""
    fixes = []
    comps = intent.get("components", [])
    taken = {c.get("ref", "") for c in comps}
    rename = {}
    for c in comps:
        ref = c.get("ref", "")
        if not re.match(r"^[A-Z]+[0-9]+$", ref) and c.get("symbol", "").count(":") == 1:
            pre = _prefix(c["symbol"]); k = 1
            while "%s%d" % (pre, k) in taken:
                k += 1
            new = "%s%d" % (pre, k); taken.add(new)
            rename[ref] = new; c["ref"] = new
            fixes.append("renamed %s -> %s" % (ref, new))
    syms = {}
    for c in comps:
        try:
            syms[c["ref"]] = library.get(c["symbol"])
        except Exception:
            pass
    for n in intent.get("nets", []):
        out = []
        for p in n.get("pins", []):
            ref, _, pin = p.partition(".")
            ref = rename.get(ref, ref)
            if ref in syms:
                try:
                    pin = syms[ref].pin(pin).number
                except KeyError:
                    pass
            out.append("%s.%s" % (ref, pin))
        n["pins"] = out
    fps = _footprint_dirs()
    if fps:
        for c in comps:
            fp = c.get("footprint", "")
            if fp and ":" in fp and _find_fp(fps, fp):
                continue
            sym = syms.get(c["ref"])
            pick = None
            if sym is not None and sym.default_footprint and ":" in sym.default_footprint \
                    and _find_fp(fps, sym.default_footprint):
                pick = sym.default_footprint
            m = re.match(r"Conn_0([12])x(\d+)", c.get("symbol", "").split(":")[-1])
            if pick is None and m:
                pick = "Connector_PinHeader_2.54mm:PinHeader_%sx%s_P2.54mm_Vertical" % (m.group(1), m.group(2))
            if pick is None and fp and ":" in fp:
                lib, name = fp.split(":", 1)
                key = [k for k in re.split(r"[_-]", name) if k][:2]
                for d in fps:
                    pd = os.path.join(d, lib + ".pretty")
                    if os.path.isdir(pd):
                        cands = sorted(f[:-10] for f in os.listdir(pd) if f.endswith(".kicad_mod")
                                       and all(k.lower() in f.lower() for k in key))
                        cands.sort(key=lambda f: ("HandSolder" in f, len(f)))
                        if cands:
                            pick = lib + ":" + cands[0]; break
            if pick and (not fp or not (":" in fp and _find_fp(fps, fp))):
                fixes.append("%s footprint %s -> %s" % (c["ref"], fp or "(none)", pick))
                c["footprint"] = pick
    return fixes


def lint(intent, library):
    """Design-sense checks the drawing verifier cannot see. Returns a list of problems."""
    probs = []
    used = {}
    symof = {c.get("ref"): c.get("symbol") for c in intent.get("components", [])}
    for n in intent.get("nets", []):
        for p in n.get("pins", []):
            used[p] = n["name"]
            ref, _, pin = p.partition(".")
            try:
                library.get(symof[ref]).pin(pin)
            except KeyError as e:
                probs.append(str(e).strip("'\"") if ref in symof else "net %s uses unknown part %s" % (n["name"], ref))
            except Exception:
                pass
    fps = _footprint_dirs()
    for c in intent.get("components", []):
        ref = c.get("ref", "")
        if not re.match(r"^[A-Z]+[0-9]+$", ref):
            probs.append("ref '%s' must be letters + number (R1, C2, D1, U1, J1)" % ref)
        try:
            sym = library.get(c["symbol"])
        except Exception:
            continue                                  # the engine reports unknown symbols itself
        pins = [p for p in sym.pins if not p.hidden]
        def connected(p):
            return ("%s.%s" % (ref, p.number) in used) or ("%s.%s" % (ref, p.name) in used)
        if len(pins) <= 3 and all(p.etype == "passive" for p in pins) or sym.lib_id.startswith("Device:LED") \
                or sym.lib_id.startswith("Device:D"):
            for p in pins:
                if not connected(p):
                    probs.append("%s (%s) pin %s is not connected: a part with a floating pin does nothing"
                                 % (ref, c.get("value", ""), p.number))
        else:
            for p in pins:
                if p.etype in ("input", "power_in") and p.name.upper() not in ("NC", "N.C.") and not connected(p):
                    probs.append("%s pin %s (%s) is an %s and is left unconnected; tie it as the datasheet says (write it as %s.%s)"
                                 % (ref, p.number, p.name, p.etype, ref, p.number))
        fp = c.get("footprint", "")
        if fps and fp:
            if ":" not in fp:
                probs.append("%s footprint '%s' must be Library:Name" % (ref, fp))
            else:
                lib, name = fp.split(":", 1)
                if not any(os.path.exists(os.path.join(d, lib + ".pretty", name + ".kicad_mod")) for d in fps):
                    near = []
                    for d in fps:
                        pd = os.path.join(d, lib + ".pretty")
                        if os.path.isdir(pd):
                            key = re.split(r"[_-]", name)[:2]
                            near += [f[:-10] for f in os.listdir(pd) if f.endswith(".kicad_mod")
                                     and all(k.lower() in f.lower() for k in key)][:6]
                    probs.append("%s footprint '%s' does not exist%s" % (
                        ref, fp, (". Close matches in %s: %s" % (lib, ", ".join(near))) if near else ""))
    return probs


def design(request, out_path, cfg=None, library=None, max_tries=4, log=print):
    """request -> verified .kicad_sch at out_path. Returns (intent, report)."""
    from .engine import IntentError
    from . import build

    prov = Provider(cfg or load_config())
    messages = [{"role": "user", "content": request}]
    last_err = None
    for attempt in range(1, max_tries + 1):
        log("[%s/%s] asking %s (%s)..." % (attempt, max_tries, prov.name, prov.model))
        try:
            reply = prov.chat(SYSTEM, messages)
        except AIError as e:
            if "empty answer" not in str(e):
                raise
            last_err = str(e)
            log("    " + last_err + ", asking again with less thinking")
            if getattr(prov, "think", None) == "high":
                prov.think = "medium"
            elif getattr(prov, "think", None) == "medium":
                prov.think = "low"
            continue
        messages.append({"role": "assistant", "content": reply})
        try:
            intent = _extract_json(reply)
        except (AIError, ValueError) as e:
            last_err = "Your reply was not valid JSON (%s). Reply with the JSON object only." % e
            messages.append({"role": "user", "content": "ERROR: " + last_err})
            continue
        from .library import Library
        library = library or Library()
        for f in autofix(intent, library):
            log("    auto-fixed: " + f)
        problems = lint(intent, library)
        if problems:
            last_err = "; ".join(problems[:8])
            log("    design check failed: " + last_err)
            messages.append({"role": "user", "content": "ERROR: design problems: " + last_err})
            continue
        try:
            sch, rep = build(intent, out_path, library)
        except IntentError as e:
            last_err = str(e)
            log("    engine rejected it: " + last_err)
            messages.append({"role": "user", "content": "ERROR: " + last_err})
            continue
        if not rep.ok:
            last_err = "; ".join(rep.errors[:6])
            log("    verifier failed: " + last_err)
            messages.append({"role": "user", "content": "ERROR: the drawn schematic failed checks: " + last_err})
            continue
        return intent, rep
    raise AIError("no valid design after %d tries. Last problem: %s" % (max_tries, last_err))
