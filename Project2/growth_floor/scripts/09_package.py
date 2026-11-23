#!/usr/bin/env python
"""SPEC 05 §4: build submission/ and deliverables/growth_floor_submission.zip.

submission/
  report.pdf, deck.pdf                      (deliverables/)
  growth_floor/                             code folder = every file git tracks or would track (git ls-files -co
                                            --exclude-standard), i.e. excluding the regenerable large outputs in
                                            .gitignore; DECISIONS.md and README.md sit at its top; data/raw + MANIFEST
  SEEDS.txt                                 seeds from results/report/numbers.json
Usage:  python scripts/09_package.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUB = ROOT / "submission"
ZIP = ROOT / "deliverables" / "growth_floor_submission.zip"


def main():
    for f in ("report.pdf", "deck.pdf"):
        if not (ROOT / "deliverables" / f).exists():
            raise SystemExit(f"missing deliverables/{f}: run scripts/08_build_report.py first")
    if SUB.exists():
        shutil.rmtree(SUB)
    code = SUB / "growth_floor"
    code.mkdir(parents=True)
    files = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True,
                           check=True).stdout.split("\n")
    n = 0
    for rel in files:
        if not rel or rel.startswith("submission/") or rel.endswith(".zip"):
            continue
        src = ROOT / rel
        if src.is_file():
            dst = code / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            n += 1
    for f in ("report.pdf", "deck.pdf"):
        shutil.copy2(ROOT / "deliverables" / f, SUB / f)
    N = json.loads((ROOT / "results/report/numbers.json").read_text())
    s = N["settings"]
    lines = [f"model seed (SPEC 02): {s['model_seed']}", f"Monte Carlo master seed: {s['master_seed']}",
             "per-asset tuning / evaluation seeds = hash(master seed, asset, 'tune' | 'eval'):"]
    lines += [f"  {a}: tune {v['tune']}, eval {v['eval']}" for a, v in N["repro"]["seeds"].items()]
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    lines += [f"code commit at packaging: {head}"]
    (SUB / "SEEDS.txt").write_text("\n".join(lines) + "\n")
    (SUB / "README.txt").write_text(
        "Submission: report.pdf, deck.pdf, growth_floor/ (code, data, DECISIONS.md, README.md), SEEDS.txt.\n"
        "Reproduce: see growth_floor/README.md, section 'Report, deck and submission' (fast path) and 'Run' (full).\n"
        "Data: growth_floor/data/raw/ (Kenneth French Data Library; SHA-256 in data/raw/MANIFEST.json).\n")
    ZIP.parent.mkdir(exist_ok=True)
    if ZIP.exists():
        ZIP.unlink()
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(SUB.rglob("*")):
            if p.is_file():
                z.write(p, Path("growth_floor_submission") / p.relative_to(SUB))
    print(f"{n} code files; wrote {SUB} and {ZIP} ({ZIP.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
