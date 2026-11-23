"""Deterministic schematic engine.

The AI (or a human) describes *what* the circuit is: components and nets.
This module decides *where* things go and draws wires, power symbols and labels.
It never trusts itself: verify.py re-reads the written file and checks it.
"""
import json
import uuid
from collections import defaultdict, deque

from .library import Library
from .geometry import (snap, rnd, xform, xdir, bbox_xform, bbox_move, bbox_inflate,
                       bbox_overlap, seg_hits_box, on_segment, collinear_overlap, seg_cross)

G = 2.54
STUB = 2.54
POWER_NAMES_GND = ("GND", "GNDA", "GNDD", "GNDPWR", "VSS", "Earth")


class IntentError(Exception):
    pass


# ----------------------------------------------------------------- intent ---
def load_intent(src):
    if isinstance(src, str):
        with open(src, encoding="utf-8") as f:
            src = json.load(f)
    comps = src.get("components") or []
    nets = src.get("nets") or []
    if not comps:
        raise IntentError("no components")
    refs = set()
    for c in comps:
        for k in ("ref", "symbol"):
            if k not in c:
                raise IntentError("component missing '%s': %r" % (k, c))
        if c["ref"] in refs:
            raise IntentError("duplicate ref %s" % c["ref"])
        refs.add(c["ref"])
    used = {}
    for n in nets:
        if "name" not in n or "pins" not in n:
            raise IntentError("net needs name and pins: %r" % n)
        for p in n["pins"]:
            if "." not in p:
                raise IntentError("pin must be REF.PIN: %r" % p)
            ref = p.split(".", 1)[0]
            if ref not in refs:
                raise IntentError("net %s uses unknown ref %s" % (n["name"], ref))
            if p in used:
                raise IntentError("pin %s is in two nets (%s, %s)" % (p, used[p], n["name"]))
            used[p] = n["name"]
    return src


# --------------------------------------------------------------- placement --
class Inst:
    """One placed unit of a component."""
    def __init__(self, comp, lib, unit):
        self.comp, self.lib, self.unit = comp, lib, unit
        self.ref = comp["ref"]
        self.x = self.y = 0.0
        self.rot = 0
        self.mirror = None
        self.pins = [p for p in lib.pins_for_unit(unit) if not (p.hidden and p.etype == "power_in" and False)]
        self.fields = {}

    @property
    def key(self):
        return (self.ref, self.unit)

    def pin_pos(self, pin):
        dx, dy = xform(pin.x, pin.y, self.rot, self.mirror)
        return (rnd(self.x + dx), rnd(self.y + dy))

    def pin_dir(self, pin):
        return xdir(pin.angle, self.rot, self.mirror)

    def body(self):
        return bbox_move(bbox_xform(self.lib.body_bbox(self.unit), self.rot, self.mirror), self.x, self.y)

    def extent(self):
        """Body + pins + stubs: space the part really needs."""
        b = list(self.body())
        for p in self.pins:
            px, py = self.pin_pos(p)
            dx, dy = self.pin_dir(p)
            for (qx, qy) in ((px, py), (px + dx * (STUB + 5.08), py + dy * (STUB + 5.08))):
                b[0] = min(b[0], qx); b[1] = min(b[1], qy); b[2] = max(b[2], qx); b[3] = max(b[3], qy)
        return tuple(b)


class Schematic:
    def __init__(self, intent, library=None):
        self.intent = load_intent(intent)
        self.lib = library or Library()
        self.title = self.intent.get("title", "Untitled")
        self.comps = {c["ref"]: c for c in self.intent["components"]}
        self.libsym = {}
        for ref, c in self.comps.items():
            try:
                self.libsym[ref] = self.lib.get(c["symbol"])
            except KeyError as e:
                raise IntentError(str(e))
        # validate pins exist
        self.pin_net = {}
        for n in self.intent["nets"]:
            for p in n["pins"]:
                ref, num = p.split(".", 1)
                try:
                    num = self.libsym[ref].pin(num).number      # normalise names -> numbers
                except KeyError as e:
                    raise IntentError(str(e).strip("'\""))
                self.pin_net[(ref, num)] = n["name"]
        # rewrite the intent with real pin numbers so the verifier compares like with like
        for n in self.intent["nets"]:
            n["pins"] = ["%s.%s" % (p.split(".", 1)[0], self.libsym[p.split(".", 1)[0]].pin(p.split(".", 1)[1]).number)
                         for p in n["pins"]]
        self.power_nets = {}
        for n in self.intent["nets"]:
            if self._is_power_net(n):
                self.power_nets[n["name"]] = "power:" + n["name"]
        self.insts = []
        self.wires, self.junctions, self.labels = [], [], []
        self.powers, self.noconnects, self.texts = [], [], []
        self.problems = []
        self._pwr_n = 0

    def _is_power_net(self, n):
        if n.get("power") is False:
            return False
        name = n["name"]
        if self.lib.exists("power:" + name):
            return True
        return False

    # ---------------- classification
    def _nets_of(self, ref):
        return {net for (r, _), net in self.pin_net.items() if r == ref}

    def _build_insts(self):
        for ref, c in self.comps.items():
            ls = self.libsym[ref]
            units = sorted({ls.unit_of_pin(num) for (r, num) in self.pin_net if r == ref} - {0}) or [1]
            if ls.unit_count > 1:
                units = list(range(1, ls.unit_count + 1))
            for u in units:
                self.insts.append(Inst(c, ls, u))

    def _inst_nets(self, inst):
        out = {}
        for p in inst.pins:
            net = self.pin_net.get((inst.ref, p.number))
            if net:
                out[p.number] = net
        return out

    def _place(self):
        """Greedy cost-based placement: each part goes where its connections are
        shortest and straightest, without overlapping anything already placed."""
        insts = self.insts
        sig = lambda net: net not in self.power_nets
        nets_by = {i.key: self._inst_nets(i) for i in insts}
        flow = self.intent.get("flow") or []
        fidx = {r: k for k, r in enumerate(flow)}
        is_gnd = lambda n: bool(n) and (n in POWER_NAMES_GND or n.upper().startswith("GND"))

        def big(i):
            return len(i.pins) >= 3 and i.ref[0] not in "JP"

        # orientation options
        def options(i):
            if len(i.pins) == 2:
                return [(r, None) for r in (0, 90, 180, 270)]
            return [(0, None), (0, "y")] if not big(i) else [(0, None)]

        # placement order: big parts by flow/degree, then BFS by connectivity
        def degree(i):
            return sum(1 for n in nets_by[i.key].values() if sig(n))
        bigs = sorted([i for i in insts if big(i)],
                      key=lambda i: (fidx.get(i.ref, 999), -degree(i), i.ref))
        order = []
        placed = []

        _tcache = {}

        def placed_terms(net):
            if net in _tcache:
                return _tcache[net]
            out = []
            for j in placed:
                for p in j.pins:
                    if self.pin_net.get((j.ref, p.number)) == net:
                        pp = j.pin_pos(p); d = j.pin_dir(p)
                        out.append(((pp[0] + d[0] * STUB, pp[1] + d[1] * STUB), d, j))
            _tcache[net] = out
            return out

        def ext(i):
            e = bbox_inflate(i.extent(), 1.27)
            if len(i.pins) == 2:
                b = i.body()
                if abs(i.pin_pos(i.pins[0])[0] - i.pin_pos(i.pins[1])[0]) < 0.01:   # vertical: text on the right
                    e = (e[0], e[1], max(e[2], b[2] + 8.89), e[3])
                else:                                                           # horizontal: text above/below
                    e = (e[0], min(e[1], b[1] - 5.08), e[2], max(e[3], b[3] + 5.08))
            return e

        def routable(se, t, boxes):
            # can an L or Z path join the two stub ends without crossing a body?
            paths = [[se, (t[0], se[1]), t], [se, (se[0], t[1]), t]]
            mx, my = snap((se[0] + t[0]) / 2, G), snap((se[1] + t[1]) / 2, G)
            paths += [[se, (mx, se[1]), (mx, t[1]), t], [se, (se[0], my), (t[0], my), t]]
            for path in paths:
                if all(not any(seg_hits_box(a, b, bx) for bx in boxes) for a, b in zip(path, path[1:]) if a != b):
                    return True
            return False

        def cost(i):
            c = 0.0
            nets = nets_by[i.key]
            power_only = nets and all(not sig(n) for n in nets.values())
            for p in i.pins:
                net = nets.get(p.number)
                if not net:
                    continue
                if not sig(net) and not power_only:
                    continue
                pp = i.pin_pos(p); d = i.pin_dir(p)
                se = (pp[0] + d[0] * STUB, pp[1] + d[1] * STUB)
                terms = placed_terms(net)
                if not terms:
                    continue
                best = 1e9
                if "_boxes" not in _ecache:
                    _ecache["_boxes"] = [bbox_inflate(k.body(), 0.3) for k in placed]
                boxes = _ecache["_boxes"] + [bbox_inflate(i.body(), 0.3)]
                for (t, td, j) in terms:
                    dist = abs(t[0] - se[0]) + abs(t[1] - se[1])
                    if abs(t[0] - se[0]) < 0.01 or abs(t[1] - se[1]) < 0.01:
                        dist -= 4          # straight wire possible
                    # pins should face each other
                    vx, vy = t[0] - se[0], t[1] - se[1]
                    if d[0] * vx + d[1] * vy < -0.01:
                        dist += 10
                    if td[0] * -vx + td[1] * -vy < -0.01:
                        dist += 10
                    if not routable(se, t, boxes):
                        dist += 40
                    best = min(best, dist)
                c += best * (0.5 if power_only else 1.0)
            # orientation preferences for 2-pin parts
            if len(i.pins) == 2:
                p1, p2 = i.pins
                n1, n2 = nets.get(p1.number), nets.get(p2.number)
                y1, y2 = i.pin_pos(p1)[1], i.pin_pos(p2)[1]
                horiz = abs(y1 - y2) < 0.01
                if n1 and n2 and sig(n1) and sig(n2):
                    c += 0 if horiz else 6
                else:
                    if horiz:
                        c += 40
                    if is_gnd(n1) and y1 < y2: c += 40
                    if is_gnd(n2) and y2 < y1: c += 40
                    if n1 and not sig(n1) and not is_gnd(n1) and y1 > y2: c += 15
                    if n2 and not sig(n2) and not is_gnd(n2) and y2 > y1: c += 15
                if i.lib.lib_id.endswith(("R", "C", "C_Polarized", "L")) and i.rot in (180, 270):
                    c += 0.5               # mild preference for canonical rotation
            # flow order: left to right
            if i.ref in fidx:
                for j in placed:
                    if j.ref in fidx and j.ref != i.ref:
                        if fidx[i.ref] > fidx[j.ref] and i.x < j.x - 0.01:
                            c += 25
                        if fidx[i.ref] < fidx[j.ref] and i.x > j.x + 0.01:
                            c += 25
            return c

        _ecache = {}

        def fits(i):
            e = ext(i)
            for j in placed:
                if j.key not in _ecache:
                    _ecache[j.key] = ext(j)
                if bbox_overlap(e, _ecache[j.key]):
                    return False
            return True

        remaining = list(insts)
        while remaining:
            if not placed:
                first = bigs[0] if bigs else sorted(remaining, key=lambda i: -degree(i))[0]
                first.x, first.y = 139.7, 99.06
                placed.append(first); remaining.remove(first)
                continue
            # next part: most signal connections to placed parts (bigs first)
            def score(i):
                links = 0
                for n in nets_by[i.key].values():
                    if sig(n) and placed_terms(n):
                        links += 1
                po = bool(nets_by[i.key]) and all(not sig(n) for n in nets_by[i.key].values())
                return (-(links + (5 if big(i) and links else 0)), po, fidx.get(i.ref, 999), i.ref)
            nxt = sorted(remaining, key=score)[0]
            # candidate window
            xs = [ext(j) for j in placed]
            X0 = min(e[0] for e in xs) - 40; X1 = max(e[2] for e in xs) + 40
            Y0 = min(e[1] for e in xs) - 40; Y1 = max(e[3] for e in xs) + 40
            best = None
            for (rot, mir) in options(nxt):
                nxt.rot, nxt.mirror = rot, mir
                x = snap(X0, G)
                while x <= X1:
                    y = snap(Y0, G)
                    while y <= Y1:
                        nxt.x, nxt.y = x, y
                        if fits(nxt):
                            c = cost(nxt) + 0.02 * (abs(x - 139.7) + abs(y - 99.06))
                            if best is None or c < best[0]:
                                best = (c, x, y, rot, mir)
                        y = rnd(y + G)
                    x = rnd(x + G)
            _, nxt.x, nxt.y, nxt.rot, nxt.mirror = best
            placed.append(nxt); remaining.remove(nxt)
            _tcache.clear(); _ecache.pop("_boxes", None)

    # ---------------- routing
    def _obstacles(self):
        if getattr(self, "_obs_cache", None) is None:
            obs = [bbox_inflate(i.body(), 0.3) for i in self.insts]
            pins = set()
            for i in self.insts:
                for p in i.pins:
                    a = i.pin_pos(p); d = i.pin_dir(p)
                    e = (rnd(a[0] - d[0] * p.length), rnd(a[1] - d[1] * p.length))
                    a2 = (a[0] - d[0] * 0.6, a[1] - d[1] * 0.6)
                    # the pin's drawn line (not its connectable end)
                    obs.append(bbox_inflate((min(a2[0], e[0]), min(a2[1], e[1]), max(a2[0], e[0]), max(a2[1], e[1])), 0.3)
                               if p.length > 0.6 else (0, 0, 0, 0))
                    pins.add(a)
            self._obs_cache = obs
            self._pin_pts = pins
        return self._obs_cache

    def _point_owner(self):
        """Every connectable point -> net name ('' = no net)."""
        owner = {}
        for i in self.insts:
            for p in i.pins:
                owner.setdefault(i.pin_pos(p), set()).add(self.pin_net.get((i.ref, p.number), "#NC:%s.%s" % (i.ref, p.number)))
        for (a, b, net) in self.wires:
            owner.setdefault(a, set()).add(net); owner.setdefault(b, set()).add(net)
        return owner

    def _seg_ok(self, a, b, net, allow_pts=()):
        if a == b:
            return True
        for box in self._obstacles():
            if seg_hits_box(a, b, box):
                return False
        owner = self._point_owner()
        for pt, nets in owner.items():
            if pt in allow_pts:
                continue
            if on_segment(pt, a, b, strict=False):
                # touching any foreign point, or any own point that is not an endpoint
                if nets - {net} or (pt not in (a, b)):
                    return False
        for (p, q, n2) in self.wires:
            if collinear_overlap((a, b), (p, q)):
                return False
            if n2 != net and (on_segment(a, p, q) or on_segment(b, p, q)):
                return False          # would short into another net's wire
            if n2 != net and (p in self._pin_pts or q in self._pin_pts) and seg_cross((a, b), (p, q)):
                return False          # never cross a pin's stub: too easy to misread
            if n2 == net:
                # own wire endpoints may not land on own wire interior (would need junction)
                if on_segment(a, p, q) or on_segment(b, p, q) or on_segment(p, a, b) or on_segment(q, a, b):
                    return False
        for (pt, _txt, _ang) in self.labels:
            if on_segment(pt, a, b, strict=False) and pt not in (a, b):
                return False
        for pw in self.powers:
            for box in [pw["box"]]:
                if seg_hits_box(a, b, box):
                    return False
        return True

    def _path_ok(self, pts, net, allow=()):
        segs = list(zip(pts, pts[1:]))
        ok = all(self._seg_ok(a, b, net, allow) for a, b in segs)
        return ok

    def _add_path(self, pts, net):
        clean = [pts[0]]
        for p in pts[1:]:
            if p != clean[-1]:
                clean.append(p)
        # merge collinear
        merged = [clean[0]]
        for k in range(1, len(clean)):
            if len(merged) >= 2 and ((merged[-2][0] == merged[-1][0] == clean[k][0]) or (merged[-2][1] == merged[-1][1] == clean[k][1])):
                merged[-1] = clean[k]
            else:
                merged.append(clean[k])
        for a, b in zip(merged, merged[1:]):
            self.wires.append((a, b, net))

    def _stub(self, inst, pin, length=STUB):
        p = inst.pin_pos(pin)
        d = inst.pin_dir(pin)
        return p, (rnd(p[0] + d[0] * length), rnd(p[1] + d[1] * length)), d

    def _power_symbol(self, net, at, d):
        lib_id = self.power_nets[net]
        ls = self.lib.get(lib_id)
        # default graphic direction: GND-like points down, supplies point up
        bb = ls.body_bbox(1)
        default = (0, 1) if abs(bb[1]) > abs(bb[3]) else (0, -1)
        for rot in (0, 90, 180, 270):
            if xform(default[0], -default[1], rot) == (d[0], d[1]) or \
               (xform(default[0], -default[1], rot)[0] == d[0] and xform(default[0], -default[1], rot)[1] == d[1]):
                break
        box = bbox_move(bbox_xform(bb, rot), at[0], at[1])
        gbox = box
        w = len(net) * 1.15 + 0.6
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        if rot in (0, 180):
            if cy < at[1]:
                box = (min(box[0], cx - w / 2), box[1] - 2.6, max(box[2], cx + w / 2), box[3])
            else:
                box = (min(box[0], cx - w / 2), box[1], max(box[2], cx + w / 2), box[3] + 2.6)
        else:
            if cx < at[0]:
                box = (box[0] - 1.6 - w, min(box[1], cy - 1), box[2], max(box[3], cy + 1))
            else:
                box = (box[0], min(box[1], cy - 1), box[2] + 1.6 + w, max(box[3], cy + 1))
        self._pwr_n += 1
        self.powers.append(dict(lib_id=lib_id, net=net, at=at, rot=rot, box=box, gbox=gbox,
                                ref="#PWR%03d" % self._pwr_n))
        return box

    def _route(self):
        # 1) no-connect flags
        for i in self.insts:
            for p in i.pins:
                if (i.ref, p.number) not in self.pin_net and not p.hidden:
                    self.noconnects.append(i.pin_pos(p))
        # 2) power pins: stub + symbol pointing away from the part
        for i in self.insts:
            for p in i.pins:
                net = self.pin_net.get((i.ref, p.number))
                if net in self.power_nets:
                    for L in (STUB, STUB * 2, STUB * 3, STUB * 4):
                        a, b, d = self._stub(i, p, L)
                        box_try = self._probe_power_box(net, b, d)
                        if self._seg_ok(a, b, net) and not any(bbox_overlap(box_try, bbox_inflate(o.body(), 0.3)) for o in self.insts) \
                                and not any(bbox_overlap(box_try, pw["box"]) for pw in self.powers):
                            self.wires.append((a, b, net))
                            self._power_symbol(net, b, d)
                            break
                    else:
                        a, b, d = self._stub(i, p, STUB)
                        self.wires.append((a, b, net))
                        self._power_symbol(net, b, d)
                        self.problems.append("power symbol for %s.%s may overlap something" % (i.ref, p.number))
        # 3) signal nets
        terms = defaultdict(list)
        for i in self.insts:
            for p in i.pins:
                net = self.pin_net.get((i.ref, p.number))
                if net and net not in self.power_nets:
                    terms[net].append((i, p))
        self._stub_ok = {}
        for net in terms:
            for (i, p) in terms[net]:
                a, b, d = self._stub(i, p)
                if self._seg_ok(a, b, net):
                    self.wires.append((a, b, net))
                    self._stub_ok[(i.key, p.number)] = True
        for net in sorted(terms, key=lambda n: len(terms[n])):
            self._route_net(net, terms[net])

    def _probe_power_box(self, net, at, d):
        ls = self.lib.get(self.power_nets[net])
        bb = ls.body_bbox(1)
        default = (0, 1) if abs(bb[1]) > abs(bb[3]) else (0, -1)
        for rot in (0, 90, 180, 270):
            if xform(default[0], -default[1], rot) == (d[0], d[1]):
                break
        box = bbox_move(bbox_xform(bb, rot), at[0], at[1])
        w = len(net) * 1.15 + 0.6
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        if rot in (0, 180):
            box = (min(box[0], cx - w / 2), box[1] - (2.6 if cy < at[1] else 0), max(box[2], cx + w / 2), box[3] + (0 if cy < at[1] else 2.6))
        else:
            box = (box[0] - (1.6 + w if cx < at[0] else 0), min(box[1], cy - 0.9), box[2] + (0 if cx < at[0] else 1.6 + w), max(box[3], cy + 0.9))
        return bbox_inflate(box, 0.1)

    def _route_net(self, net, members):
        if len(members) == 1:
            i, p = members[0]
            self.problems.append("net %s has a single pin (%s.%s)" % (net, i.ref, p.number))
        stubs = []
        for (i, p) in members:
            a, b, d = self._stub(i, p)
            stubs.append((a, b, d, i, p))
        stubs.sort(key=lambda s: (s[1][0], s[1][1]))
        pin_pts = {st[0] for st in stubs}
        trees = []            # each: dict(verts=[...], anchor=(pt, dir))
        for (a, b, d, i, p) in stubs:
            if not self._stub_ok.get((i.key, p.number)):
                trees.append(dict(verts=[], anchor=(a, d), pinonly=True))
                continue
            done = False
            for t in sorted(trees, key=lambda t: min([abs(v[0] - b[0]) + abs(v[1] - b[1]) for v in t["verts"]] or [1e9])):
                if t.get("pinonly"):
                    continue
                got = self._try_connect(net, b, d, t["verts"], pin_pts)
                if got:
                    t["verts"] += [a, b] + got
                    done = True
                    break
            if not done:
                trees.append(dict(verts=[a, b], anchor=(b, d)))
        # second pass: join separate trees with a wire where possible
        merged = True
        while merged and len(trees) > 1:
            merged = False
            for ti in range(len(trees)):
                for tj in range(len(trees)):
                    if ti == tj or trees[ti].get("pinonly") or trees[tj].get("pinonly"):
                        continue
                    A, B = trees[ti], trees[tj]
                    for vb in [v for v in B["verts"] if v not in pin_pts]:
                        got = self._try_connect(net, vb, None, A["verts"], pin_pts, allow_start=True)
                        if got:
                            A["verts"] += B["verts"] + got
                            trees.pop(tj)
                            merged = True
                            break
                    if merged:
                        break
                if merged:
                    break
        if len(trees) > 1:
            for t in trees:
                if t.get("pinonly"):
                    pt, d = t["anchor"]
                    self.labels.append((pt, net, self._label_angle(d)))
                    continue
                self._place_label(net, t, pin_pts)
        pts = defaultdict(int)
        for (x, y, n) in self.wires:
            if n == net:
                pts[x] += 1; pts[y] += 1
        for pt, k in pts.items():
            if k >= 3 and pt not in self.junctions:
                self.junctions.append(pt)

    def _label_box(self, pt, txt, ang):
        w = len(txt) * 1.15 + 1.0
        return {0: (pt[0], pt[1] - 1.9, pt[0] + w, pt[1] + 0.3), 180: (pt[0] - w, pt[1] - 1.9, pt[0], pt[1] + 0.3),
                90: (pt[0] - 1.9, pt[1] - w, pt[0] + 0.3, pt[1]), 270: (pt[0] - 1.9, pt[1], pt[0] + 0.3, pt[1] + w)}[ang]

    def _label_free(self, box, skip=()):
        for o in self._obstacles():
            if bbox_overlap(box, o):
                return False
        for (p, q, n) in self.wires:
            if (p, q) in skip:
                continue
            if seg_hits_box(p, q, box):
                return False
        for pw in self.powers:
            if bbox_overlap(box, pw["box"]):
                return False
        for (pt, txt, ang) in self.labels:
            if bbox_overlap(box, self._label_box(pt, txt, ang)):
                return False
        return True

    def _place_label(self, net, tree, pin_pts):
        # 1) a dangling stub end whose direction is free
        anchor, d = tree["anchor"]
        cands = []
        for v in tree["verts"]:
            if v in pin_pts:
                continue
            for dd in ((1, 0), (-1, 0), (0, -1), (0, 1)):
                for L in (2.54, 5.08):
                    e = (rnd(v[0] + dd[0] * L), rnd(v[1] + dd[1] * L))
                    cost = abs(v[0] - anchor[0]) + abs(v[1] - anchor[1]) + L + (3 if dd[1] else 0)
                    cands.append((cost, v, e, dd))
        cands.sort(key=lambda c: c[0])
        for (_c, v, e, dd) in cands:
            ang = self._label_angle(dd)
            box = self._label_box(e, net, ang)
            if self._seg_ok(v, e, net) and self._label_free(box):
                self.wires.append((v, e, net))
                self.labels.append((e, net, ang))
                return
        self.labels.append((anchor, net, self._label_angle(d)))

    def _try_connect(self, net, b, d, verts, pin_pts, allow_start=False):
        """Find a wire path from point b to any vertex of a tree. Adds it and returns
        the new vertices, or returns None."""
        verts = sorted({v for v in verts if v not in pin_pts and v != b},
                       key=lambda v: (abs(v[0] - b[0]) + abs(v[1] - b[1]), v))
        options = []
        for v in verts[:8]:
            L = abs(v[0] - b[0]) + abs(v[1] - b[1])
            options.append((L, [b, (v[0], b[1]), v], v, None))
            options.append((L, [b, (b[0], v[1]), v], v, None))
            for k in (0.5, 0.25, 0.75):
                mx = snap(b[0] + (v[0] - b[0]) * k, G); my = snap(b[1] + (v[1] - b[1]) * k, G)
                options.append((L + 1, [b, (mx, b[1]), (mx, v[1]), v], v, None))
                options.append((L + 1, [b, (b[0], my), (v[0], my), v], v, None))
        if d is not None:
            for v in verts[:4]:
                L = abs(v[0] - b[0]) + abs(v[1] - b[1])
                for k in (1, 2, 3, 4):
                    e = (rnd(b[0] + d[0] * G * k), rnd(b[1] + d[1] * G * k))
                    options.append((L + 6 + 2 * k, [b, e, (v[0], e[1]), v], v, None))
                    options.append((L + 6 + 2 * k, [b, e, (e[0], v[1]), v], v, None))
                    for side in (-1, 1):
                        for m in (2, 4):
                            if d[0] == 0:
                                sx = rnd(v[0] + side * G * m)
                                options.append((L + 12 + 2 * k + m, [b, e, (sx, e[1]), (sx, v[1]), v], v, None))
                            else:
                                sy = rnd(v[1] + side * G * m)
                                options.append((L + 12 + 2 * k + m, [b, e, (e[0], sy), (v[0], sy), v], v, None))
        vset = set(verts)
        for (p_, q_, n_) in list(self.wires):
            if n_ != net or not (p_ in vset or q_ in vset):
                continue
            foot = (p_[0], b[1]) if p_[0] == q_[0] else (b[0], p_[1])
            if on_segment(foot, p_, q_) and abs(foot[0] - snap(foot[0], G)) < 1e-6 and abs(foot[1] - snap(foot[1], G)) < 1e-6:
                L = abs(foot[0] - b[0]) + abs(foot[1] - b[1])
                options.append((L + 0.5, [b, foot], foot, (p_, q_)))
        options.sort(key=lambda o: o[0])
        allow_extra = (b,) if allow_start else ()
        for (_L, path, v, split) in options:
            if split is not None:
                p_, q_ = split
                self.wires.remove((p_, q_, net))
                self.wires += [(p_, v, net), (v, q_, net)]
            if self._path_ok(path, net, allow=(v,) + allow_extra):
                self._add_path(path, net)
                return path[1:]
            if split is not None:
                self.wires.remove((p_, v, net)); self.wires.remove((v, q_, net))
                self.wires.append((p_, q_, net))
        return None

    @staticmethod
    def _label_angle(d):
        return {(1, 0): 0, (-1, 0): 180, (0, -1): 90, (0, 1): 270}.get(tuple(d), 0)

    # ---------------- power flags
    def _power_flags(self):
        """Each power net that nothing drives gets a PWR_FLAG in a corner."""
        drivers = defaultdict(bool)
        for (ref, num), net in self.pin_net.items():
            et = self.libsym[ref].pin(num).etype
            if et in ("power_out",):
                drivers[net] = True
        ex = [i.extent() for i in self.insts]
        x = snap(min(e[0] for e in ex), G)
        y0 = snap(min(e[1] for e in ex) - 15.24, G)
        for net in sorted(self.power_nets):
            if drivers[net]:
                continue
            at = (x, y0)
            gndish = net.upper().startswith("GND") or net in POWER_NAMES_GND
            sym_at = (x, rnd(y0 + 2.54)) if gndish else (x, rnd(y0 - 2.54))
            self.wires.append((at, sym_at, net))
            self._power_symbol(net, sym_at, (0, 1) if gndish else (0, -1))
            self.wires.append((at, (rnd(x + 5.08), y0), net))
            self.flags = getattr(self, "flags", []) + [((rnd(x + 5.08), y0), net)]
            x += 17.78

    # ---------------- fields
    def _place_fields(self):
        boxes = [bbox_inflate(i.body(), 0.2) for i in self.insts] + [pw["box"] for pw in self.powers]
        for pw in self.powers:
            b_ = pw["box"]; w = len(pw["net"]) * 1.15 + 1
            cx_, cy_ = (b_[0] + b_[2]) / 2, (b_[1] + b_[3]) / 2
            if pw["rot"] in (0, 180):
                ty = b_[1] - 1.27 if cy_ < pw["at"][1] else b_[3] + 1.27
                boxes.append((cx_ - w / 2, ty - 1, cx_ + w / 2, ty + 1))
            else:
                if cx_ < pw["at"][0]:
                    boxes.append((b_[0] - 1.27 - w, cy_ - 1, b_[0], cy_ + 1))
                else:
                    boxes.append((b_[2], cy_ - 1, b_[2] + 1.27 + w, cy_ + 1))
        taken = []
        pin_lines = []
        for i in self.insts:
            for p in i.pins:
                a = i.pin_pos(p); d = i.pin_dir(p)
                e_ = (rnd(a[0] - d[0] * p.length), rnd(a[1] - d[1] * p.length))
                boxes.append(bbox_inflate((min(a[0], e_[0]), min(a[1], e_[1]), max(a[0], e_[0]), max(a[1], e_[1])), 1.4))
        for (pt, txt, ang) in self.labels:
            w = len(txt) * 1.15 + 1
            lb = {0: (pt[0], pt[1] - 1.8, pt[0] + w, pt[1] + 0.2), 180: (pt[0] - w, pt[1] - 1.8, pt[0], pt[1] + 0.2),
                  90: (pt[0] - 1.8, pt[1] - w, pt[0] + 0.2, pt[1]), 270: (pt[0] - 1.8, pt[1], pt[0] + 0.2, pt[1] + w)}[ang]
            boxes.append(lb)
        def text_box(x, y, txt, just):
            w = max(1, len(txt)) * 1.15 + 0.5; h = 1.6
            if just == "left":
                return (x, y - h / 2, x + w, y + h / 2)
            if just == "right":
                return (x - w, y - h / 2, x, y + h / 2)
            return (x - w / 2, y - h / 2, x + w / 2, y + h / 2)
        def free(b):
            if any(bbox_overlap(b, o) for o in boxes + taken):
                return False
            for (p, q, _n) in self.wires + pin_lines:
                if seg_hits_box(p, q, b):
                    return False
            for (pt, txt, ang) in self.labels:
                if b[0] - 3 < pt[0] < b[2] + 3 and b[1] - 2 < pt[1] < b[3] + 2:
                    return False
            return True
        for i in self.insts:
            b = i.body()
            ref = i.ref
            val = i.comp.get("value", i.lib.lib_id.split(":")[1])
            cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
            cands = [
                ("left", (b[2] + 1.27, cy - 1.27), (b[2] + 1.27, cy + 1.27)),
                ("center", (cx, b[1] - 3.81), (cx, b[1] - 1.27)),
                ("center", (cx, b[3] + 1.9), (cx, b[3] + 4.44)),
                ("right", (b[0] - 1.27, cy - 1.27), (b[0] - 1.27, cy + 1.27)),
                ("left", (b[2] + 1.27, b[1] - 3.81), (b[2] + 1.27, b[1] - 1.27)),
                ("left", (b[2] + 1.27, b[3] + 1.9), (b[2] + 1.27, b[3] + 4.44)),
                ("right", (b[0] - 1.27, b[1] - 3.81), (b[0] - 1.27, b[1] - 1.27)),
                ("right", (b[0] - 1.27, b[3] + 1.9), (b[0] - 1.27, b[3] + 4.44)),
            ]
            if i.rot in (90, 270):
                cands = [c for c in cands if c[0] == "center"] + [
                    ("center", (cx, b[1] - 6.35), (cx, b[1] - 3.81)),
                    ("center", (cx, b[3] + 4.44), (cx, b[3] + 6.98))]
            if i.lib.unit_count > 1:
                ref = ref + chr(ord("A") + i.unit - 1)
            chosen = None
            for just, rp, vp in cands:
                rb, vb = text_box(*rp, ref, just), text_box(*vp, val, just)
                if free(rb) and free(vb):
                    chosen = (just, rp, vp, rb, vb); break
            if chosen is None:
                just, rp, vp = cands[0]
                chosen = (just, rp, vp, text_box(*rp, ref, just), text_box(*vp, val, just))
                self.problems.append("text of %s may overlap something" % i.ref)
            just, rp, vp, rb, vb = chosen
            taken += [rb, vb]
            i.fields = dict(just=just, ref=(snap(rp[0]), snap(rp[1])), val=(snap(vp[0]), snap(vp[1])))

    # ---------------- build
    def _recenter(self):
        xs, ys = [], []
        for i in self.insts:
            e = i.extent(); xs += [e[0], e[2]]; ys += [e[1], e[3]]
        for (a, b, n) in self.wires:
            xs += [a[0], b[0]]; ys += [a[1], b[1]]
        for pw in self.powers:
            xs += [pw["box"][0], pw["box"][2]]; ys += [pw["box"][1], pw["box"][3]]
        cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        dx, dy = snap(148.59 - cx, G), snap(95.25 - cy, G)
        if max(ys) - min(ys) > 150 or max(xs) - min(xs) > 250:
            self.problems.append("drawing is larger than an A4 sheet")
        mv = lambda p: (rnd(p[0] + dx), rnd(p[1] + dy))
        mb = lambda b: (rnd(b[0] + dx), rnd(b[1] + dy), rnd(b[2] + dx), rnd(b[3] + dy))
        for i in self.insts:
            i.x, i.y = rnd(i.x + dx), rnd(i.y + dy)
            if i.fields:
                i.fields["ref"] = mv(i.fields["ref"]); i.fields["val"] = mv(i.fields["val"])
        self.wires = [(mv(a), mv(b), n) for (a, b, n) in self.wires]
        self.junctions = [mv(j) for j in self.junctions]
        self.noconnects = [mv(j) for j in self.noconnects]
        self.labels = [(mv(p), n, a) for (p, n, a) in self.labels]
        for pw in self.powers:
            pw["at"] = mv(pw["at"]); pw["box"] = mb(pw["box"]); pw["gbox"] = mb(pw["gbox"])
        self.flags = [(mv(p), n) for (p, n) in getattr(self, "flags", [])]

    def build(self):
        self._build_insts()
        self._place()
        self._power_flags()
        self._route()
        self._place_fields()
        self._recenter()
        return self

    # ---------------- write
    def write(self, path, project=None):
        from .writer import write_sch
        write_sch(self, path, project)
        return path
