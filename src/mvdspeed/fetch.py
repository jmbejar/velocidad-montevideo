"""Download the monthly open-data files, enumerated through CKAN.

Run by hand, not on app start:

    uv run mvdspeed-fetch --list                      # what the catalogue offers
    uv run mvdspeed-fetch --only 2022-03,2024-03,2026-03   # the identity gate
    uv run mvdspeed-fetch                             # everything, ~15 GB
    uv run mvdspeed-fetch --from 2022-01              # skip the COVID year

The whole history is 68 monthly resources, January 2021 onward, and about 15 GB
as published. That is a long unattended download over a public endpoint, so this
is restartable by design: a file whose size on disk already matches the size the
catalogue reports for it is skipped, and a partial download is written to a
`.part` file and only renamed once it is complete. Interrupting and re-running
costs one file, not the run.

Files are kept exactly as published -- a `.zip` stays zipped. See the note beside
DATASET_SLUG in config.py for why.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import pandas as pd

from mvdspeed.config import CKAN_API, DATA_RAW, DATASET_SLUG, MONTHS_ES

TIMEOUT_S = 120
USER_AGENT = "mvdspeed/0.1"

# Month-name spellings the catalogue actually uses, which is not the same as the
# month names we display. The feed writes September as "Setiembre" -- the standard
# Rioplatense spelling -- for 2021 through 2024, then switches to "Septiembre"
# from 2025. Accepting only the dictionary spelling silently dropped four
# Septembers out of a five-year panel, which would have left September absent
# from four of six years and quietly corrupted any seasonal term fitted on it.
#
# Hence also the unparsed-title count that `resources()` prints: the next
# spelling change should be loud, not another four missing months.
_MONTH_ALIASES = {
    "setiembre": 9,
    **{name.lower(): i + 1 for i, name in enumerate(MONTHS_ES)},
}

# "Velocidad promedio - Marzo 2024", with separator and spacing varying across
# eras. Anchored on the month name so a renamed prefix does not break it. Longest
# alternatives first, so "Setiembre" cannot be shadowed by a shorter prefix match.
_TITLE_MONTH = re.compile(
    r"("
    + "|".join(sorted(_MONTH_ALIASES, key=len, reverse=True))
    + r")\s*(?:de\s*)?(\d{4})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FetchReport:
    downloaded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    bytes_written: int = 0

    def summarise(self) -> str:
        lines = [
            f"{len(self.downloaded)} downloaded "
            f"({self.bytes_written / 1e9:.1f} GB), "
            f"{len(self.skipped)} already present"
        ]
        if self.unavailable:
            lines.append(
                f"  {len(self.unavailable)} unavailable: "
                + ", ".join(self.unavailable)
            )
        return "\n".join(lines)


def month_from_title(title: str) -> str | None:
    """`"Velocidad promedio - Marzo 2024"` -> `"2024-03"`, or None."""
    match = _TITLE_MONTH.search(title)
    if not match:
        return None
    name, year = match.groups()
    return f"{int(year):04d}-{_MONTH_ALIASES[name.lower()]:02d}"


def resources(*, slug: str = DATASET_SLUG) -> pd.DataFrame:
    """Every monthly resource in the catalogue, newest last.

    Columns: ym, title, url, size, is_zip. `size` is the catalogue's own figure
    and is NA for resources that serve no file -- those are kept in the frame so
    they can be reported as unavailable rather than silently vanishing from the
    month list.
    """
    request = urllib.request.Request(
        f"{CKAN_API}?id={slug}", headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        raise RuntimeError(
            f"could not read the catalogue at {CKAN_API} ({type(exc).__name__}: "
            f"{exc}). This is the only step that needs the network; once the "
            f"files are under {DATA_RAW} nothing else does."
        ) from exc

    if not payload.get("success"):
        raise RuntimeError(f"CKAN reported failure for {slug!r}")

    rows, unparsed = [], []
    for item in payload["result"]["resources"]:
        title = item.get("name") or ""
        ym = month_from_title(title)
        if ym is None:
            # The dataset carries one metadata.txt with no month in its name;
            # anything else here is a spelling this parser does not know, and it
            # would cost a month of the panel. See _MONTH_ALIASES.
            if item.get("format", "").upper() != "TXT":
                unparsed.append(title)
            continue
        url = item["url"]
        rows.append(
            {
                "ym": ym,
                "title": item["name"],
                "url": url,
                "size": item.get("size"),
                "is_zip": url.lower().endswith(".zip"),
            }
        )

    if unparsed:
        # Loud on purpose. A month this parser cannot name is a month missing
        # from the panel, and the last time that happened it was four Septembers.
        raise SystemExit(
            f"{len(unparsed)} resource title(s) carry no month this parser "
            "recognises, so they would be dropped from the panel silently. Add "
            "the spelling to _MONTH_ALIASES in fetch.py:\n  "
            + "\n  ".join(repr(t) for t in unparsed)
        )

    frame = pd.DataFrame(rows).sort_values("ym", ignore_index=True)
    duplicated = frame["ym"].duplicated(keep=False)
    if duplicated.any():
        # Not fatal, but it decides which file a month is ingested from, so it
        # has to be visible rather than resolved by sort order.
        print(
            "  warning: more than one resource claims the same month:\n"
            + frame.loc[duplicated, ["ym", "title"]].to_string(index=False),
            file=sys.stderr,
        )
    return frame


def local_path(row: pd.Series, *, dest: Path = DATA_RAW) -> Path:
    """Where a resource lands, named by month so the ETL can glob in order."""
    suffix = ".zip" if row["is_zip"] else ".csv"
    return dest / f"velocidad_{row['ym']}{suffix}"


def _download_one(url: str, target: Path) -> int:
    """Stream to `<target>.part`, then rename. Returns bytes written."""
    partial = target.with_suffix(target.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        with partial.open("wb") as handle:
            shutil.copyfileobj(response, handle, length=1 << 20)
    written = partial.stat().st_size
    if written == 0:
        partial.unlink()
        raise urllib.error.URLError("served an empty file")
    partial.rename(target)
    return written


def download(
    rows: pd.DataFrame, *, dest: Path = DATA_RAW, force: bool = False
) -> FetchReport:
    """Fetch each resource, skipping any whose size on disk already matches."""
    dest.mkdir(parents=True, exist_ok=True)
    downloaded, skipped, unavailable, written = [], [], [], 0

    for _, row in rows.iterrows():
        target = local_path(row, dest=dest)
        expected = row["size"]
        if target.exists() and not force:
            actual = target.stat().st_size
            if pd.isna(expected) or actual == expected:
                skipped.append(row["ym"])
                continue
            print(
                f"  {row['ym']}: on disk {actual:,} B but catalogue says "
                f"{int(expected):,} B -- refetching",
                file=sys.stderr,
            )

        size_note = "unknown size" if pd.isna(expected) else f"{expected / 1e6:.0f} MB"
        print(f"  {row['ym']}: {size_note} ...", end="", flush=True, file=sys.stderr)
        try:
            got = _download_one(row["url"], target)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # Report and carry on rather than failing the run: a 15 GB download
            # should not be lost to one bad resource, and the ETL works from
            # whatever months are on disk. Every month the catalogue lists does
            # serve a file today, so anything landing here is worth reading.
            print(f" unavailable ({type(exc).__name__})", file=sys.stderr)
            unavailable.append(row["ym"])
            continue
        print(f" {got / 1e6:.0f} MB", file=sys.stderr)
        downloaded.append(row["ym"])
        written += got

    return FetchReport(
        downloaded=downloaded,
        skipped=skipped,
        unavailable=unavailable,
        bytes_written=written,
    )


def select(
    frame: pd.DataFrame,
    *,
    only: Sequence[str] | None = None,
    since: str | None = None,
) -> pd.DataFrame:
    if only:
        wanted = set(only)
        missing = wanted - set(frame["ym"])
        if missing:
            raise SystemExit(
                f"no such month(s) in the catalogue: {', '.join(sorted(missing))}"
            )
        frame = frame[frame["ym"].isin(wanted)]
    if since:
        frame = frame[frame["ym"] >= since]
    return frame.reset_index(drop=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        help="comma-separated months to fetch, e.g. 2022-03,2024-03,2026-03",
    )
    parser.add_argument("--from", dest="since", help="earliest month, e.g. 2022-01")
    parser.add_argument(
        "--force", action="store_true", help="redownload files already on disk"
    )
    parser.add_argument(
        "--list", action="store_true", help="print the catalogue and exit"
    )
    args = parser.parse_args(argv)

    catalogue = resources()
    print(
        f"{len(catalogue)} monthly resources, {catalogue['ym'].iloc[0]} to "
        f"{catalogue['ym'].iloc[-1]}",
        file=sys.stderr,
    )

    if args.list:
        listing = catalogue.assign(
            size_mb=lambda f: (f["size"] / 1e6).round(0),
            on_disk=lambda f: [local_path(r).exists() for _, r in f.iterrows()],
        )
        print(listing[["ym", "size_mb", "is_zip", "on_disk", "title"]].to_string(index=False))
        return 0

    wanted = select(
        catalogue,
        only=args.only.split(",") if args.only else None,
        since=args.since,
    )
    total = wanted["size"].sum(skipna=True) / 1e9
    print(f"fetching {len(wanted)} month(s), about {total:.1f} GB", file=sys.stderr)

    report = download(wanted, force=args.force)
    print(report.summarise(), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
