"""kicad_ai: describe a circuit as components + nets, get a verified KiCad schematic."""
from .engine import Schematic, load_intent, IntentError
from .library import Library
from .verify import check

__version__ = "0.1.0"


def build(intent, out_path, library=None, kicad_cli=None):
    """Build, write and verify. Returns (schematic, report)."""
    sch = Schematic(intent, library).build()
    sch.write(out_path)
    rep = check(out_path, sch.intent, kicad_cli=kicad_cli)
    for p in sch.problems:
        rep.warnings.append("engine: " + p)
    return sch, rep
