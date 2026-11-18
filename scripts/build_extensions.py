"""Build native C extensions for meal_planner."""

import os
import subprocess
import sys
import sysconfig
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
C_SRC = ROOT / "meal_planner" / "engine" / "_speedups.c"
OUT_SO = ROOT / "meal_planner" / "engine" / "_speedups.so"


def build():
    if not C_SRC.exists():
        print(f"Error: {C_SRC} not found.", file=sys.stderr)
        return 1
    cmd = [os.environ.get("CC", "gcc"), "-O3", "-shared", "-fPIC", f"-I{sysconfig.get_path('include')}", str(C_SRC), "-o", str(OUT_SO)]
    print(f"Compiling C extension: {' '.join(cmd)}")
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"Compilation failed:\n{res.stderr}", file=sys.stderr)
        return res.returncode
    print(f"Successfully compiled native extension: {OUT_SO}")
    return 0


if __name__ == "__main__":
    sys.exit(build())
