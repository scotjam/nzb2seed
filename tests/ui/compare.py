"""python compare.py classic.json new.json - shows where the two versions differ, word by word."""
import difflib
import json
import sys


def show(a, b, key=""):
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            show(a.get(k), b.get(k), f"{key}.{k}" if key else k)
        return
    if a == b:
        print(f"= {key}")
        return
    print(f"! {key}")
    if isinstance(a, str) and isinstance(b, str):
        for op in difflib.ndiff(a.split(), b.split()):
            if op[0] in "+-":
                print("    " + op)
    else:
        print("    classic:", json.dumps(a)[:1500])
        print("    new:    ", json.dumps(b)[:1500])


c, n = (json.load(open(p, encoding="utf-8")) for p in sys.argv[1:3])
show(c, n)
