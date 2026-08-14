# Multi-year history, trend decomposition, and a partial-month nowcast

Working plan for the `multi-year-panel` branch. Written 13 Aug 2026; status updated
14 Aug 2026.

## Where this stands

| Phase | State |
|---|---|
| 0 — Acquire the history | **Done**, commit `271f4d2`. `mvdspeed-fetch` + first tests in the repo. |
| 1 — Identity gate | **Run, and it failed as designed.** Phase 3 rewritten below on the evidence. |
| 2 — Multi-month ETL | Not started. Next. Must handle the three file formats found in Phase 1. |
| 3 — Site identity | Not started; **design changed** — two mechanisms, see Phase 3. |
| 4 — Windowed references | Not started. |
| 5 — Panel and model | Not started. |
| 6 — Validation | Not started. Runs *before* any app work. |
| 7 — Artifacts | Not started. |
| 8 — The app | Not started. |

On disk but gitignored, so re-fetch with `uv run mvdspeed-fetch --only …` if lost:
`data/raw/velocidad_{2021-03,2025-05}.csv` and `velocidad_2025-06.zip` (0.8 GB), which are
the three months the identity gate was measured on.

### Open decisions, both needing a call before the phase that hits them

**1. The deployed app now reads committed parquet, and Phase 2's output is far too big for
that.** `main` gained `51c99ef "Commit the aggregated parquet so the deployed app has data"`,
which force-adds `data/processed/measurements.parquet` (1.9 MB) and `detectors.parquet` past
the gitignore rule. Phase 2 replaces the first with a hive-partitioned directory and adds a
site-grain `app_panel.parquet` at roughly 190 MB — rewritten every time a month publishes.
Three ways out:

- Shrink it: zstd at int32/int8/float32 probably lands under 100 MB, and `daytype` grain
  instead of `dow` would roughly third it — at the cost of the per-`dow` counts that every
  composition claim in this feature rests on. Not preferred.
- **Commit only recent months for the deployment** (say the last 12) and keep the full panel
  local. The multi-year views need only the ~1.5 MB fitted artifacts, and the map shows one
  month at a time regardless. **Recommended.**
- Fetch the panel at deploy time, which breaks the repo's deliberate "no network dependency
  but the basemap" rule.

**2. `MIN_TREND_MONTHS = 12` is still a round number.** It needs its evidence written out —
how many sites it withholds, and what the fitted slopes look like either side of the line — or
it does not go in `config.py` at all. Same treatment `SURFACE_ALPHA_REF` got in `2748c9c`.

## Context

The dashboard currently reads one month (Aug 1–11 2026) and every "typical" reference in it
is a lifetime aggregate over that single file. The goal is to use the full published
history to (a) find out how city speeds actually changed across the years and (b) estimate
the present.

**A premise correction found while planning.** The README says "A new monthly CSV appears
once the month closes." That is not what the feed does. Verified against the CKAN API
(`catalogodatos.gub.uy/api/3/action/package_show`):

- 68 monthly resources, Jan 2021 → Aug 2026.
- Since early 2026 publication is clockwork on the 1st: March finalised Apr 1, April →
  May 1, May → Jun 1, June → Jul 1, July → Aug 1.
- **The running month is published in progress.** The Aug 2026 resource was last modified
  2026-08-13 at 46 MB against a ~114 MB full-month steady state, and holds Aug 1–11. The
  real publication lag is about two days.

So "predict a month we cannot see" mostly evaporates, and what replaces it is better posed:
**the current month is a fragment, and a fragment is not comparable to a full month.** In
the live data Wed/Thu/Fri each appear once while Sat/Sun appear twice, and weekend days run
~2.5 km/h faster than weekdays. Measured consequence:

| Scope | Naive mean | Day-balanced | Bias |
|---|---|---|---|
| All days | 30.701 | 30.360 | **+0.341 km/h** |
| Weekdays only (the app default) | 29.546 | 29.433 | +0.113 km/h |

That is small in absolute terms — but a plausible city trend is 1–3% per year, so a 1.1%
composition artefact is the same order as the entire signal we are trying to measure. The
correction is the feature.

## What the numbers say the deliverable has to be

A subagent measured the noise floor by splitting one month's weekdays in two: **2.82 km/h
MAE** per detector×bucket (2.50 weighted at site×bucket), 7.3% median relative deviation,
on cells with a median of 42 readings. That is pure sampling noise — no trend, no horizon.

A per-sensor forward nowcast therefore cannot beat a per-(sensor, bucket, day-type)
historical mean by much. The aggregate index averages ~40,000 cells and its standard error
falls by √N, so **the signal is in the index and the trend, not in per-sensor prediction.**

Agreed scope: build the pipeline and the trend decomposition as the primary deliverable;
ship the composition-corrected current-month estimate; gate the forward per-sensor nowcast
on a pre-registered backtest margin.

## Decisions taken

- **Model: weighted fixed-effects decomposition, numpy only.** No new dependency, ~10 s for
  the full panel (benchmarked on a full-size synthetic panel: 9.4 s / 30 sweeps / 231 MB,
  converged by sweep 5). Its coefficients *are* the insight deliverable, which gradient
  boosting cannot emit — and with no exogenous features in the feed (no weather, no
  incidents, no roadworks) there is nothing for a tree ensemble to discover beyond the
  detector × hour × day-type interaction this design already saturates.
- **2021 is ingested but excluded from the fit.** `ANALYSIS_START = "2022-01"`. Uruguay's
  restrictions ran roughly March–June 2021, so those months are not exchangeable with 2026.
  Keeping them costs ~4 GB and buys the best available calibration for how large a real
  city-wide signal has to be before it is believable, plus one genuinely good chart.
  Reversible with `--from`.
- **Forward predictions never go on the map.** Extrapolation lives only in the trend chart,
  dashed, with an empirical residual band. See "Three epistemic statuses" below.

## Verified ground truth (all measured, not assumed)

| Fact | Value |
|---|---|
| `cod_detector` → geometry tuple, within a month | 998 codes, exactly 1 tuple each, 0 violations |
| `cod_detector` → `id_carril` | 1 lane each — the code *is* the lane |
| Exact duplicate `(cod_detector, fecha, hora)` rows | **434** (0.014%), currently double-counted by `etl.py` |
| Full month | ~8.9 M rows, ~785 MB CSV; ×68 ≈ 51 GB |
| Download volume (CSV era + zip era) | ~15 GB compressed |
| Placeholder coord `-34.890561,-56.220631` | 50 detectors, 13 street names, 5.0% of rows |
| Site nearest-neighbour distance | median 78.9 m, p5 **4.0 m**, min **0.0 m** |
| Mirrored opposite-direction site pairs | **57**, of which **4 at 0.0 m separation** |
| Speed gap in the 0.0 m mirrored pairs | 36.5 vs 43.6 · 23.9 vs 30.2 · 33.9 vs 36.4 km/h |
| Distinct sites sharing a *normalised* street triple | 20 triples / 40 sites, spread 0.1 m → 9.2 km |
| Worst collision | `Bv Artigas: Cufre → Gral Flores`, two sites **0.1 m apart**, **20.4 vs 41.3 km/h**, disjoint detector codes |
| Weekday zero share after the dead-lane filter | 1.71% |
| `(detector, bucket)` weekday occupancy | 41,503 / 41,808 = **99%** |
| Eight resources with null `size` | 2023-07…12, 2025-07, 2025-08 — **all eight serve files**; the null is unpopulated CKAN metadata, verified by range request |
| Months catalogued / available / in the fit | **68** resources (Jan 2021 → Aug 2026), no gaps; **68** with a file; **56** from `2022-01` |
| September's spelling in the feed | **"Setiembre"** 2021-2024, "Septiembre" from 2025 — a dictionary-only parser drops four Septembers silently |

## Two traps found while planning

**1. `st.cache_data` never hashed `_key`, and one character makes it a wrong-month bug.**
Arguments whose names start with `_` are excluded from Streamlit's cache key. So
`CACHE_KEY` (`app.py:246`, passed as `_key` at 182-237) contributes *nothing* to invalidation
today — the row-count fingerprint is documentation, not a mechanism — and `_metric_name` is
doubly inert since `column` already discriminates. Harmless so far, because `load_data()` is
a `cache_resource` that never reloads. But if the month follows the file's local convention
and arrives as `_ym`, **every month collides onto one cache entry and the map silently shows
another month's data.** Fix in the same commit: rename `_key` → `key`, drop `_metric_name`,
and pass `ym` as a plain argument. Comment it at the wrapper, not just in the commit message.

**2. The day-level panel does not fit in a session.** `measurements.parquet` is 450,106 rows
for 11 days and 46.8 MB deep in pandas (the `date` object column dominates). A full month is
~1.2 M rows; 54 months is ~66 M rows and ~2 GB downcast, held for the session by
`@st.cache_resource`. So the app cannot be served from it. The app reads a site-grain
`app_panel.parquet` at **`(site_id, ym, dow, bucket)` → `speed_sum, n_moving, n_zero`** —
~10 M rows, ~190 MB at int32/int8/float32 and sparse in practice — plus
`day_counts.parquet` at `(ym, dow) → n_days`, and keeps day-level `measurements` for the
newest month only, the only view that needs a date.

**`dow` grain, not `daytype` grain**, for two reasons: `DAY_SCOPES` splits the weekend into
Sat and Sun, so a `daytype` panel could not serve the existing day-scope radio; and the
per-`dow` day counts are the *receipt* for every composition claim in the feature — the
balanced mode, the partial-month caption, and the "days this month holds" table all read
from them. The model's own `panel.parquet` stays at detector grain (Phase 5).

## Phase 0 — Acquire the history

New `src/mvdspeed/fetch.py`, CLI `mvdspeed-fetch`.

- Enumerate via CKAN `package_show` on the dataset slug — **not** by scraping the HTML page,
  and not by guessing URLs. Filenames are inconsistent across eras
  (`autoscope_01_2021_velocidad.csv` vs `velocidad_promedio_julio_2026.zip`) and the
  `format` field is unreliable (July 2026 is labelled `CSV` with a `.zip` URL; the distinct
  format values include `csv zip`). **Key off the URL extension.**
- Derive the month from the CKAN resource *name* (`Velocidad promedio - Marzo 2024`), then
  verify against the modal `fecha` in the file itself and refuse on mismatch.
- Resume by comparing local size to the resource `size`; `--only 2022-03,2024-03,2026-03`
  for the Phase 1 gate; `--force` to redownload. Skip and report the two null-size
  resources rather than failing the run.

```python
def resources(*, slug: str = DATASET_SLUG) -> pd.DataFrame   # ym, name, url, size, is_zip
def download(rows, *, dest: Path = DATA_RAW, force: bool = False) -> FetchReport
def main(argv: list[str] | None = None) -> int
```

## Phase 1 — Identity gate — **FAILED as designed; Phase 3 needs rework**

`cod_detector` is clean *within* Aug 2026, but it does **not** persist across the history.
Measured by range-probing the published files:

| Month | `cod_detector` shape | sample |
|---|---|---|
| 2022-03 … 2025-03 | **3 digits** | `103, 104, 106, 107, 108` |
| 2025-08 … 2026-08 | **5–7 digits** | `1001001, 1001002, 1001003, 1001201` |

The coding scheme was **renumbered between 2025-03 and 2025-08**, so union-find over shared
detector codes cannot link a site across that boundary — it would produce two disjoint sites
per physical location, one per era, each with a truncated record, and every δ would be fitted
on half the panel. Exactly the failure this gate existed to catch, caught before 15 GB was
downloaded.

### Resolved, by measuring 2025-05 against 2025-06 (adjacent months across the break)

**`cod_detector` is not an identity in the old era at all.** Measured on the full months:

| | old era (2025-05) | new era (2025-06) |
|---|---|---|
| distinct codes | **52** | 897 |
| distinct `(code, lane)` | 94 | 897 |
| distinct geometries | 222 | 441 |
| **max geometries per code** | **92** | **1** |

Code `107` alone appears at 92 different locations; `106` at 55. In the old era the code is a
channel or device number, reused freely across the city. So the verified "998 codes, exactly
one geometry each" holds **only for the modern era**, and the union-find-over-shared-codes
design is dead for anything before mid-2025. The geometry tuple is the only identity the old
era has.

**Exact-tuple matching across the boundary also fails** — 3 of 222 geometries and 4 of 221
coordinates match, because the modern era re-surveyed the coordinates (and stores 8 dp rather
than 6). **Proximity plus name works**, which is the `assign_corridors` pattern already in the
repo:

| old sites | ≤5 m | ≤25 m | ≤60 m | ≤120 m | median nearest |
|---|---|---|---|---|---|
| 220 | 32 | 82 | 135 | **169** | 43.3 m |

Of those 169 nearest pairs, 143 agree on the street name and 99 on the full triple. The
sub-metre pairs are unmistakably the same site under cosmetic renaming — `Bv Batlle y
Ordonez` → `Batlle y Ordonez`, `Rivera` → `Fernandez Crespo` for a cross street — which is
exactly what `streets.normalize` (generic tokens dropped) and `name_score` already handle at
`STREET_NAME_FLOOR`.

**So Phase 3 becomes two mechanisms, not one:**

1. **Within an era** — group by the exact geometry tuple (coordinates normalised to 6 dp), and
   in the modern era carry `cod_detector` as a corroborating key since it is 1:1 there.
2. **Across the one known boundary** — a frozen, committed crosswalk built by proximity
   (`STREET_SNAP_M` = 120 m) gated by `name_score` ≥ `STREET_NAME_FLOOR`, with the pairing
   solved as a mutual-nearest match so two old sites cannot claim one new site. Every
   unmatched site and every ambiguous pair is **reported and left unlinked**, never guessed —
   painting a five-year trend through two different intersections is a worse failure than a
   site with a short record.

`data/sites.parquet` gains `era` and `linked_from`, and the crosswalk is frozen and committed
so the boundary is decided once rather than re-derived per run.

### What this costs, stated plainly

A linked panel of roughly **140–170 sites spans 2021 → 2026**, against 220 old-era and 441
modern sites. The rest are era-limited: the ~270 sites that only exist from mid-2025 get no
long trend, and ~50 old sites end at the boundary. `MIN_MONTHS_FOR_TREND` and the δ shrinkage
already handle this correctly — those sites are withheld from the trend map rather than fitted
on half a record.

The **city index survives**, because it is identified from within-detector variation and the
fixed effects absorb entry and exit. But at a boundary where 220 sites leave and 441 arrive at
once, `gamma` is bridged *only* by the ~150 sites present on both sides — about 21,600 cells,
which is ample, but it makes the boundary month the single most fragile point in the series.
It gets its own verification step, and Chart A should mark it, because a reader is entitled to
know the index is chained there rather than continuous.

### The file format changes three times — Phase 2 must handle all three

Measured from the headers and first rows of 2021-01, 2022-03 and 2025-08:

| | 2021 | 2022 → 2025-03 | 2025-08 → 2026 |
|---|---|---|---|
| Encoding | **UTF-8 BOM** | plain | plain |
| Delimiter | **`;`** | `,` | `,` |
| Speed column | **`velocidad_promedio`** | `velocidad` | `velocidad` |
| Time format | `23:55:00.0000000` | `23:55:04.0` | `23:55:0` |
| Coordinates | 6 dp | 6 dp | **8 dp** |
| Empty speed | empty | empty | **`""`** |
| `cod_detector` | 3 digits | 3 digits | 5–7 digits |

`etl.py`'s existing `COLUMN_ALIASES` already anticipates `velocidad` / `velocidad_promedio`,
so that part is covered. Three new hazards are not:

- **The BOM** may attach to the first column name, so the alias lookup would fail to find
  `cod_detector` in a 2021 file. Resolve the header case-insensitively and BOM-stripped.
- **`read_csv_auto` must sniff `;` for 2021.** It usually does, but the delimiter is the kind
  of thing to assert rather than hope for — a single-column parse would look like a schema
  change rather than an error.
- **`""` for an empty speed is the dangerous one.** If the sampler types the column as VARCHAR
  because of the quoted empties, every `speed > 0` comparison changes meaning silently. Aug
  2026 currently parses them as NULL (the ETL reports 15.6% empty), so it works today —
  but the ingest should assert the column came back numeric per file, not assume it.

Also: **8-decimal coordinates in the modern era** mean the exact-tuple grouping that Phase 3
relies on is only exact *within* an era. `-34.890561` and `-34.89056100` are the same place
and different strings. Normalise coordinates to a fixed 6 dp before grouping — which is not
the same as the rounding Phase 3 rejects, because 6 dp is ~11 cm and the collisions that
rejection was about sit at 0.1 m *with identical* stored values.

### The new detector code is structured, and the old one is not

`100301`/`100302` sit at identical coordinates with `id_carril` 1 and 2, and `1001001` through
`1001004` run in lane order. So the modern code is **`site * 100 + lane`** — site identity is
readable straight off it. The old 3-digit codes are per-lane but arbitrary: in 2022-03, `107`
(lane 1) and `114` (lane 3) share one coordinate and street triple. So old → new is a genuine
renumbering, not a re-encoding, and **the geometry crosswalk is the design that has to work.**

One immediate corroboration: `100301`/`100302` sit at exactly `-34.89056100, -56.22063100` —
the placeholder coordinate — so the feed's fallback behaviour survives the renumbering, and
`MAX_STREETS_PER_COORD` still earns its keep in the modern era.

## Phase 2 — Multi-month ETL

`src/mvdspeed/etl.py`, `build(csv_paths: Sequence[Path], *, con=None, force=False) -> IngestReport`,
keeping the existing `main(argv) -> int` shape. `ETL_VERSION = 2`.

```
data/processed/
  measurements/ym=2026-08/part.parquet     # daily archive, hive-partitioned   ~410 MB total
  detector_months/ym=2026-08/part.parquet  # per (cod_detector, ym) facts      ~1 MB
  detector_hist/ym=2026-08/part.parquet    # integer speed histograms          ~15-25 MB
  panel.parquet                            # the modelling table               ~30-45 MB
  ingest_log.parquet                       # provenance, drives incrementality
```

Four changes that matter:

1. **Per-month loop, not a glob over 68 files.** Not an OOM concern — duckdb streams a
   `GROUP BY` — but every reference and quality test is now per-month by definition, and a
   50–100 minute run must be restartable. Skip a month when `ingest_log` matches
   `(ym, source_name, source_bytes, etl_version)`. Incremental cost of next month: 45–90 s.
2. **Dedupe.** `any_value(velocidad) … GROUP BY ALL EXCLUDE (speed)` collapses the 434
   identical rows. Harmless today at 0.014%, but they inflate `n_moving`/`n_zero` in a
   sums-and-counts contract, and the same bug arrives at scale if two files ever overlap at
   a month boundary.
3. **Stop baking the quality filters into the archive.** `etl.py` currently drops dead lanes
   before writing (`etl.py:152-217`). `FLATLINE_SPEED`, `STALLED_*` and
   `MAX_PLAUSIBLE_SPEED` each carry a paragraph of evidence in `config.py`, and a constant
   you cannot change without a 90-minute rebuild is not really a constant. The archive keeps
   every plausible row; `detector_months` carries `is_dead`/`day_speed`/`night_speed`
   **per month**; the filters apply in `panel.build()`, in seconds.
4. **Store an integer speed histogram** per `(detector, ym, daytype)` — 121 bins, ~10 M
   sparse rows, 15–25 MB. Once you aggregate to bucket sums, quantiles are gone forever:
   you cannot compute a pooled 12-month p85 free-flow from `speed_sum`/`n_moving`. Speeds
   are integers, so a histogram is lossless and makes *any* percentile over *any* epoch
   exact. It also answers whether the distribution is shifting or the tail is thickening,
   which is a better insight than a mean shift.

Partition by the row's own `fecha`, not the filename, and report any row whose month
disagrees — a boundary leak would otherwise be silently misfiled.

## Phase 3 — Stable site identity

New `src/mvdspeed/identity.py`, CLI `mvdspeed-sites`.

Today's `site_id` is `dense_rank() OVER (ORDER BY lat, lon, street, …)` over one file
(`etl.py:112-121`). The *tuple* is correct within a month; only the numbering is unstable.

**Identity anchor is `cod_detector`. A site is a display grouping, linked across months by
detector-code overlap.**

1. Within each month, group by the exact raw tuple, as today — correct by construction, no
   thresholds.
2. Across months, **union-find over shared detector codes**: two monthly groups sharing ≥1
   code are the same site. Survives renames (`Bv Artigas` → `Bv. Artgias`), coordinate
   re-rounding, lane re-lettering, and the placeholder pile-up. Zero tuned thresholds.
   Extract the union-find from `streets._components` (`streets.py:148-191`) rather than
   writing a third one.
3. `site_key = min(cod_detector)` in the group, **frozen on first assignment**. Not a
   content hash — a hash changes when a lane joins or drops, which is the exact instability
   being removed. `mvdspeed-sites` preserves every existing assignment and only allocates
   for unseen codes, so history can never renumber.
4. Guards, **reported not silently applied**: coordinate spread > `SITE_SPLIT_M` (120 m,
   matching `STREET_SNAP_M`) splits at the discontinuity; pairwise `name_score` below
   `STREET_NAME_FLOOR` splits; a whole site's codes replaced at once (cabinet swap) makes
   *no* link, and the discontinuity is reported so the trend model can see it. Never merged
   on name or coordinate similarity alone.

**Rejected, with evidence.** Rounding coordinates into the key fuses `Bv Artigas: Cufre →
Gral Flores` — two sites 0.1 m apart with byte-identical street triples, disjoint code
ranges, averaging **20.4 and 41.3 km/h**. Normalising the street triple into the key fuses
20 triples covering 40 sites spread up to 9.2 km. An unordered from/to pair fuses 4
opposite-direction pairs at *identical* coordinates with 2.5–7.1 km/h gaps. `normalize()`
and `name_score()` are validators here, never key material.

`data/sites.parquet` (~1,100 rows, ~40 KB) is **committed**, for a stronger version of the
`streets.parquet` reason at `config.py:17-21`: it must be frozen or every `site_key` in
every model artifact silently changes meaning.

## Phase 4 — Windowed references, and the denominator trap

Free-flow is a **within-month** object: the 85th percentile of *this month's* non-zero
readings, computed exactly from the histogram per `(cod_detector, ym)`. Same for
`mean_speed`, `day_speed`, `night_speed`, `is_dead`, `is_stalled`.

Month-scoping the dead-lane test is the correctness fix a naive glob would miss: over 68
months, a lane dead for 60 and healthy for 7 passes a lifetime-mean test and gets
resurrected, while a lane healthy until 2024 and dead since gets deleted from 2022's history.

**Do not reuse free-flow as the cross-year denominator.** If free-flow slides with the
trend, a uniformly degrading corridor reports constant congestion. The fix is not to freeze
free-flow — it is that the cross-year metric should not involve free-flow at all:

| Metric | Denominator | Scope | Answers |
|---|---|---|---|
| `congestion` (unchanged) | `free_flow_speed[site, ym]` | within month | how obstructed was this corridor that month |
| `vs_base` (**new**) | `speed_base[site, bucket, daytype]` over `REFERENCE_EPOCH` | fixed | how much slower than baseline, in % |

`vs_base` needs no `MIN_FREE_FLOW_FOR_RATIO` gate, so it does not `NA` out the slow sites
that are the interesting ones — and it is the model's δ, so chart and map agree by
construction. `REFERENCE_EPOCH = ("2023-01", "2023-12")`: a full calendar year so
seasonality cancels, clear of 2021, old enough to leave the trend room. A site with no
baseline months gets `pd.NA`, never a fallback, and the count is reported.

`data.py` changes are additive only: `load(ym=None)` composes `sites` from
`data/sites.parquet` ⋈ `detector_months` and emits **exactly today's column names**, so the
flag derivation at `data.py:110-131` is untouched. Add `Dataset.ym`/`.months`/`.n_days`,
`sites["speed_base"]`, and `by_site(..., reference="epoch"|"base")` — default `epoch`, so
the single-month view is unchanged. (`Dataset.dates` has exactly one consumer, the caption
at `app.py:341`.)

## Phase 5 — The panel and the model

New `src/mvdspeed/panel.py` and `src/mvdspeed/model.py`, both free of Streamlit and colour,
matching the `surface.py`/`streets.py` docstring convention ("Deliberately free of colour
and Streamlit so it can be checked on its own").

`panel.DAYTYPES = {"weekday": (1,2,3,4,5), "sat": (6,), "sun": (0,)}` — a genuine partition.
`data.DAY_SCOPES` cannot be reused; "Every day" overlaps the other two. Exclude
`HOLIDAY_DATES` (Semana de Turismo and Carnival): Easter week empties the city and lands in
March some years, April others, so a full week of missing traffic would otherwise be
attributed to the trend.

```
log(speed_sum[i,b,d,m] / n_moving[i,b,d,m])
    = alpha[i,b,d] + gamma[m] + delta[s(i)]·u(m) + theta[b,d]·u(m) + eps
```

- `alpha[i,b,d]` — detector's time-of-day × day-type signature. Saturated, 998×48×3 =
  143,712 params, ~7,300 readings each. **Fit at detector grain, not site grain**: a site
  gaining a third lane in 2024 changes its average for reasons unrelated to traffic, and
  lane composition does churn. This also decouples the model from the site key entirely.
- `gamma[m]` — **the city index, net of panel composition.** One free parameter per month,
  no smoothing imposed on the headline series. The primary insight deliverable.
- `delta[s]` — site trend relative to the city, log per year, ~450 values, ridge-shrunk
  toward 0 with one λ chosen by backtest. `0` and **flagged** for sites with
  < `MIN_MONTHS_FOR_TREND` (12) months, never silently fitted.
- `theta[b,d]` — is the peak degrading faster than the trough. 144 params against ~9 M
  cells. Kept only if the backtest likes it.
- Constraints: `gamma` and `delta` weighted-mean-zero, `alpha` carries the level.

**Target and weighting.** `y = log(speed_sum / n_moving)` — log of the *arithmetic* mean,
because the quantity predicted must be the quantity displayed, and the dashboard displays
arithmetic km/h. Weight `w = n_moving`. Report the `w = 1` fit too; if the trend
coefficients move by more than ~0.2 km/h equivalent, understand why before shipping.

**The zero convention is a second model on the same design.** Since
`speed_sum/(n_moving+n_zero) ≡ (speed_sum/n_moving) × (n_moving/(n_moving+n_zero))` exactly,
fit speed on `n_moving` only and fit `logit(n_zero/(n_moving+n_zero))` on the identical
design with one extra bincount pass. The `include_zeros` toggle is then honoured exactly by
multiplying two predictions, rather than being unavailable or approximated. City-wide zero
share is 1.71%, so it is a small correction overall — but not at the sites where standstill
is the story.

**Fitting is backfitting with `np.bincount`**, since the design is a sum of saturated
blocks. Missing `(site, month)` cells need no imputation ever — an absent cell simply does
not appear in the bincount. Drop cells with total `n_moving < MIN_CELL_MOVING` (30).

**Partial-month estimation** — the headline capability. Fit `gamma` for an incomplete month
using only the cells present; `alpha[i,b,d]` absorbs which days and hours were sampled, so
the result is a month-equivalent index rather than an average of whatever landed in the
window.

```python
def fit(panel, *, months=None, shrink_delta=DELTA_SHRINK, with_bucket_trend=True,
        max_sweeps=40, tol=1e-7) -> Fit
def predict(fit, *, ym: str, include_zeros: bool = False, quantiles=None) -> pd.DataFrame
def extrapolate_index(city_index, *, horizon=2, season_window=36, damping=INDEX_DAMPING)
def backtest(panel, *, origins, horizons=(1, 2), methods=BASELINES) -> pd.DataFrame
def load_fit() -> Fit | None
```

`extrapolate_index` is a deliberate second stage: decompose the 68-point `gamma` series into
`level + local linear slope + season[month_of_year]`, extrapolate with a damped slope
(`φ ≈ 0.9`), and bound the slope to the last `SEASON_WINDOW` (36) months so a 2022 recovery
slope cannot drive a 2026 forecast. Less pure than one-stage; far more inspectable.

Report `gamma` as an **index in %**, never as km/h. `exp(mean of logs)` is a geometric mean,
and presenting it as "the average speed in km/h" would be exactly the kind of number this
project refuses to fake. km/h stays where it is meaningful: per site, per bucket.

## Phase 6 — Validation, before any UI work

**Primary test — partial-month recovery.** Truncate each of ~60 known-complete historical
months to its first k days (k = 3, 7, 11, 15), estimate the month-equivalent index, and
compare against that month's true full-month index. This is directly testable on abundant
real data and it measures the exact thing being shipped. Baseline to beat: the naive mean
over the truncated window — the thing the dashboard does today, whose bias we measured at
+0.341 km/h on the live fragment.

**Secondary — rolling-origin forward backtest.** Origins every month from 2024-01 onward
(~30), fit on `≤ o`, predict `o+1` and `o+2`. ~30 fits × ~3 s ≈ 2 minutes, which is the
point of choosing a 10-second model.

- Metric: MAE in km/h at `(detector, bucket, daytype)`, **weighted by the held-out month's
  `n_moving`** — the error a reader meets when they look at a sensor-hour. Report alongside:
  unweighted MAE; MAE restricted to bucket 35 (17:30, the app's default) on weekdays; and
  **bias separately**, since an unbiased noisy estimate is far more shippable than one
  running 1 km/h fast.
- Index metric: `|gamma_hat − gamma|` in percentage points against a refit including the
  held-out month.
- Baselines, with (2) named up front as the one to beat: (1) per-`(i,b,d)` mean over all
  history; (2) **per-`(i,b,d,month-of-year)` mean over the last 12 months**; (3) seasonal
  naive 12 months back; (4) last observed month.
- Uncertainty = **empirical quantiles of the backtest residuals** per horizon and day-type.
  Not analytic OLS standard errors: residuals are serially correlated within detector, so
  those intervals would be dishonestly narrow.

**The placebo that matters most.** Compute the naive city mean per month — what a glob-ETL
dashboard would show — alongside `gamma`, and report the gap. If the naive series says −8%
and `gamma` says −2%, six points of "the city got slower" was sensors entering and leaving
the panel. That belongs in the README.

**Pre-registered ship rule, decided before looking:** the forward per-sensor nowcast ships
only if it beats baseline (2) by ≥5% weighted MAE at both `h=1` and `h=2` and is not worse
at any of the last 6 origins. Otherwise the trend, the δ map and the partial-month
correction ship, and the forward number appears only as an index extrapolation with its
residual band.

## Phase 7 — Artifacts

`data/model/` — **committed**, ~1.5 MB: `alpha.parquet` (143,712 rows), `city_index.parquet`
(~70), `site_trend.parquet` (~450), `bucket_trend.parquet` (144), `site_refs.parquet`,
`residual_quantiles.parquet`, `manifest.json`.

The `config.py:17-21` argument applies with more force than it does for `streets.parquet`:
these are small, they change only when a month publishes, and they **cannot** be regenerated
without 51 GB of CSV a fresh clone will never have — while the trend chart is the headline.
`data/processed/**` stays gitignored. Accepted cost: ~1.5 MB binary diff per refit, ~18 MB/yr.

`manifest.json` carries `model_version`, `fit_at`, `code_revision`, `etl_version`, `months`,
`analysis_start`, `reference_epoch`, `sites_digest`, `config_digest`, cell counts and the
backtest summary. Three staleness checks with three different severities, because a fresh
clone with no archive must still render the committed trend chart:

| Check | On mismatch |
|---|---|
| `model_version` ≠ `config.MODEL_VERSION` | **hard error** with the refit command |
| `sites_digest` ≠ `sha256(data/sites.parquet)` | **hard error** — a re-keyed crosswalk makes every `site_key` silently meaningless |
| `last_month` < newest month in the local archive | visible caption, not an error |
| `config_digest` ≠ digest of model-relevant constants | caption: thresholds moved since the fit |

## Phase 8 — The app

### Three epistemic statuses, never blurred

| Status | Where it may appear | How it reads |
|---|---|---|
| **Measured** | anywhere; the map's default | today's behaviour, unchanged |
| **Balanced** (composition-corrected) | map, in its own mode; mandatory in any cross-month comparison | a reweighting of *this sensor's own observed cells* to a balanced month — nothing invented |
| **Extrapolated** | the trend chart only, dashed, band-first | never on the map, never in `st.metric` |

Four rules, each closing a specific hole:

1. **No extrapolated value is ever painted on the map** — not as raster, road, or dot, and
   through no `colors.sequential`/`diverging` call.
2. **No extrapolated value appears in `st.metric`.** That row is the measured KPIs; a
   prediction inside it is a prediction in a measurement's clothing.
3. **Every extrapolated value prints band-first**: `"−3.1% to +0.4% (central −1.4%)"`. There
   is no code path that renders the point estimate alone.
4. **Dashed means extrapolated, and nothing measured is ever dashed.** The dashed segment
   always sits right of an orange rule at the last month with data, reusing the `#eb6834`
   "you are here" rule from `app.py:736-740`.
5. **The unit carries the epistemic status** — the cheapest and least forgettable device, and
   stronger than any label. **km/h is reserved for measurements**, so Measured and Balanced
   both render in km/h and neither the index nor any extrapolation is ever converted to it.
   One deliberate exception: a caption states the base month's measured city average so a
   reader can convert by hand.
6. **A month in progress is hollow, not dashed.** `mark_point(filled=False)` on its last
   index value. Its `gamma` is fit on cells that *exist* — it is provisional because it will
   move as the month lands, not because it was invented. Dashed would file it under
   extrapolation and misstate what it is.
7. **Never show a corrected number alone.** The adjustment's whole value is its *difference*
   from the naive number, so the two always appear together with the gap stated in the
   slice's own units. Hiding the naive figure would repeat the error `4bb7820` fixed — a
   scale that silently disagrees with the mark drawn on it.
8. **Gap months break the line.** The catalogue turns out to have no gaps — all 68 months
   serve a file — but a month can still fail to ingest, so insert explicit null rows for any
   absent month and let `mark_line` break rather than drawing a straight segment across
   unmeasured weeks. Same reasoning as `congestion = pd.NA`.

**Rejected: a "predicted speed" map metric.** It would share the `"Average speed"` heat ramp
with the observed metric — by design, so they stay comparable — which leaves the reader no
channel to tell a measurement from a guess. Not sharing the scale is worse: then they can
neither compare nor distinguish. This is the `f825339` failure (wrong statistical object)
compounded by the `2748c9c` failure (a channel that says nothing).

**Rejected: a residual map.** Undefined for the month that matters, and for backtest months a
7.3% residual field renders as a random pattern a reader will inevitably read as geography.

**Rejected: `support_alpha` as the prediction channel.** It already means "how many sensors
back this estimate *in space*", recalibrated once precisely so it says one thing. A second
meaning makes it say neither. Short record is not spatial support: sites below
`MIN_TREND_MONTHS` are **withheld as `pd.NA`, not faded**, reusing the congestion-NA drop at
`app.py:381-392` verbatim.

**Rejected: a separate Nowcast page.** A page whose subject is the prediction over-claims by
architecture. The nowcast is two numbers and a dashed tail; it gets two numbers and a dashed
tail.

### Structure — a keyed radio, not `st.tabs`

`st.tabs` executes every tab body, so the trend charts would rebuild inside the 0.45 s Play
loop for nothing, and tab selection is frontend state that an `st.rerun()` every 450 ms can
snap back to tab 1. A keyed radio cannot lose state to a rerun and lets the heavy body be
genuinely skipped.

- **New first sidebar control at `app.py:250`**, above `st.subheader("View")` — it decides
  which other controls even apply:
  `view = st.radio("Show", ["This month's map", "Across the years"], horizontal=True, key="view")`,
  `on_map = view == "This month's map"`. Wrap 250-291 in `if on_map:` with an `else` pinning
  `metric_name = "Average speed"` so shared references stay valid.
- **View branch at line 370**, after `window_label` and before the map's fetch at 371:
  `if not on_map: render_years(...); st.stop()`. Reuses the guard pattern already at 379 and
  391, keeps the diff to two lines instead of re-indenting 350, and stops the animation loop
  by construction.
- **New "Period" section at line 292**: `st.selectbox("Month", [ALL_MONTHS] + MONTHS_DESC,
  index=1, format_func=mvd.month_label)`. Selectbox not radio — 56 options. Newest-first so
  the default is one click from the top and stays correct as files land. The in-progress month
  is labelled with its day count.
- **Metric options filtered by period.** `METRICS` entries gain a `"periods"` key; the two new
  entries are `("all",)` only, so a per-month "trend" map cannot be constructed at all.
  `"Average speed"` is in both scopes, so the by-name default lookup from `9dae456` never
  raises.
- **Disabled on the trend view**: Play (`disabled=all_day or not on_map`), `include_zeros`
  and `min_samples`. This is load-bearing, not tidiness — the naive city mean is computed at
  request time and *could* honour `include_zeros`, but `gamma[month]` was fit once and cannot.
  A toggle that moves one line and not the other would corrupt the most important chart in
  the feature. Disable with a caption naming the fit's conventions; or fit both conventions
  (it is two gamma series) and re-enable, which is cheaper than the caption.
- **New "Method" fork** after `min_samples`: `by_site(..., reference=...)` has no correct
  answer once the panel is five years long, so it goes to the reader in the `REACH_MODELS`
  spirit — "Its average in the selected period" (default) vs "…across all 56 months". With
  the pooled reference, a 2022 slice partly measures the trend rather than the time of day.
- **The balanced mode is a modifier on `speed`, not a metric.** It reweights each site's
  *own observed cells* to a balanced day-type mix, so it belongs beside `include_zeros` as
  `st.checkbox("Balance the day mix", value=False, disabled=is_complete_month)` — not in
  `METRICS`, which selects *what* is coloured, not *how* the same quantity is averaged.
  Default off for a single month, because what the sensors saw is the honest default and the
  measured-vs-balanced gap is itself informative. **Forced on wherever months are compared**
  (the trend view, and any cross-month KPI delta), since that comparison is invalid without
  it. Where a site has too few cells in the window to reweight, the value is withheld as
  `pd.NA` — same rule as the trend metric, and the count joins the quality notes.

### New `METRICS` entries (appended after `app.py:96`; order is safe post-`9dae456`)

- **`"Trend vs. the city"`** → `trend_pct`, `kind="diverging"`, `domain=(-4.0, 4.0)`,
  `periods=("all",)`. Polarity, so diverging per the `config.py:178` rule. Fixed domain, not
  data-driven, and fixed *across* time-of-day so the 03:00 slope and the 17:30 slope stay
  readable against each other: ridge shrinkage puts almost every site inside ±4%/yr, and the
  few outside are the ones shrinkage trusts least, so letting them set the scale would spend
  the whole ramp on the least reliable values. `colors.diverging` is warm-below-zero, so
  losing ground reads red — agreeing with the rest of the map, with no new code.

  **The column must be `delta[site] + theta[bucket, daytype]`, not `delta` alone.** `delta` is
  one number per site, so a `delta`-only map would not move with the time slider and would
  teach the reader that the control had stopped working — the `2748c9c` failure again, a
  channel that says nothing. If the fit ships without `theta`, this metric is withheld from
  the map rather than drawn as a constant field.
- **`"Months of readings"`** → `n_months`, `kind="sequential"`, `ramp="blue"`,
  `domain=(0.0, 60.0)`, `periods=("all",)`. A neutral count, so no heat — same role for the
  trend that `"Reading volume"` plays for speed. Ship it second but do ship it: without it the
  trend map's withheld sites look like coverage gaps rather than short records.

Two mechanical additions to the existing four: `"periods": ("month", "all")`, and an
`"na_note"` key so the congestion-specific NA drop at 381-392 and its quality note at 444-448
generalise to any metric that can be `pd.NA`.

Also considered and rejected: `theta` as a map metric — it has no per-site variation, so it
would paint 450 dots one colour and teach the reader the map had stopped responding.

### Charts in "Across the years"

All in the existing idiom: `make_axis()` (708-714), `background=chart_surface`,
`.configure_view(strokeWidth=0)`, the `["#2a78d6", "#eb6834", "#1baf7a"]` trio, orange rule
for "you are here", `st.caption` carrying the numbers. Frame shaping goes in a new pure-pandas
`src/mvdspeed/trend.py`; the Altair specs stay in `app.py`, per the repo's split.

**A. "The city index, and what the raw average would have told you" — the headline.**
Answers: is Montevideo actually slowing, and how much of the apparent change was the panel
changing rather than the traffic? Two lines over 56 months, both in % against the base month
so they share one axis: `gamma[month]` in `#2a78d6` at strokeWidth 2.5, and the naive weighted
mean of whatever reported that month in `TEXT_MUTED` at 1.5 — gray deliberately, it is the
wrong answer shown for contrast and must not compete. A `mark_area` between them at 0.10
opacity makes the composition effect visible *as* an area. Orange rule at the last complete
month; the in-progress month marked distinctly; then the dashed tail and the p10–p90 band from
the backtest residual quantiles. The band and dashed line must include the last published
point or they float detached from the series they continue. Read in %, with the km/h
conversion in the caption via the base month's measured average — never on a second axis.

**B. "Whether the peak is degrading faster than the night."** `theta[bucket, daytype]` in
%/yr against hour of day, reusing `hour_axis` (718) verbatim so it reads as a sibling directly
under the existing city profile at 705-753. Two lines (weekday, weekend), a `TEXT_MUTED` zero
rule, and the orange rule at the selected bucket — which is what keeps the time slider honest
on this view. **No band:** the uncertainty we have is a *forecast* residual by horizon, and
pinning it to a fitted contrast would borrow an interval from a different question.

**C. "Did my street diverge from the city, and when?"** The street comparison at 791-837 with
the x-axis swapped from hour to month: up to 3 streets (`max_selections=3`), their monthly
index in %, against `gamma` as a `TEXT_MUTED` reference. **The street lines stop at the last
complete month; only the city index continues past the orange rule** — rule 4 doing visible
work exactly where a per-street nowcast would be most tempting. Ships with the
`st.expander("Table view of these lines")` for the same reason as 831-835.

**D. The receipt for the band**, in an expander under A. Empirical coverage of the p10–p90
interval by horizon and day-type from the rolling-origin backtest: nominal 80% as a
`TEXT_MUTED` rule, achieved coverage as points. If it lands at 62%, the chart says so before a
reader has to discover it. **A prediction interval with no coverage evidence anywhere in the
UI is the same posture as a confidence channel that says nothing — if the backtest cannot
produce coverage by horizon, the band does not ship either.**

Rejected: a histogram of the ~450 `delta` values. Ridge shrinkage does its heaviest and least
trustworthy work in the tails, and a histogram's entire visual argument is its tails.

### Cache threading

`ym` is a **plain hashable argument, never `_ym`** (see trap 1). Threaded into all five
wrappers at 182-237 right after `key`, with `_key` renamed to `key` and `_metric_name` dropped.

`max_entries` becomes mandatory: the Play loop already fills 48 entries per state, and the
month dimension multiplies the key space by 56 while `st.cache_data` defaults to unbounded.
`max_entries=256` on `surface_for` and `street_field_for`, `512` on `sites_at`; leave
`profile`/`street_curves` alone. `ym` is constant across a Play pass, so the second pass is
all hits, exactly as today.

The model artifact loads beside `load_streets()` as a `cache_resource` that **may return
`None`** — the map is the measurement and must never depend on the fit; a missing artifact
costs the trend view and the two new metrics, never the dashboard. Caption once and carry on:
*"No fitted trend is available — run `mvdspeed-fit`. The map is unaffected."*

**Two separate tokens, not one:**

```python
CACHE_KEY = (dataset.panel_rows, dataset.n_months, dataset.latest_days)
MODEL_KEY = (model.version, model.fitted_at, model.n_months) if model else None
```

The separation is the point: refitting the model must not invalidate the map's slice caches,
and re-ingesting a month the fit has not seen must not invalidate the model's frames. The new
`latest_days` component (how many days the in-progress file holds) makes the fingerprint
survive a re-ingest that happens to land on the same row count, and makes the intent legible.
Where a map frame genuinely depends on the fit — `by_site` merging `trend_pct`/`n_months` —
that call takes both tokens.

The rename's one behavioural change is the intended one: rebuilding the parquet mid-session
now actually flushes the derived caches, which will show as a spinner during a re-ingest.

### Data-driven month strings

`st.set_page_config` must precede every other `st` call and `load_data()` is decorated (its
`show_spinner` string is an `st` call), so the title cannot come from the cached loader. Add
an **un-decorated** `data.panel_months()` reading a tiny `months.parquet` (one row per file:
`ym`, `n_days`, `n_sites`, `published_at`, `is_complete`), plus `data.month_label(ym)` beside
`bucket_label` and `MONTHS_ES` in `config.py`. Then: `app.py:44` becomes the *range*
(`"… · 2022–2026"`) since one month is no longer the whole extent; `337` takes
`month_label(ym)`; `338-343` swaps `len(dataset.dates)` (now ~1,200) for `Dataset.n_days(ym)`;
`322-325` and `data.py:91` rewritten.

**The ETL must derive `ym` from the readings' own `fecha`, not the filename** — same reasoning
as `MAX_STREETS_PER_COORD`, detect it structurally. Filenames carry the Spanish month name and
are the thing most likely to be renamed; the timestamps are the data.

### Copy — the pieces that carry the honesty

**Freshness caption, map view, new line at 420 under the KPI row:**

> Agosto 2026 is still in progress: 11 of 31 days, and the days it holds are not a balanced
> week — Wednesday, Thursday and Friday appear once each while Saturday and Sunday appear
> twice. Since weekends run about 2.5 km/h faster, the plain average over this window sits
> **+0.34 km/h high** for that reason alone. Nothing on this map is predicted. The balanced
> figure, and what the model expects for September, are under **Across the years**.

**The nowcast callout — the only place an extrapolated number is printed. Markdown, not
`st.metric`, band first:**

> **Nowcast · septiembre 2026.** The model puts the city index between **−3.1% and +0.4%**
> against agosto, central estimate −1.4%.
>
> That band is the p10–p90 of what this exact model did at a one-month horizon across 30
> rolling-origin backtests on weekdays. It is measured error, not a standard error, and it
> does not include the possibility that the model is wrong about the shape.
>
> **There is no per-sensor nowcast here, and there will not be one.** Split a single month's
> weekdays in two and predict one half from the other — no trend, no seasonality, no horizon
> at all — and a sensor's half-hour average is still missed by 2.5 km/h, 7.3% in the median. A
> city trend of 1–3% per year is a third of that noise. A predicted map would be a picture of
> the historical average wearing this month's label, on the same colour scale as the
> measurements, with nothing left to tell the reader which is which. The index can be nowcast
> because it averages about 40,000 of those cells; the cells cannot.

**Chart A caption** (numbers to be filled from the real fit): blue is the month effect from a
fit holding detector, half hour and day type constant, so it moves only when the same sensors
at the same hours change; gray is the plain weighted average of whatever reported; the shaded
gap is apparent change that was really the panel. Then the measured gap — *"the raw average
falls X% while the index falls Y% — most of what the raw series shows is composition"* — and
the km/h anchor for the base month, since the index is a geometric mean read in %.

**Trend-view disabled-controls caption:** "The fit uses one convention and the sidebar cannot
change it: zero readings excluded, sensors below 20 readings in a cell dropped, weekday and
weekend fit separately. Those two switches are disabled here rather than left live and ignored."

**Tooltip and CSV.** On the trend metric the sensor footnote at 500-503 becomes
`"{n} months of readings · fitted, not measured"`, and the street layer's footnote at 609
becomes `"fitted slope, estimated along the road"` — a road painted with a fitted slope is two
steps from a measurement and the tooltip should say both. In the CSV export (781-790) the
trend columns append *after* the measured ones and the column is named
`trend_pct_per_year_fitted`: a CSV strips every visual distinction, so naming is the only
defence left.

### Judgement calls to revisit during implementation

- **The pooled "Every month" map is the sin Chart A denounces** — a per-site mean over an
  unbalanced five-year panel *is* panel composition. Ship it anyway, with the new quality
  note, because refusing it pushes the reader to compute it worse in Excel. But there is a
  real argument for restricting "Every month" to the two fitted metrics. Least-settled
  decision in the design.
- **`MIN_TREND_MONTHS = 12` is currently a plausible round number**, and it is exactly the
  kind of threshold `2748c9c` was about. It goes in `config.py` with its evidence written out
  — how many sites it withholds, and what the fitted slopes look like just above and below
  the line — or it does not go in at all.

## Files

| Path | Change |
|---|---|
| `src/mvdspeed/fetch.py` | **new** — CKAN enumeration + download, `mvdspeed-fetch` |
| `src/mvdspeed/identity.py` | **new** — census, union-find site keys, `mvdspeed-sites` |
| `src/mvdspeed/panel.py` | **new** — day-type partition, holidays, modelling table |
| `src/mvdspeed/model.py` | **new** — fit / predict / extrapolate / backtest, `mvdspeed-fit` |
| `src/mvdspeed/trend.py` | **new** — pure-pandas frame shaping for the three charts + `load()` |
| `src/mvdspeed/etl.py` | multi-month loop, dedupe, unbaked filters, partitioned outputs, histogram, `months.parquet`, site-grain `app_panel.parquet` |
| `src/mvdspeed/config.py` | ~20 constants, each with its paragraph of evidence |
| `src/mvdspeed/data.py` | `load(ym=)`, `Dataset.ym/.months/.n_days`, `by_site(reference=)` |
| `src/mvdspeed/streets.py` | extract union-find for `identity.py` to share |
| `src/mvdspeed/app.py` | tabs, month picker, two metrics, three charts, data-driven titles |
| `pyproject.toml` | four new scripts; **declare `numpy` and `pillow`** (both load-bearing, both currently transitive); a `[dependency-groups]` dev group with `pytest` — the first in this repo |
| `README.md` | correct the publication-lag claim; add the new modules to `## Layout`; add the composition-artefact finding |

Reuse rather than reinvent: `streets.normalize`/`name_score`/`to_km`/union-find,
`surface.support_alpha`, `streets.street_field` for the δ corridor map, and `data._weighted`'s
numerator/denominator convention as the model's target.

## Verification

The repo has no tests and no CI; the convention is pure functions verified against real data
with the numbers written into the commit message. Following it, plus a first pytest module
for the two new pure modules (`panel.py`, `model.py`) — they are the first code here whose
correctness cannot be eyeballed on a map.

1. **Identity gate** — three spaced months share detector codes; report overlap, coordinate
   drift and `name_score`. Hard stop if codes do not persist.
2. **ETL parity** — re-ingesting Aug 2026 alone reproduces today's `measurements.parquet`
   except for the 434 duplicates and the unbaked filter. Diff row counts and the city curve.
3. **Identity coverage** — all 442 current sites map to exactly one `site_key`; the 20 triple
   collisions and 57 mirrored pairs stay separate; the 50 placeholder detectors are keyed and
   flagged `has_location = False`.
4. **Dashboard parity** — `by_site(load(ym="2026-08"))` reproduces today's numbers: city
   curve peak 42.8 km/h at 02:30, trough 20.8 at 17:30, 404 mappable sites at 17:30.
5. **Model recovers a synthetic trend** — generate a panel with known `gamma`, `delta`,
   `theta`, confirm recovery within tolerance, and confirm invariance to dropping random
   `(site, month)` cells. This is the test that the composition correction actually works.
6. **Partial-month recovery** — the Phase 6 primary table, at k = 3, 7, 11, 15 days across
   ~60 months, against the naive-window baseline.
7. **Forward backtest** — the Phase 6 table across ~30 origins, per-origin plot, against all
   four baselines. Decides whether the forward nowcast ships at all.
8. **Cache-key regression** — the trap that would be silent. Select two different months and
   assert the rendered city average differs; assert `sites_at` returns different frames for
   two `ym` values with all else equal. This is the test that proves the `_key` → `key`
   rename actually took effect, and it cannot be eyeballed.
9. **Band coverage** — empirical p10–p90 coverage by horizon and day-type from the backtest.
   If it is not near 80%, the band does not ship (Chart D is the receipt).
10. **App smoke** — run `streamlit run src/mvdspeed/app.py`; check both views, the month
    picker including the in-progress month, all six metrics, three reach models, both
    basemaps, Play on the map view and Play correctly disabled on the trend view.

## Sequencing

Phase 1 can invalidate Phase 3, so it runs on three spaced months before the full download.
Phase 6 runs **before** any app work, because its table decides what there is to build.

Budget: ~15 GB download, ~51 GB expanded CSV, ~500 MB of durable artifacts. Checked — 339 GB
free on this machine, so no constraint. The raw CSVs can be recompressed to `.csv.gz` (duckdb
reads them natively, ~8 GB) or deleted after ingest; the 410 MB archive plus the committed
40 KB crosswalk is the durable thing, not the CSVs.

Rough effort: Phases 0–4 are the bulk of the engineering and the least glamorous; Phase 5 is
small (the fit is ~40 lines of `bincount`); Phase 6 decides Phase 8's scope. The full ingest
is ~50–100 minutes unattended, once.
