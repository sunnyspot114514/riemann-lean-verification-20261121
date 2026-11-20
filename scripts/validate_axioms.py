#!/usr/bin/env python3
import json
from pathlib import Path
import re
import sys

names = ["riemannHypothesis", "riemannZeta_eq_zero_re",
         "LFunction_ne_zero_of_half_lt_re", "LFunction_eq_zero_re",
         "LFunction_eq_zero_re_of_isPrimitive"]
allowed = {"propext", "Classical.choice", "Quot.sound"}
text = Path(sys.argv[1]).read_text()
result = {}
for name in names:
    m = re.search(r"'" + re.escape(name) + r"' depends on axioms:\s*\[([^\]]*)\]", text)
    if not m:
        raise SystemExit("Missing or unrecognized axiom report for " + name)
    axioms = [a.strip() for a in m.group(1).split(",") if a.strip()]
    result[name] = axioms
    unexpected = set(axioms) - allowed
    if unexpected:
        raise SystemExit("Unexpected axioms for " + name + ": " + repr(sorted(unexpected)))
print(json.dumps(result, indent=2))
