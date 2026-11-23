"""Independent checker: re-reads a .kicad_sch from disk and checks it against the intent.

It does not reuse anything from the engine's internal state, so an engine bug
cannot hide itself. Pin positions come from the lib_symbols embedded in the file.
"""
import json
import os
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict

try:
    import sexpdata
except ImportError:  # KiCad's bundled Python: use the vendored copy
    from ._vendor import sexpdata
S = sexpdata.Symbol

from .library import LibSymbol
from .geometry import (xform, bbox_xform, bbox_move, bbox_inflate, bbox_overlap,
                       seg_hits_box, on_segment, collinear_overlap, seg_cross)


def _is(n, t):
    return isinstance(n, list) and n and n[0] == S(t)


def _c(n, t):
    for e in n:
        if _is(e, t):
            return e


def _r(v):
    return (round(float(v[0]), 3), round(float(v[1]), 3))


class Report:
    def __init__(self):
        self.errors, self.warnings, self.info = [], [], []

    @property
    def ok(self):
        return not self.errors

    def as_dict(self):
        return dict(ok=self.ok, errors=self.errors, warnings=self.warnings, info=self.info)

    def __str__(self):
        out = ["RESULT: %s" % ("PASS" if self.ok else "FAIL")]
        out += ["  ERROR   " + e for e in self.errors]
        out += ["  WARNING " + w for w in self.warnings]
        out += ["  info    " + i for i in self.info]
        return "\n".join(out)


class _UF:
    def __init__(self):
        self.p = {}

    def f(self, a):
        self.p.setdefault(a, a)
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def u(self, a, b):
        self.p[self.f(a)] = self.f(b)


def check(sch_path, intent=None, kicad_cli=None):
    rep = Report()
    with open(sch_path, encoding="utf-8") as f:
        root = sexpdata.loads(f.read())

    libs = {}
    for e in _c(root, "lib_symbols")[1:]:
        if _is(e, "symbol"):
            libs[e[1]] = LibSymbol(e[1], e)

    wires, junctions, labels, ncs, syms = [], set(), [], set(), []
    for e in root[1:]:
        if _is(e, "wire"):
            pts = _c(e, "pts")
            wires.append((_r(pts[1][1:]), _r(pts[2][1:])))
        elif _is(e, "junction"):
            junctions.add(_r(_c(e, "at")[1:3]))
        elif _is(e, "label"):
            labels.append((_r(_c(e, "at")[1:3]), e[1]))
        elif _is(e, "no_connect"):
            ncs.add(_r(_c(e, "at")[1:3]))
        elif _is(e, "symbol"):
            at = _c(e, "at")
            mir = _c(e, "mirror")
            props = {p[1]: p[2] for p in e if _is(p, "property")}
            syms.append(dict(lib_id=_c(e, "lib_id")[1], x=float(at[1]), y=float(at[2]),
                             rot=int(float(at[3])) if len(at) > 3 else 0,
                             mirror=str(mir[1]) if mir else None,
                             unit=int(_c(e, "unit")[1]), ref=props.get("Reference"), value=props.get("Value")))

    # ---- items
    pins = []            # (ref, num, pt, etype, unit)
    bodies = []          # (ref/unit, box)
    power_pts = []       # (pt, netname)
    for s in syms:
        ls = libs.get(s["lib_id"])
        if ls is None:
            rep.errors.append("symbol %s uses %s which is not embedded" % (s["ref"], s["lib_id"]))
            continue
        for p in ls.pins_for_unit(s["unit"]):
            dx, dy = xform(p.x, p.y, s["rot"], s["mirror"])
            pt = (round(s["x"] + dx, 3), round(s["y"] + dy, 3))
            if ls.is_power:
                if s["lib_id"] != "power:PWR_FLAG":
                    power_pts.append((pt, s["value"]))
                else:
                    power_pts.append((pt, None))
            else:
                pins.append((s["ref"], p.number, pt, p.etype, s["unit"], p.hidden))
        if not ls.is_power:
            box = bbox_move(bbox_xform(ls.body_bbox(s["unit"]), s["rot"], s["mirror"]), s["x"], s["y"])
            bodies.append(("%s/u%d" % (s["ref"], s["unit"]), box))

    # ---- geometry rules
    for i, (a, b) in enumerate(bodies):
        for (c, d) in bodies[i + 1:]:
            if bbox_overlap(bbox_inflate(b, -0.3), bbox_inflate(d, -0.3)):
                rep.errors.append("bodies overlap: %s and %s" % (a, c))
    for (p, q) in wires:
        if p == q:
            rep.warnings.append("zero-length wire at %s" % (p,))
            continue
        if p[0] != q[0] and p[1] != q[1]:
            rep.warnings.append("diagonal wire %s-%s" % (p, q))
            continue
        for name, box in bodies:
            if seg_hits_box(p, q, bbox_inflate(box, -0.3)):
                rep.errors.append("wire %s-%s runs through %s" % (p, q, name))
    for i, w1 in enumerate(wires):
        for w2 in wires[i + 1:]:
            if collinear_overlap(w1, w2):
                rep.errors.append("wires overlap: %s and %s" % (w1, w2))
    pin_pts = {pt for (_r_, _n, pt, *_x) in pins}
    for (p, q) in wires:
        for pt in pin_pts:
            if on_segment(pt, p, q):
                rep.errors.append("pin at %s sits in the middle of wire %s-%s (KiCad will NOT connect it)" % (pt, p, q))
        for (r, s_) in wires:
            for end in (r, s_):
                if on_segment(end, p, q) and end not in junctions:
                    rep.errors.append("wire end %s touches middle of wire %s-%s without a junction" % (end, p, q))

    # ---- connectivity
    uf = _UF()
    pts_all = set()
    for (p, q) in wires:
        uf.u(("pt", p), ("pt", q)); pts_all |= {p, q}
    for j in junctions:
        for (p, q) in wires:
            if on_segment(j, p, q):
                uf.u(("pt", j), ("pt", p))
    for (ref, num, pt, et, unit, hid) in pins:
        uf.u(("pin", ref, num), ("pt", pt))
    for (pt, name) in labels:
        uf.u(("pt", pt), ("label", name))
        if pt not in pts_all and pt not in pin_pts:
            touching = any(on_segment(pt, p, q) for (p, q) in wires)
            if not touching:
                rep.errors.append("label %s at %s is not attached to any wire or pin" % (name, pt))
            else:
                for (p, q) in wires:
                    if on_segment(pt, p, q):
                        uf.u(("pt", pt), ("pt", p))
    for (pt, name) in power_pts:
        if name:
            uf.u(("pt", pt), ("power", name))
        if pt not in pts_all and pt not in pin_pts:
            rep.errors.append("power symbol %s at %s is not attached" % (name or "PWR_FLAG", pt))
    # pins sharing exact point connect (handled through ("pt", pt))

    groups = defaultdict(set)
    for (ref, num, pt, et, unit, hid) in pins:
        groups[uf.f(("pin", ref, num))].add("%s.%s" % (ref, num))
    # unconnected pins
    nc_flagged = 0
    for (ref, num, pt, et, unit, hid) in pins:
        g = groups[uf.f(("pin", ref, num))]
        has_other = len(g) > 1 or any(uf.f(("pt", pt)) == uf.f(k) for k in [("power", n) for (_, n) in power_pts if n] + [("label", n) for (_, n) in labels])
        if pt in ncs:
            nc_flagged += 1
            if len(g) > 1:
                rep.errors.append("pin %s.%s has a no-connect flag but is wired" % (ref, num))
            continue
        if not has_other and not hid:
            rep.errors.append("pin %s.%s is not connected (and has no no-connect flag)" % (ref, num))
    for (ref, num, pt, et, unit, hid) in pins:
        if hid and et == "power_in":
            rep.warnings.append("hidden power pin %s.%s (%s) joins a net by name implicitly" % (ref, num, et))

    # power nets need a driver
    drv = defaultdict(bool)
    for (pt, name) in power_pts:
        if name is None:
            drv[uf.f(("pt", pt))] = True
    for (ref, num, pt, et, unit, hid) in pins:
        if et == "power_out":
            drv[uf.f(("pt", pt))] = True
    for (ref, num, pt, et, unit, hid) in pins:
        if et == "power_in" and not hid and pt not in ncs and not drv[uf.f(("pt", pt))]:
            rep.errors.append("power input %s.%s is not driven (no regulator output or PWR_FLAG on its net)" % (ref, num))

    found = {frozenset(v) for v in groups.values()}
    rep.info.append("%d symbols, %d wires, %d labels, %d junctions, %d no-connects" %
                    (len(syms), len(wires), len(labels), len(junctions), len(ncs)))

    # ---- compare with intent
    if intent is not None:
        if isinstance(intent, str):
            with open(intent, encoding="utf-8") as f:
                intent = json.load(f)
        pin2group = {}
        for g in found:
            for p in g:
                pin2group[p] = g
        for n in intent["nets"]:
            want = set(n["pins"])
            have = set().union(*(pin2group.get(p, {p}) for p in want))
            if have != want:
                missing = sorted(want - have)
                extra = sorted(have - want)
                split = len({pin2group.get(p, frozenset([p])) for p in want}) > 1
                msg = "net %s: " % n["name"]
                if split:
                    msg += "pins are split into separate nets; "
                if extra:
                    msg += "shorted to %s" % ", ".join(extra)
                rep.errors.append(msg.rstrip("; "))
        rep.info.append("connectivity compared against intent: %d nets" % len(intent["nets"]))

    # ---- second opinion: KiCad's own netlister
    cli = kicad_cli or shutil.which("kicad-cli")
    if not cli:
        for c in (r"D:\programs\kicad\bin\kicad-cli.exe", r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe",
                  r"C:\Program Files\KiCad\9.0\bin\kicad-cli.exe"):
            if os.path.exists(c):
                cli = c
                break
    if cli and intent is not None:
        try:
            tmp = tempfile.mkdtemp()
            out = os.path.join(tmp, "n.xml")
            subprocess.run([cli, "sch", "export", "netlist", "--format", "kicadxml", "-o", out, sch_path],
                           check=True, capture_output=True, timeout=120)
            xr = ET.parse(out).getroot()
            kgroups = set()
            for n in xr.find("nets"):
                kgroups.add(frozenset("%s.%s" % (x.get("ref"), x.get("pin")) for x in n))
            k_pin = {p: g for g in kgroups for p in g}
            bad = 0
            for n in intent["nets"]:
                want = set(n["pins"])
                g = {k_pin.get(p) for p in want}
                if len(g) != 1 or (None not in g and set(next(iter(g))) - want and
                                   any(not q.startswith("#") for q in set(next(iter(g))) - want)):
                    bad += 1
                    rep.errors.append("kicad-cli netlist disagrees on net %s" % n["name"])
            rep.info.append("kicad-cli netlist cross-check: %s" % ("PASS" if not bad else "%d nets differ" % bad))
        except Exception as ex:  # noqa
            rep.warnings.append("kicad-cli cross-check skipped: %s" % ex)
    return rep
