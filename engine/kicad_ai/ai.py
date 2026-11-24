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
import time
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
        self.think = cfg.get("think", "medium")          # gpt-oss: low/medium/high; others: on/off
        self.progress = None                              # callback(str) for live status
        self.skip_hw = bool(cfg.get("skip_hardware_check"))   # big local models can be slow
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

    def _stream_ollama(self, url, body):
        """Stream so a long answer never hits the timeout, and report live progress."""
        body = dict(body, stream=True)
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers={"content-type": "application/json"})
        parts, n_think, n_text, t0, last, first = [], 0, 0, time.time(), 0, None
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                for line in r:
                    if not line.strip():
                        continue
                    d = json.loads(line.decode("utf-8"))
                    if d.get("error"):
                        raise AIError("ollama: %s" % d["error"])
                    m = d.get("message") or {}
                    if (m.get("thinking") or m.get("content")) and first is None:
                        first = time.time()             # speed is measured after the model has loaded
                    if first is not None and not self.skip_hw:
                        from .hardware import too_slow, MIN_TOKENS_PER_S
                        el = time.time() - first
                        if too_slow(n_think + n_text, el):
                            raise AIError("HARDWARE: Too slow on this computer\nThe model is producing %.1f tokens "
                                          "per second (minimum %.0f). A design needs several thousand tokens and "
                                          "several attempts, so it would take far too long. Use a smaller model, "
                                          "a computer with more graphics memory, or an online provider in Settings."
                                          % ((n_think + n_text) / el, MIN_TOKENS_PER_S))
                    if m.get("thinking"):
                        n_think += 1
                    if m.get("content"):
                        parts.append(m["content"]); n_text += 1
                    now = time.time()
                    if self.progress and now - last > 2:
                        last = now
                        rate = (n_think + n_text) / max(now - t0, 0.1)
                        what = "writing the design" if n_text else ("thinking" if n_think else "loading model")
                        self.progress("%s... %d tokens (%.1f tokens/s)" % (what, n_think + n_text, rate))
                    if d.get("done"):
                        break
        except urllib.error.HTTPError as e:
            raise AIError("ollama returned HTTP %d: %s" % (e.code, e.read().decode("utf-8", "replace")[:300]))
        except urllib.error.URLError as e:
            raise AIError("cannot reach ollama at %s (%s)" % (self.base_url, e.reason))
        return "".join(parts)

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
            elif self.think in (True, False, "on", "off"):
                body["think"] = self.think in (True, "on")
            else:
                # qwen3 / deepseek-r1 / others: thinking off by default, big local models
                # are too slow on a laptop GPU when they think for thousands of tokens
                body["think"] = False if re.match(r"(qwen3|deepseek-r1|magistral)", self.model) else None
                if body["think"] is None:
                    del body["think"]
            text = self._stream_ollama(root + "/api/chat", body)
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
    fp_as_sym = {"Resistor_SMD": "Device:R", "Resistor_THT": "Device:R", "Capacitor_SMD": "Device:C",
                 "Capacitor_THT": "Device:C", "Capacitor_Tantalum_SMD": "Device:C_Polarized",
                 "LED_SMD": "Device:LED", "LED_THT": "Device:LED", "Diode_SMD": "Device:D", "Diode_THT": "Device:D",
                 "Inductor_SMD": "Device:L", "Inductor_THT": "Device:L", "Crystal": "Device:Crystal"}
    for c in comps:
        sym = c.get("symbol", "")
        lib = sym.split(":")[0]
        if lib in fp_as_sym:
            if not c.get("footprint"):
                c["footprint"] = sym
            c["symbol"] = fp_as_sym[lib]
            if "Polarized" not in c["symbol"] and lib.startswith("Capacitor") and \
                    re.search(r"(\d+(\.\d+)?)\s*[uµ]F", str(c.get("value", ""))) and \
                    float(re.search(r"(\d+(\.\d+)?)", str(c.get("value"))).group(1)) >= 10:
                c["symbol"] = "Device:C_Polarized"
            fixes.append("%s symbol %s -> %s (that was a footprint library)" % (c.get("ref"), sym, c["symbol"]))
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
        except KeyError:
            import difflib
            sym = c.get("symbol", "")
            if sym.count(":") != 1:
                continue
            lib, name = sym.split(":")
            try:
                near = difflib.get_close_matches(name, library.names(lib), n=1, cutoff=0.85)
            except KeyError:
                near = []
            if near:
                c["symbol"] = lib + ":" + near[0]
                fixes.append("%s symbol %s -> %s" % (c["ref"], sym, c["symbol"]))
                try:
                    syms[c["ref"]] = library.get(c["symbol"])
                except KeyError:
                    pass
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
            if pick is None:
                import difflib
                lib, name = (fp.split(":", 1) + [""])[:2] if fp and ":" in fp else ("", fp or "")
                pool = []
                for d in fps:
                    pd = os.path.join(d, lib + ".pretty") if lib else ""
                    if lib and os.path.isdir(pd):
                        pool += [lib + ":" + f[:-10] for f in os.listdir(pd) if f.endswith(".kicad_mod")]
                if pool:
                    m2 = difflib.get_close_matches(fp, pool, n=1, cutoff=0.5)
                    pick = m2[0] if m2 else None
                if pick is None:
                    thru = "THT" in (fp or "")
                    sid = c.get("symbol", "")
                    defaults = {"Device:LED": ("LED_THT:LED_D5.0mm", "LED_SMD:LED_0805_2012Metric"),
                                "Device:R": ("Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal",
                                             "Resistor_SMD:R_0805_2012Metric"),
                                "Device:C": ("Capacitor_THT:C_Disc_D5.0mm_W2.5mm_P5.00mm",
                                             "Capacitor_SMD:C_0805_2012Metric"),
                                "Device:C_Polarized": ("Capacitor_THT:CP_Radial_D5.0mm_P2.00mm",
                                                       "Capacitor_SMD:CP_Elec_5x5.4")}
                    if sid in defaults:
                        cand = defaults[sid][0 if thru else 1]
                        if _find_fp(fps, cand):
                            pick = cand
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
    is_gnd = lambda n: bool(n) and (n.upper().startswith("GND") or n.upper() in ("VSS", "0V", "EARTH"))
    power_like = {n["name"] for n in intent.get("nets", [])
                  if re.match(r"^(\+|-)?\d|^V(CC|DD|IN|BAT|BUS)|^\+", n["name"].upper())}
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
        # --- electrical sense ------------------------------------------------
        def net_of(p):
            return used.get("%s.%s" % (ref, p.number)) or used.get("%s.%s" % (ref, p.name))
        for p in pins:
            n = net_of(p)
            if not n:
                continue
            nm = p.name.upper()
            active_low = "~" in p.name
            core = nm.replace("~{", "").replace("}", "").replace("~", "")
            if active_low and any(k in core for k in ("RST", "RESET", "MR", "CLR")) and is_gnd(n):
                probs.append("%s pin %s (%s) is an active-low reset tied to %s: the chip would stay in reset "
                             "forever. Tie it to the supply (or through a pull-up)" % (ref, p.number, core, n))
            if p.etype == "power_in" and core in ("VCC", "VDD", "V+", "VIN") and is_gnd(n):
                probs.append("%s supply pin %s (%s) is on %s" % (ref, p.number, core, n))
            if p.etype == "power_in" and core in ("GND", "VSS", "V-") and not is_gnd(n) and n in power_like:
                probs.append("%s ground pin %s (%s) is on %s" % (ref, p.number, core, n))
        if sym.lib_id.split(":")[1].upper().startswith("LED") or sym.lib_id == "Device:LED":
            pa = next((p for p in pins if p.name.upper() in ("A", "ANODE")), None)
            pk = next((p for p in pins if p.name.upper() in ("K", "CATHODE")), None)
            if pa and pk:
                na, nk = net_of(pa), net_of(pk)
                if na and is_gnd(na):
                    probs.append("%s is reversed: its anode (pin %s) is on %s. Anode goes toward the supply/driver, "
                                 "cathode (pin %s) toward ground" % (ref, pa.number, na, pk.number))
                if na and nk:
                    series = False
                    for r in intent.get("components", []):
                        if not r.get("symbol", "").startswith("Device:R"):
                            continue
                        rn = {used.get("%s.1" % r["ref"]), used.get("%s.2" % r["ref"])}
                        if (na in rn or nk in rn) and not ({na, nk} <= rn):
                            series = True
                    if not series:
                        probs.append("%s has no series current-limiting resistor (a resistor across the LED, between "
                                     "the same two nets, does not limit current)" % ref)
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


def design(request, out_path, cfg=None, library=None, max_tries=6, log=print, progress=None):
    """request -> verified .kicad_sch at out_path. Returns (intent, report)."""
    from .engine import IntentError
    from . import build

    cfg = cfg or load_config()
    prov = Provider(cfg)
    prov.progress = progress
    if prov.kind == "ollama" and not cfg.get("skip_hardware_check"):
        from . import hardware
        ok, level, title, text = hardware.assess(prov.base_url, prov.model)
        if not ok:
            raise AIError("HARDWARE: %s\n%s" % (title, text))
        if level == "weak":
            log("    note: " + title + ". " + text)
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
            else:
                prov.think = "off"
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
        fixed = autofix(intent, library)
        for f in fixed:
            log("    auto-fixed: " + f)
        if fixed:
            # let the model continue from the corrected design, not repeat its old mistakes
            messages[-1] = {"role": "assistant", "content": json.dumps(intent, ensure_ascii=False)}
        problems = lint(intent, library)
        if problems:
            last_err = "; ".join(problems[:8])
            log("    design check failed: " + last_err)
            messages.append({"role": "user", "content": "ERROR: design problems in your JSON above: " + last_err +
                             ". Keep everything else exactly as it is (same refs), fix only these, and reply "
                             "with the full JSON."})
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
