"""Parser for Kenneth French Data Library CSV files.

French CSVs contain a free-text preamble, one or more data sections (each introduced by an
optional title line and a header row whose first field is empty), blank-line separators and a
copyright footer. Values are in percent; missing data are coded as -99.99 or -999.

Public API
----------
list_sections(path)              -> list of section dicts (title, slug, header line number)
read_french(path, section=None)  -> (DataFrame in decimal units, metadata dict)
file_manifest_entry(path)        -> dict for data/raw/MANIFEST.json
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import re
from pathlib import Path

import numpy as np
import pandas as pd

# Missing-value sentinels used by French (percent units, exact match after float parse).
SENTINELS = (-99.99, -999.0)


class FrenchParseError(ValueError):
    """Raised when a French CSV cannot be parsed as expected."""


def _split(line: str) -> list[str]:
    return [f.strip() for f in line.rstrip("\r\n").split(",")]


def _is_header(line: str) -> bool:
    fields = _split(line)
    return (
        len(fields) >= 2
        and fields[0] == ""
        and all(f != "" for f in fields[1:])
        and not all(_is_number(f) for f in fields[1:])
    )


def _is_number(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")


def _read_lines(path: str | Path) -> list[str]:
    # French files are plain ASCII/latin-1; be tolerant of stray bytes.
    with open(path, encoding="latin-1") as fh:
        return fh.readlines()


def list_sections(path: str | Path) -> list[dict]:
    """Locate every data section in a French CSV.

    A section starts at a header row (first field empty, other fields are names). Its title is the
    non-blank line immediately above the header, if any; otherwise ``section_<i>``.
    """
    lines = _read_lines(path)
    sections = []
    for i, line in enumerate(lines):
        if _is_header(line):
            prev = lines[i - 1].strip() if i > 0 else ""
            title = prev if prev and "," not in prev else f"section_{len(sections)}"
            sections.append(
                {"index": len(sections), "title": title, "slug": _slug(title), "header_line": i}
            )
    if not sections:
        raise FrenchParseError(f"{path}: no header row found (expected a line like ',Mkt-RF,SMB,...')")
    return sections


def _select_section(sections: list[dict], section) -> dict:
    if section is None:
        return sections[0]
    if isinstance(section, int):
        try:
            return sections[section]
        except IndexError as e:
            raise FrenchParseError(f"section index {section} out of range ({len(sections)} sections)") from e
    tokens = [t for t in _slug(str(section)).split("_") if t]
    matches = [s for s in sections if all(t in s["slug"].split("_") for t in tokens)]
    if len(matches) != 1:
        raise FrenchParseError(
            f"section {section!r} matched {len(matches)} sections; available: "
            f"{[s['title'] for s in sections]}"
        )
    return matches[0]


def _parse_date(token: str) -> tuple[pd.Timestamp, str]:
    if len(token) == 8:
        return pd.Timestamp(_dt.datetime.strptime(token, "%Y%m%d")), "daily"
    if len(token) == 6:
        return pd.Timestamp(_dt.datetime.strptime(token, "%Y%m")) + pd.offsets.MonthEnd(0), "monthly"
    if len(token) == 4:
        return pd.Timestamp(year=int(token), month=12, day=31), "annual"
    raise FrenchParseError(f"unrecognised date token {token!r}")


def read_french(path: str | Path, section=None) -> tuple[pd.DataFrame, dict]:
    """Parse one section of a French CSV.

    Parameters
    ----------
    path : file path.
    section : None (first section: value-weighted for portfolio files, monthly block for monthly
        factor files), an int index, or a string whose tokens must all appear in the section title
        (e.g. ``"value_weighted"``, ``"equal_weighted"``, ``"annual"``).

    Returns
    -------
    df : DataFrame indexed by date (``DatetimeIndex`` named ``date``), float columns in **decimal**
        units, sentinels replaced by NaN. ``df.attrs["units"] == "decimal"``.
    meta : dict with file, section title, frequency, rows, first/last date, sentinel and NaN counts.
    """
    path = Path(path)
    lines = _read_lines(path)
    sections = list_sections(path)
    sec = _select_section(sections, section)
    columns = _split(lines[sec["header_line"]])[1:]

    dates, rows, freq = [], [], None
    stop_reason = "end_of_file"
    for line in lines[sec["header_line"] + 1 :]:
        if not line.strip():
            stop_reason = "blank_line"
            break
        fields = _split(line)
        if not fields[0].isdigit():
            stop_reason = f"non_numeric_date_field: {fields[0][:40]!r}"
            break
        d, f = _parse_date(fields[0])
        if freq is None:
            freq = f
        elif f != freq:
            raise FrenchParseError(f"{path.name}: mixed date frequencies in section {sec['title']!r}")
        vals = fields[1:]
        if len(vals) != len(columns):
            raise FrenchParseError(
                f"{path.name}: row {fields[0]} has {len(vals)} values, expected {len(columns)}"
            )
        rows.append([float(v) if v != "" else np.nan for v in vals])
        dates.append(d)

    if not rows:
        raise FrenchParseError(f"{path.name}: section {sec['title']!r} has no data rows")

    raw = pd.DataFrame(rows, index=pd.DatetimeIndex(dates, name="date"), columns=columns)
    is_sentinel = raw.isin(SENTINELS)
    sentinel_counts = {c: int(v) for c, v in is_sentinel.sum().items()}
    blank_counts = {c: int(v) for c, v in raw.isna().sum().items()}
    df = raw.mask(is_sentinel) / 100.0
    df.attrs["units"] = "decimal"

    meta = {
        "file": path.name,
        "section_title": sec["title"],
        "section_index": sec["index"],
        "available_sections": [s["title"] for s in sections],
        "frequency": freq,
        "rows": int(len(df)),
        "columns": columns,
        "first_date": df.index[0].strftime("%Y-%m-%d"),
        "last_date": df.index[-1].strftime("%Y-%m-%d"),
        "sentinel_counts": sentinel_counts,
        "blank_field_counts": blank_counts,
        "nan_counts": {c: int(v) for c, v in df.isna().sum().items()},
        "stop_reason": stop_reason,
        "units": "decimal",
    }
    df.attrs["meta"] = meta
    return df, meta


def sha256(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def file_manifest_entry(path: str | Path, previous: dict | None = None) -> dict:
    """Manifest entry for one raw file.

    ``download_date`` keeps a user-entered value from ``previous`` when the hash is unchanged;
    otherwise it defaults to the file modification date (``download_date_source = "file_mtime"``).
    """
    path = Path(path)
    digest = sha256(path)
    mtime = _dt.date.fromtimestamp(path.stat().st_mtime).isoformat()
    entry = {
        "file": path.name,
        "sha256": digest,
        "size_bytes": path.stat().st_size,
        "download_date": mtime,
        "download_date_source": "file_mtime",
        "sections": [],
    }
    if previous and previous.get("sha256") == digest and previous.get("download_date_source") == "user":
        entry["download_date"] = previous["download_date"]
        entry["download_date_source"] = "user"
    for s in list_sections(path):
        df, meta = read_french(path, section=s["index"])
        entry["sections"].append(
            {"title": s["title"], "frequency": meta["frequency"], "rows": meta["rows"],
             "first_date": meta["first_date"], "last_date": meta["last_date"]}
        )
    entry["first_date"] = entry["sections"][0]["first_date"]
    entry["last_date"] = entry["sections"][0]["last_date"]
    return entry
