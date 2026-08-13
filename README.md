# Montevideo average speed

Interactive exploration of Montevideo's open traffic-sensor data: a city map with
a congestion heatmap and a time-of-day slider, so you can watch the rush hour
build and drain.

![Montevideo speed dashboard](docs/screenshot.jpg)

## Quick start

```bash
uv sync                                                          # install
uv run mvdspeed-etl ~/Downloads/Velocidad_promedio_Agosto_2026.csv   # once
uv run streamlit run src/mvdspeed/app.py                         # serve
```

The ETL turns the 262 MB / 3.1 M-row CSV into a 2 MB parquet pair in about 25
seconds. Re-run it when a new month's file is published.

## What the dashboard shows

- **A map, four ways.** Colour by *Congestion* (how far below its own free-flow
  speed a sensor is), *Average speed*, *vs. its own typical* (where this time of
  day is unusual for that specific spot), or *Reading volume* as a coverage
  sanity check.
- **A time-of-day slider** over 30-minute buckets, with a ▶ Play button that
  animates the full day, and an all-day-average toggle.
- **Day scope**: weekdays, weekend, or every day.
- **The city's daily curve**, with the selected half hour marked.
- **Rankings and street comparison**, plus a CSV export of the current slice.

## What the data actually says

Weekdays across 1–11 August 2026, at 442 measuring points:

| | |
|---|---|
| Free-flow peak | **42.8 km/h** at 02:30 |
| Worst half hour | **20.8 km/h** at 17:30 |
| Morning drop | 31.0 → 24.8 km/h between 07:00 and 07:30 |
| Evening recovery | 22.3 → 27.4 km/h between 18:00 and 18:30 |

The evening peak bites harder than the morning one, and midday never recovers to
overnight speeds — the city sits on a ~23.7 km/h plateau from 08:00 to 16:00.

## Reading the data honestly

The raw column `velocidad` hides three different things, and conflating them is
the easiest way to draw a wrong map. Of 3,146,779 rows:

| | rows | treatment |
|---|---|---|
| A real speed measurement | 2,492,730 (79.2%) | used |
| Exactly `0` | 155,359 (4.9%) | **excluded from averages by default** |
| Empty | 491,222 (15.6%) | dropped — the sensor reported nothing |
| Above 120 km/h | 7,468 (0.2%) | dropped as sensor error (the file reaches 540) |

A `0` means *either* that no vehicle crossed the lane in those five minutes *or*
that traffic was completely stopped, and nothing in the file distinguishes them.
Averaging them in would drag every quiet street and every night hour toward
zero, so they are excluded by default and reported separately as the "Zero
readings" figure. The sidebar has a toggle if you want the other convention.

Four further sensor-quality rules, all in `config.py`:

- **13 sensors share one placeholder coordinate.** The feed falls back to
  `(-34.890561, -56.220631)` — a point out in the bay — when it has no real
  position, so Rambla, Belloni, Gral Flores, Bv Artigas, Bv Batlle y Ordoñez and
  Camino Carrasco all pile onto one dot in the water. Rather than hardcode that
  coordinate (next month's file may use a different one), it is caught
  structurally: a genuine point carries **at most two** street names — an
  intersection, or one street under two spellings (`Ariel` / `Camino Ariel`,
  `Gral Flores` / `SB Gral Flores`). Three or more distinct streets at identical
  coordinates means the coordinate is a fallback, not a place. Those sensors keep
  their readings in the daily curve, where position is irrelevant, and are left
  off the map and the rankings, where it is not.

- **16 sensors never recorded moving traffic** in the whole month (one reports
  ~1.6 km/h across 5,930 readings). They are treated as stuck and excluded.

- **7 sensors are not watching through traffic.** They sit pinned under 3 km/h
  for the fifteen hours from 07:00 to 22:00, then report 9–26 km/h at 3am. Real
  congestion clears by late evening; theirs never does. Their free-flow
  references corroborate it: 12–14 km/h where the other twenty sensors along
  Av Italia see a median of 45. Most are probably turn lanes, bus bays or
  permanently occupied queues — real places, but not the road they are named
  after. Detected as `day_speed < 3 km/h AND night_speed > 3 × day_speed`,
  measured on weekdays, since weekend mornings dilute the daytime window enough
  to hide one of the seven. Excluded from every view, including the city curve —
  a detector stuck at 1.9 km/h all month would otherwise drag it down.
- **Congestion is a ratio**, so it is left undefined rather than faked for the 7
  sensors whose free-flow reference is under 10 km/h — at that scale the ±0.5
  km/h rounding is already worth 5%. This is what stopped a sensor crawling at
  1 km/h from being drawn as "0% congested".

Of 442 sites, **404 are mappable** at a typical rush-hour slice once the stuck,
stalled and unlocatable sensors are set aside.

Street names in the source are inconsistent (`Bv Artigas` vs `Bv. Artgias`,
`Bv Espana` vs `Bv. España`, trailing spaces). The ETL trims whitespace but does
not attempt to merge misspellings, so a few streets appear twice in the
comparison picker.

## How the numbers are computed

The ETL aggregates to 30-minute buckets but stores **sums and counts, not
averages**, so every mean the app computes later — over any set of days, hours,
or streets — is exact rather than an average of averages. A "site" is one
physical measuring point; the raw file has one row per *lane*, and several lane
detectors share a coordinate, so 998 detectors collapse to 442 sites.

Free-flow speed per sensor is its 85th-percentile non-zero reading, the standard
traffic-engineering proxy — robust to both jams and the occasional speeder.

## Layout

```
src/mvdspeed/
  config.py   paths, cleaning thresholds, palette
  etl.py      CSV -> parquet (duckdb)
  data.py     loading and slicing
  colors.py   value -> colour scales and legends
  app.py      the Streamlit dashboard
```

## Visual design notes

Colour is assigned by the job it does, which is why there are three scales and
no rainbow anywhere:

- **Traffic severity** (congestion, average speed) → a yellow→orange→red heat
  ramp. Multi-hue sequential is only legitimate for analogous neighbours or
  semantic heat, and this is both. It is verified strictly monotonic in OKLab
  lightness (0.988 → 0.381), which is what preserves the ordering under
  colour-vision deficiency even where the hue shift is lost.
- **A neutral count** (reading volume) → one blue hue, light to dark. It carries
  no good/bad reading, so it gets no heat.
- **Polarity** (`vs. its own typical`) → diverging red↔blue with a neutral gray
  midpoint, and it is never drawn as a heatmap, because a heat cloud cannot
  carry a +/− sign.

All scales are re-stepped for the dark basemap rather than flipped: on dark the
sequential ramps run dark→light, so the extreme value is the one that survives
against near-black (the heat ramp becomes a thermal white-hot core), and the
legend gradient reverses with it. Legends carry five labelled ticks rather than
two, so the intermediate steps can actually be decoded.

The three-colour street palette was checked with a colour-vision validator
(worst all-pairs ΔE 9.2 light / 9.4 dark); because one of those hues sits under
3:1 against the light surface, that chart ships a table view rather than relying
on colour alone.

### Why the heat cloud needed more than colour

Two deck.gl defaults conspire to flatten a `HeatmapLayer` built on point
sensors, and both had to be overridden:

- **`aggregation` defaults to `SUM`**, so three ordinary sensors on one corner
  outrank one badly jammed sensor on its own — the cloud ends up mapping sensor
  density rather than congestion. It is set to `MEAN`. Note that pydeck
  serialises a bare Python string as the accessor expression `"@@=MEAN"`, which
  deck.gl evaluates to `undefined` before silently falling back to `SUM`;
  `pdk.types.String("MEAN")` is required to pass it as a literal.
- **`colorDomain` defaults to the single hottest cell**, so one pair of adjacent
  near-stopped sensors saturates the scale and squashes the rest of the city
  into the bottom step. It is pinned to the 10th–92nd percentile of the current
  slice, so one outlier cannot reclaim the ramp.

`MEAN` fixes `SUM`'s density bias but has one of its own, worth knowing when
reading the map: **the same value draws darker where sensors are sparse.** The
east of the city has a median of 3 sensors within 1 km against 30 in the centre,
so an eastern cell renders one sensor's value undiluted with a hard edge, while a
central cell averages ~30 and regresses toward the city mean. Carrasco looks
worse than it is for this reason — its median congestion is 0.54 against 0.52 for
the rest of the city. The dots are the authoritative read.

## Caveats

- 11 days of one winter month is not a seasonal baseline.
- Sensors sit at intersections on monitored corridors, so coverage is not
  uniform across the city — absence of dots is not absence of congestion.
- The heat cloud smooths 442 point sensors and interpolates between them, so it
  reads as coverage over areas that have no sensor at all. The dots carry the
  real measurements.
- The basemap tiles come from CARTO's CDN, so the map needs network access
  (no API key required).
