"""Intraday event study inputs: event-window yield moves and 5-minute grids for all 562 TAQ days.  (~3 min)

    .venv/bin/python code/run_intraday.py      -> data/processed/intraday.pkl"""
import pickle
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from yc import intraday as it  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = {"release 8:30": ("08:25", "08:50"), "FOMC statement": ("13:55", "14:25"),
           "FOMC + press conference": ("13:55", "15:30")}


def main():
    ev = pd.read_csv(ROOT / "data/raw/events/event_calendar.csv", parse_dates=["date"])
    ev.loc[ev.date == "2020-03-15", "date"] = pd.Timestamp("2020-03-16")
    days = pd.read_csv(ROOT / "data/raw/events/day_list.csv", parse_dates=["date"])
    rows, grids = [], {}
    for k, r in enumerate(days.itertuples()):
        d = r.date
        types = ev[ev.date == d]
        grids[d] = it.grid_moves(d)
        if r.kind == "control":
            for w, (a, b) in WINDOWS.items():
                rows.append(dict(date=d, kind="control", event="control", window=w, scheduled=True, **it.window_moves(d, a, b)))
        for e in types.itertuples():
            if e.event in ("CPI", "NFP"):
                ws = ["release 8:30"] if e.time_et == "08:30" else []
            else:
                ws = ["FOMC statement", "FOMC + press conference"] if e.time_et == "14:00" else []
            for w in ws:
                a, b = WINDOWS[w]
                rows.append(dict(date=d, kind="event", event=e.event, window=w, scheduled=bool(e.scheduled),
                                 **it.window_moves(d, a, b)))
        if k % 50 == 0:
            print(k, d.date(), flush=True)
    W = pd.DataFrame(rows)
    pickle.dump({"windows": W, "grids": grids, "days": days, "events": ev}, open(ROOT / "data/processed/intraday.pkl", "wb"))
    print(W.groupby(["event", "window"]).size())


if __name__ == "__main__":
    main()
