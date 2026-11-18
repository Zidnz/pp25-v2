import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

class Cronometro:
    def __init__(self, nombre): self.nombre = nombre
    def __enter__(self): self.t = time.time(); print(f"\n▶ {self.nombre}"); return self
    def __exit__(self, *a): print(f"  ✓ {self.nombre} ({time.time() - self.t:.0f} s)")
