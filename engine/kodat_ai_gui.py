"""KODAT PCB - AI window.

A small window for the AI designer: write what you want, watch its status
(thinking / checking / error / done), then open the result in KODAT PCB.
Runs with KiCad's own Python (pythonw.exe), which ships wxPython.
"""
import datetime
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request

import wx

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from kicad_ai import ai  # noqa: E402

OUT_ROOT = os.path.join(HERE, "out")

COLORS = {
    "idle": wx.Colour(140, 140, 140),
    "busy": wx.Colour(60, 140, 255),
    "check": wx.Colour(240, 160, 40),
    "error": wx.Colour(230, 70, 70),
    "done": wx.Colour(60, 200, 90),
}


def slug(text):
    s = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")[:32]
    return (s or "design") + datetime.datetime.now().strftime("_%H%M%S")


def ollama_models(base_url):
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    with urllib.request.urlopen(root + "/api/tags", timeout=4) as r:
        return [m["name"] for m in json.loads(r.read().decode("utf-8")).get("models", [])]


class StatusBar(wx.Panel):
    """Coloured dot + message + elapsed time + pulsing bar."""

    def __init__(self, parent):
        super().__init__(parent)
        self.state = "idle"
        self.t0 = None
        self.dot = wx.Panel(self, size=(14, 14))
        self.msg = wx.StaticText(self, label="Ready")
        f = self.msg.GetFont(); f.SetPointSize(f.GetPointSize() + 1); f.SetWeight(wx.FONTWEIGHT_BOLD)
        self.msg.SetFont(f)
        self.clock = wx.StaticText(self, label="")
        self.gauge = wx.Gauge(self, range=100, size=(-1, 6))
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(self.dot, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        row.Add(self.msg, 1, wx.ALIGN_CENTER_VERTICAL)
        row.Add(self.clock, 0, wx.ALIGN_CENTER_VERTICAL)
        col = wx.BoxSizer(wx.VERTICAL)
        col.Add(row, 0, wx.EXPAND)
        col.Add(self.gauge, 0, wx.EXPAND | wx.TOP, 6)
        self.SetSizer(col)
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._tick, self.timer)
        self.set("idle", "Ready")

    def set(self, state, text):
        self.state = state
        self.dot.SetBackgroundColour(COLORS[state]); self.dot.Refresh()
        self.msg.SetLabel(text)
        self.msg.SetForegroundColour(COLORS[state] if state in ("error", "done") else wx.NullColour)
        if state in ("busy", "check"):
            if self.t0 is None:
                self.t0 = time.time()
            if not self.timer.IsRunning():
                self.timer.Start(250)
        else:
            self.timer.Stop()
            self.gauge.SetValue(100 if state == "done" else 0)
        self.Layout()

    def reset_clock(self):
        self.t0 = None
        self.clock.SetLabel("")

    def _tick(self, _evt):
        self.gauge.Pulse()
        if self.t0 is not None:
            s = int(time.time() - self.t0)
            self.clock.SetLabel("%02d:%02d" % (s // 60, s % 60))


class DesignPage(wx.Panel):
    def __init__(self, parent, frame):
        super().__init__(parent)
        self.frame = frame
        self.worker = None
        self.cancelled = False
        self.result = None

        lab = wx.StaticText(self, label="Describe the circuit (Arabic or English):")
        self.req = wx.TextCtrl(self, style=wx.TE_MULTILINE, size=(-1, 90),
                               value="LED blinker with NE555 at 1 Hz, 9V supply")
        self.go = wx.Button(self, label="Generate")
        self.stop = wx.Button(self, label="Stop"); self.stop.Disable()
        self.status = StatusBar(self)
        self.who = wx.StaticText(self, label="")
        self.log = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL)
        self.notes = wx.StaticText(self, label="")
        self.open = wx.Button(self, label="Open in KODAT PCB"); self.open.Disable()
        self.folder = wx.Button(self, label="Open folder"); self.folder.Disable()

        btns = wx.BoxSizer(wx.HORIZONTAL)
        btns.Add(self.go, 0, wx.RIGHT, 6); btns.Add(self.stop, 0); btns.AddStretchSpacer()
        btns.Add(self.who, 0, wx.ALIGN_CENTER_VERTICAL)
        res = wx.BoxSizer(wx.HORIZONTAL)
        res.Add(self.open, 0, wx.RIGHT, 6); res.Add(self.folder, 0)
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(lab, 0, wx.ALL, 8)
        s.Add(self.req, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        s.Add(btns, 0, wx.EXPAND | wx.ALL, 8)
        s.Add(self.status, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        s.Add(self.log, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        s.Add(self.notes, 0, wx.EXPAND | wx.ALL, 8)
        s.Add(res, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.SetSizer(s)

        self.go.Bind(wx.EVT_BUTTON, self.on_go)
        self.stop.Bind(wx.EVT_BUTTON, self.on_stop)
        self.open.Bind(wx.EVT_BUTTON, self.on_open)
        self.folder.Bind(wx.EVT_BUTTON, lambda e: os.startfile(os.path.dirname(self.result)))
        self.refresh_who()

    def refresh_who(self):
        c = ai.load_config()
        self.who.SetLabel("AI: %s / %s" % (c.get("provider", "?"), c.get("model") or ai.PRESETS.get(c.get("provider", ""), {}).get("model", "?")))
        self.Layout()

    def prog(self, text):
        def upd():
            if self.status.state == "busy":
                self.status.msg.SetLabel(self.status.msg.GetLabel().split("  |")[0] + "  |  " + text)
        wx.CallAfter(upd)

    def say(self, line):
        wx.CallAfter(self._say, line)

    def _say(self, line):
        self.log.AppendText(time.strftime("%H:%M:%S  ") + line + "\n")
        low = line.lower()
        if "asking" in low:
            m = re.search(r"\[(\d+)/(\d+)\]", line)
            n = "attempt %s of %s" % m.groups() if m else ""
            self.status.set("busy", "AI is thinking... %s" % n)
        elif "rejected" in low or "verifier failed" in low:
            self.status.set("check", "AI made a mistake, sending it back to fix")

    def on_go(self, _evt):
        text = self.req.GetValue().strip()
        if not text:
            return
        try:
            prov = ai.Provider(ai.load_config())
        except ai.AIError as e:
            self.status.set("error", "Settings problem: %s" % e)
            return
        self.cancelled = False
        self.result = None
        self.log.Clear(); self.notes.SetLabel("")
        self.open.Disable(); self.folder.Disable()
        self.go.Disable(); self.stop.Enable()
        self.status.reset_clock()
        self.status.set("busy", "Connecting to %s..." % prov.name)
        name = slug(text)
        out = os.path.join(OUT_ROOT, name, name + ".kicad_sch")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        self.worker = threading.Thread(target=self._run, args=(text, out, prov), daemon=True)
        self.worker.start()

    def _run(self, text, out, prov):
        try:
            if prov.name == "ollama":
                try:
                    ollama_models(prov.base_url)
                except Exception:
                    raise ai.AIError("Ollama is not running. Start the Ollama app and try again.")
            intent, rep = ai.design(text, out, {"provider": prov.name, "model": prov.model,
                                                "base_url": prov.base_url, "api_key": prov.api_key},
                                    log=self.say, progress=self.prog)
            proj = os.path.splitext(out)[0] + ".kicad_pro"
            with open(proj, "w") as f:
                json.dump({"meta": {"filename": os.path.basename(proj), "version": 1}}, f)
            with open(os.path.splitext(out)[0] + ".intent.json", "w", encoding="utf-8") as f:
                json.dump(intent, f, indent=2, ensure_ascii=False)
            wx.CallAfter(self._done, out, intent, rep)
        except Exception as e:  # noqa
            wx.CallAfter(self._fail, str(e))

    def _done(self, out, intent, rep):
        if self.cancelled:
            return
        self.result = out
        self.go.Enable(); self.stop.Disable()
        self.open.Enable(); self.folder.Enable()
        n = len(intent.get("components", []))
        self.status.set("done", "Done - %d parts, all checks passed" % n)
        self._say(str(rep))
        self.notes.SetLabel(intent.get("notes", ""))
        self.notes.Wrap(self.GetSize().width - 20)
        self.Layout()

    def _fail(self, msg):
        if self.cancelled:
            return
        self.go.Enable(); self.stop.Disable()
        if msg.startswith("HARDWARE:"):
            title, _, text = msg[len("HARDWARE:"):].strip().partition("\n")
            self.status.set("error", title)
            self._say("STOPPED: " + title + ". " + text)
            wx.MessageBox(text, "KODAT PCB - " + title, wx.OK | wx.ICON_WARNING, self)
            return
        self.status.set("error", "Stopped with an error (details below)")
        self._say("ERROR: " + msg)

    def on_stop(self, _evt):
        self.cancelled = True
        self.go.Enable(); self.stop.Disable()
        self.status.set("idle", "Stopped. (The model may finish in the background; its answer is ignored.)")

    def on_open(self, _evt):
        proj = os.path.splitext(self.result)[0] + ".kicad_pro"
        kodat = ai.load_config().get("kodat_dir") or os.path.dirname(HERE)
        launcher = os.path.join(kodat, "KODAT-PCB.cmd")
        exe = os.path.join(os.path.dirname(sys.executable), "kicad.exe")
        if os.path.exists(launcher):
            subprocess.Popen(["cmd", "/c", launcher, proj], creationflags=0x08000000)
        elif os.path.exists(exe):
            subprocess.Popen([exe, proj])
        else:
            os.startfile(proj)


class SettingsPage(wx.Panel):
    def __init__(self, parent, frame):
        super().__init__(parent)
        self.frame = frame
        cfg = ai.load_config()
        g = wx.FlexGridSizer(cols=2, vgap=8, hgap=10)
        g.AddGrowableCol(1)
        self.prov = wx.Choice(self, choices=list(ai.PRESETS))
        self.prov.SetStringSelection(cfg.get("provider", "ollama"))
        self.model = wx.ComboBox(self, value=cfg.get("model", ""))
        self.refresh = wx.Button(self, label="List Ollama models")
        self.url = wx.TextCtrl(self, value=cfg.get("base_url", ""))
        self.key = wx.TextCtrl(self, value=cfg.get("api_key", ""), style=wx.TE_PASSWORD)
        self.kodat = wx.DirPickerCtrl(self, path=cfg.get("kodat_dir", ""), message="KODAT PCB folder (has KODAT-PCB.cmd)")
        mrow = wx.BoxSizer(wx.HORIZONTAL)
        mrow.Add(self.model, 1, wx.RIGHT, 6); mrow.Add(self.refresh, 0)
        for label, ctl in (("Provider", self.prov), ("Model", mrow),
                           ("Base URL (optional)", self.url), ("API key", self.key),
                           ("KODAT PCB folder", self.kodat)):
            g.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            g.Add(ctl, 1, wx.EXPAND)
        hint = wx.StaticText(self, label=(
            "Claude / OpenAI / DeepSeek / Kimi (via Fireworks) need an API key. Ollama runs on this computer, no key\n"
            "(local design needs an NVIDIA GPU with 16 GB+; 24 GB recommended).\n"
            "Free to try: groq (console.groq.com), gemini (aistudio.google.com), openrouter. Custom = any OpenAI-compatible URL.\n"
            "Settings are saved only on this computer: %s" % ai.config_path()))
        self.save = wx.Button(self, label="Save")
        self.test = wx.Button(self, label="Test connection")
        self.hw = wx.Button(self, label="Check this computer")
        self.status = StatusBar(self)
        self.hw.Show(self.prov.GetStringSelection() == "ollama")
        b = wx.BoxSizer(wx.HORIZONTAL)
        b.Add(self.save, 0, wx.RIGHT, 6); b.Add(self.test, 0, wx.RIGHT, 6); b.Add(self.hw, 0)
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(g, 0, wx.EXPAND | wx.ALL, 12)
        s.Add(hint, 0, wx.LEFT | wx.RIGHT, 12)
        s.Add(b, 0, wx.ALL, 12)
        s.Add(self.status, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)
        self.SetSizer(s)
        self.prov.Bind(wx.EVT_CHOICE, self.on_prov)
        self.refresh.Bind(wx.EVT_BUTTON, lambda e: self.list_models())
        self.save.Bind(wx.EVT_BUTTON, lambda e: self.do_save())
        self.test.Bind(wx.EVT_BUTTON, self.on_test)
        self.hw.Bind(wx.EVT_BUTTON, self.on_hw)
        self.on_prov(None)

    def cfg(self):
        c = {"provider": self.prov.GetStringSelection(), "model": self.model.GetValue().strip()}
        if self.url.GetValue().strip():
            c["base_url"] = self.url.GetValue().strip()
        if self.key.GetValue().strip():
            c["api_key"] = self.key.GetValue().strip()
        if self.kodat.GetPath().strip():
            c["kodat_dir"] = self.kodat.GetPath().strip()
        return c

    def on_prov(self, _evt):
        p = self.prov.GetStringSelection()
        is_ollama = p == "ollama"
        self.refresh.Show(is_ollama); self.key.Enable(not is_ollama)
        if hasattr(self, "hw"):
            self.hw.Show(is_ollama)
        if not self.model.GetValue():
            self.model.SetValue(ai.PRESETS[p]["model"])
        if is_ollama:
            self.list_models(quiet=True)
        self.Layout()

    def list_models(self, quiet=False):
        url = self.url.GetValue().strip() or ai.PRESETS["ollama"]["base_url"]
        try:
            names = ollama_models(url)
        except Exception:
            if not quiet:
                self.status.set("error", "Ollama is not running")
            return
        cur = self.model.GetValue()
        self.model.Set(names); self.model.SetValue(cur if cur in names or not names else names[0])
        if not quiet:
            self.status.set("done", "%d models found" % len(names))

    def do_save(self):
        ai.save_config(self.cfg())
        self.frame.design.refresh_who()
        self.status.set("done", "Saved")

    def on_hw(self, _evt):
        c = self.cfg()
        if c["provider"] != "ollama":
            wx.MessageBox("Online providers run on their servers, so this computer's hardware does not matter.",
                          "KODAT PCB - hardware check", wx.OK | wx.ICON_INFORMATION, self)
            return
        from kicad_ai import hardware
        ok, level, title, text = hardware.assess(c.get("base_url") or ai.PRESETS["ollama"]["base_url"], c["model"])
        icon = wx.ICON_INFORMATION if level == "good" else wx.ICON_WARNING
        self.status.set("done" if level == "good" else "error" if not ok else "check", title)
        wx.MessageBox(text, "KODAT PCB - " + title, wx.OK | icon, self)

    def on_test(self, _evt):
        self.do_save()
        self.status.reset_clock()
        self.status.set("busy", "Testing (a local model may need a minute to load)...")
        self.test.Disable()

        def run():
            try:
                r = ai.Provider(ai.load_config()).test().strip()
                wx.CallAfter(self.status.set, "done", "Connected. Model replied: %s" % r[:60])
            except Exception as e:  # noqa
                wx.CallAfter(self.status.set, "error", str(e)[:200])
            wx.CallAfter(self.test.Enable)
        threading.Thread(target=run, daemon=True).start()


class Frame(wx.Frame):
    def __init__(self):
        super().__init__(None, title="KODAT PCB - AI", size=(560, 680))
        nb = wx.Notebook(self)
        self.design = DesignPage(nb, self)
        self.settings = SettingsPage(nb, self)
        nb.AddPage(self.design, "Design")
        nb.AddPage(self.settings, "Settings")
        # dock to the right edge of the screen, full height
        area = wx.Display(0).GetClientArea()
        self.SetSize(wx.Rect(area.x + area.width - 560, area.y, 560, area.height))


def main():
    app = wx.App(False)
    Frame().Show()
    app.MainLoop()


if __name__ == "__main__":
    main()
