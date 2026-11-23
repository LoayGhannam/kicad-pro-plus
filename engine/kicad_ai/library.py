"""Symbol library access: load .kicad_sym files, flatten derived symbols,
extract pins and body bounding boxes, and produce embeddable lib_symbols."""
import copy
import os
import glob
try:
    import sexpdata
except ImportError:  # KiCad's bundled Python: use the vendored copy
    from ._vendor import sexpdata
S = sexpdata.Symbol


def _is(node, tag):
    return isinstance(node, list) and node and node[0] == S(tag)


def _child(node, tag):
    for e in node:
        if _is(e, tag):
            return e
    return None


def _children(node, tag):
    return [e for e in node if _is(e, tag)]


def default_symbol_dirs():
    """Where stock KiCad symbol libraries usually live."""
    dirs = []
    for k, v in os.environ.items():
        if k.startswith("KICAD") and k.endswith("SYMBOL_DIR") and v:
            dirs.append(v)
    dirs += [
        "/usr/share/kicad/symbols",
        "/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols",
    ]
    dirs += sorted(glob.glob(r"C:\Program Files\KiCad\*\share\kicad\symbols"), reverse=True)
    dirs += sorted(glob.glob(r"D:\programs\kicad\share\kicad\symbols"), reverse=True)
    return [d for d in dirs if os.path.isdir(d)]


class Pin:
    __slots__ = ("number", "name", "etype", "x", "y", "angle", "length", "unit", "hidden")

    def __init__(self, number, name, etype, x, y, angle, length, unit, hidden):
        self.number, self.name, self.etype = number, name, etype
        self.x, self.y, self.angle, self.length = x, y, angle, length
        self.unit, self.hidden = unit, hidden

    def __repr__(self):
        return "Pin(%s %s %s u%d @%s,%s a%s)" % (self.number, self.name, self.etype, self.unit, self.x, self.y, self.angle)


class LibSymbol:
    def __init__(self, lib_id, sexpr):
        self.lib_id = lib_id
        self.sexpr = sexpr            # flattened, top name == lib_id
        self.pins = []
        self.unit_count = 1
        self.is_power = _child(sexpr, "power") is not None or S("power") in sexpr
        self.bbox = {}                # unit -> (x0,y0,x1,y1) in lib coords (y up)
        self.default_footprint = ""
        self.datasheet = "~"
        self._parse()

    def _parse(self):
        name = self.lib_id.split(":")[1]
        units = set()
        for prop in _children(self.sexpr, "property"):
            if prop[1] == "Footprint":
                self.default_footprint = prop[2]
            if prop[1] == "Datasheet":
                self.datasheet = prop[2] or "~"
        for sub in _children(self.sexpr, "symbol"):
            sname = sub[1]
            # sub-symbol name: <name>_<unit>_<style>
            try:
                unit, style = map(int, sname.rsplit("_", 2)[-2:])
            except ValueError:
                continue
            if style == 2:            # De Morgan alternate body: ignore
                continue
            if unit:
                units.add(unit)
            for g in sub:
                if not isinstance(g, list) or not g:
                    continue
                tag = g[0]
                if tag == S("pin"):
                    at = _child(g, "at")
                    num = str(_child(g, "number")[1])
                    nm = str(_child(g, "name")[1])
                    ln = _child(g, "length")
                    hd = _child(g, "hide")          # KiCad 8+: (hide yes)
                    hidden = S("hide") in g or (hd is not None and len(hd) > 1 and str(hd[1]) == "yes")
                    self.pins.append(Pin(num, nm, str(g[1]), float(at[1]), float(at[2]),
                                         float(at[3]) if len(at) > 3 else 0.0,
                                         float(ln[1]) if ln else 0.0, unit, hidden))
                    # pin line counts toward body extents only by its inner end
                    self._grow(unit, *self._pin_inner(g))
                elif tag in (S("rectangle"),):
                    s, e = _child(g, "start"), _child(g, "end")
                    self._grow(unit, s[1], s[2]); self._grow(unit, e[1], e[2])
                elif tag in (S("polyline"), S("bezier")):
                    for xy in _child(g, "pts")[1:]:
                        self._grow(unit, xy[1], xy[2])
                elif tag == S("circle"):
                    c, r = _child(g, "center"), _child(g, "radius")
                    self._grow(unit, c[1] - r[1], c[2] - r[1]); self._grow(unit, c[1] + r[1], c[2] + r[1])
                elif tag == S("arc"):
                    for k in ("start", "mid", "end"):
                        p = _child(g, k)
                        if p: self._grow(unit, p[1], p[2])
        self.unit_count = max(units) if units else 1
        # unit 0 = common graphics: merge into every unit
        if 0 in self.bbox:
            for u in range(1, self.unit_count + 1):
                b0 = self.bbox[0]
                self._grow(u, b0[0], b0[1]); self._grow(u, b0[2], b0[3])
        # pins must start outside the body box: clip decorative graphics
        # (e.g. LED light arrows) that would otherwise cover a pin's end
        import math
        for u, b in list(self.bbox.items()):
            x0, y0, x1, y1 = b
            for p in self.pins:
                if p.unit not in (0, u) and u != 0:
                    continue
                if x0 < p.x < x1 and y0 < p.y < y1:
                    a = math.radians(p.angle)
                    ix, iy = p.x + p.length * round(math.cos(a)), p.y + p.length * round(math.sin(a))
                    c = round(math.cos(a)); s_ = round(math.sin(a))
                    if c == 1: x0 = max(x0, ix)
                    elif c == -1: x1 = min(x1, ix)
                    elif s_ == 1: y0 = max(y0, iy)
                    elif s_ == -1: y1 = min(y1, iy)
            self.bbox[u] = (x0, y0, x1, y1)

    @staticmethod
    def _pin_inner(g):
        import math
        at = _child(g, "at"); ln = _child(g, "length")
        a = math.radians(float(at[3]) if len(at) > 3 else 0)
        L = float(ln[1]) if ln else 0
        return float(at[1]) + L * math.cos(a), float(at[2]) + L * math.sin(a)

    def _grow(self, unit, x, y):
        x, y = float(x), float(y)
        b = self.bbox.get(unit)
        if b is None:
            self.bbox[unit] = (x, y, x, y)
        else:
            self.bbox[unit] = (min(b[0], x), min(b[1], y), max(b[2], x), max(b[3], y))

    def pins_for_unit(self, unit):
        return [p for p in self.pins if p.unit in (0, unit)]

    def unit_of_pin(self, number):
        for p in self.pins:
            if p.number == number:
                return p.unit
        raise KeyError("%s has no pin %s" % (self.lib_id, number))

    def pin(self, number):
        for p in self.pins:
            if p.number == number:
                return p
        # allow pins by name too (e.g. 'Shield', 'VCC') when the name is unique
        named = [p for p in self.pins if p.name.lower() == str(number).lower()]
        if len(named) == 1:
            return named[0]
        have = ", ".join("%s(%s)" % (p.number, p.name) for p in self.pins)
        raise KeyError("%s has no pin '%s'. Pins are: %s" % (self.lib_id, number, have))

    def body_bbox(self, unit):
        return self.bbox.get(unit) or self.bbox.get(0) or (-1.27, -1.27, 1.27, 1.27)

    def embed(self):
        return sexpdata.dumps(self.sexpr)


class Library:
    def __init__(self, dirs=None):
        self.dirs = dirs or default_symbol_dirs()
        if not self.dirs:
            raise RuntimeError("No KiCad symbol library directory found; pass dirs=[...]")
        self._files = {}
        self._cache = {}

    def _load(self, lib):
        if lib not in self._files:
            for d in self.dirs:
                p = os.path.join(d, lib + ".kicad_sym")
                if os.path.exists(p):
                    with open(p, encoding="utf-8") as f:
                        self._files[lib] = sexpdata.loads(f.read())
                    break
            else:
                raise KeyError("Symbol library '%s' not found in %s" % (lib, self.dirs))
        return self._files[lib]

    def _raw(self, lib, name):
        for e in self._load(lib)[1:]:
            if _is(e, "symbol") and e[1] == name:
                return e
        raise KeyError("Symbol '%s' not found in library '%s'" % (name, lib))

    def get(self, lib_id):
        if lib_id in self._cache:
            return self._cache[lib_id]
        lib, name = lib_id.split(":", 1)
        raw = copy.deepcopy(self._raw(lib, name))
        ext = _child(raw, "extends")
        if ext is not None:
            parent = copy.deepcopy(self._raw(lib, ext[1]))
            pname = parent[1]
            # take parent's graphics/pins, child's properties
            child_props = {p[1]: p for p in _children(raw, "property")}
            flat = [S("symbol"), name]
            for e in parent[2:]:
                if _is(e, "property"):
                    flat.append(child_props.pop(e[1], e))
                elif _is(e, "symbol"):
                    e[1] = e[1].replace(pname + "_", name + "_", 1)
                    flat.append(e)
                else:
                    flat.append(e)
            flat.extend(child_props.values())
            raw = flat
        raw[1] = lib_id
        sym = LibSymbol(lib_id, raw)
        self._cache[lib_id] = sym
        return sym

    def exists(self, lib_id):
        try:
            self.get(lib_id)
            return True
        except KeyError:
            return False
