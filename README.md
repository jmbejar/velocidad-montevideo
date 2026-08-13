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

Road geometry for the street layer ships with the repo
(`data/streets.parquet`), so nothing else is needed. `uv run mvdspeed-osm
--refresh` re-fetches it from Overpass, which is worth doing only when the
sensor set moves onto roads the current extract does not cover.

## What the dashboard shows

- **A map, four ways.** Colour by *Congestion* (how far below its own free-flow
  speed a sensor is), *Average speed*, *vs. its own typical* (where this time of
  day is unusual for that specific spot), or *Reading volume* as a coverage
  sanity check.
- **Three ways to draw it**: the sensors as dots, a smooth heat *surface*
  between them, or the *streets* themselves — the avenues painted along their
  real OSM geometry, which is the only one of the three that does not imply the
  measurement covers ground it never saw.
- **A time-of-day slider** over 30-minute buckets, with a ▶ Play button that
  animates the full day, and an all-day-average toggle. Colours are on a fixed
  scale, so hours are comparable as it runs.
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

Five further sensor-quality rules, all in `config.py`:

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

- **37 of 908 lane detectors never measured moving traffic** (or never averaged
  3 km/h) across the whole month, and are dropped before anything is aggregated.
  This test has to run per *lane*, because the raw file is one row per lane and a
  healthy lane hides a dead one when they are averaged together: `Barradas`
  (Av Italia → Rambla) advertised two lanes, but lane 1 logged 3,168 zeros and
  never a single moving vehicle. Judged at site level the site looks fine,
  because lane 2 is. Ten otherwise-healthy sites had a lane dropped this way.
  Their speeds barely move — a dead lane contributes only zeros, which are
  excluded by default — but it stops them inflating lane counts and the
  "Zero readings" figure, which fell from 1.9% to 0.7% at 06:00 once the dead
  detectors stopped voting.
- **16 sites where every lane is dead** are treated as stuck and excluded
  outright, but kept in the inventory so the exclusion stays reportable rather
  than the site silently vanishing from the totals.

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
- **Congestion is a ratio**, so it is left undefined rather than faked for the 3
  sensors whose free-flow reference is under 10 km/h — at that scale the ±0.5
  km/h rounding is already worth 5%. This is what stopped a sensor crawling at
  1 km/h from being drawn as "0% congested".

Of 442 sites, **404 are mappable** at a typical rush-hour slice:
442 − 16 stuck − 7 stalled − 13 unlocatable = 406, of which 404 clear the
minimum-readings floor.

Street names in the source are inconsistent (`Bv Artigas` vs `Bv. Artgias`,
`Bv Espana` vs `Bv. España`, trailing spaces). The ETL trims whitespace but does
not attempt to merge misspellings, so a few streets appear twice in the
comparison picker.

## How the numbers are computed

The ETL aggregates to 30-minute buckets but stores **sums and counts, not
averages**, so every mean the app computes later — over any set of days, hours,
or streets — is exact rather than an average of averages. A "site" is one
physical measuring point; the raw file has one row per *lane*, and several lane
detectors share a coordinate. Of 908 lane detectors that reported anything usable,
871 survive the dead-lane test, and they sit at 442 sites.

Free-flow speed per sensor is its 85th-percentile non-zero reading, the standard
traffic-engineering proxy — robust to both jams and the occasional speeder.

## Layout

```
src/mvdspeed/
  config.py   paths, cleaning thresholds, surface parameters, palette
  etl.py      CSV -> parquet (duckdb)
  data.py     loading and slicing
  surface.py  kernel-weighted heat surface over the point sensors
  streets.py  sensor -> road matching, and the three reach models
  osm.py      one-off Overpass fetch -> data/streets.parquet
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

deck.gl's `HeatmapLayer` estimates point **density** — "where are there many
events". That is the wrong statistical object for an attribute measured at fixed
stations, and no amount of colour tuning fixes it. It bins points and averages
within each bin, so **intensity depended on how many neighbours a sensor happened
to have.** The east of the city has a median of 3 sensors within 1 km against 30
in the centre, so a lone eastern sensor rendered undiluted with a hard edge while
a clustered central one was averaged toward the local mean. Carrasco read as dark
blobs on a median congestion of 0.54, against 0.52 for the rest of the city.

So the surface is computed here instead (`surface.py`), with one expression
applied identically at every point:

```
w_i     = exp(-(d_i / 500 m)^2),  zero beyond 1.2 km
value   = Σ(w_i · v_i) / Σw_i
support = Σw_i
```

Density no longer changes `value` — it changes `support`, which becomes opacity
(sublinear: support spans 0.02–52, so a linear map would leave all but the
densest cells invisible; the reference and exponent matter, see
`SURFACE_ALPHA_*`). Cells with no sensor inside the cutoff are fully transparent,
so unmeasured ground stays bare instead of being interpolated across. About
10,200 cells build in ~45 ms and 38% of them end up supported.

**It still renders as a smooth heatmap**, because the grid is drawn as a single
texture: the raster is tinted in numpy, PNG-encoded to a ~17 KB data URI (28 ms),
and handed to a `BitmapLayer`, where the GPU interpolates between cell centres.
Drawing it as thousands of `GridCellLayer` squares was the first attempt and read
as visibly pixelated. Two notes for anyone touching this:

- The image must be wrapped in `pdk.types.String(...)`. pydeck serialises plain
  strings as accessor expressions, so a bare data URI arrives as
  `"@@=data:image/png;..."` and deck.gl dies parsing it at the colon — the same
  trap that silently broke `aggregation="MEAN"` earlier.
- Image rows run north-to-south while the grid is built south-to-north, so the
  raster is flipped on encode, and `bounds` is the grid's outer edge rather than
  the corner cell centres. Getting either wrong offsets the surface against the
  dots.

Unsupported cells still get a colour (the ramp's low end) and are hidden with
alpha alone, so bilinear filtering has no black to bleed in from at the edges.

### Colouring the avenues instead

The surface fixed *how* the estimate was computed but not *what shape* it was
drawn as. Painting an area still implies areal support for a linear measurement:
a sensor watches one stretch of one avenue, not the neighbourhood around it. The
street layer draws the roads themselves (`streets.py`), which also disposes of
the surface's worst artefact for free — a coastal sensor can no longer bleed
1.2 km out over the bay, because there is no road there to paint.

The matching this needs looked like the hard part and turned out not to be, once
the question was asked the right way round. Matching `Bv. Artgias` to `Bulevar
General Artigas` from the string alone is hard. But every sensor already carries
a coordinate, so **proximity does the join and the name only disambiguates**
between the two-to-four roads meeting at that corner — a much easier job.
Names are compared on normalized tokens (accents stripped, `Avenida`/`Bulevar`/
`General` dropped), with two rules that each pay for themselves:

- a single-letter token matches a word beginning with it, which is what carries
  `L A de Herrera` to `Avenida Luis Alberto de Herrera`;
- on tokens of five characters or more, a `difflib` ratio of 0.85 counts as a
  match, which absorbs `artgias` → `artigas` (0.86) while correctly rejecting
  `colonia` → `colorado` (0.67). An earlier four-character prefix rule accepted
  both, and quietly painted the wrong avenue.

That lands **392 of the 406 mappable sites (97%)** on a road. The 14 that miss
are genuinely not streets — `Salida Pereira Rossell`, `Tunel Av Italia`,
`Entrada Nuevocentro`, `Circ Batlle W` — and they are left as dots and counted in
the data-quality line rather than snapped to whatever road happens to be nearest.
Painting a whole avenue from a sensor that was really watching the cross street
is a worse failure than admitting the miss.

Two details that are load-bearing:

- Corridors are grouped by normalized name and then split into spatially
  connected components, because two unrelated streets can share a name at
  opposite ends of the city and merging them lets one sensor's colour teleport
  across town. Endpoint matching alone would not do it either: a dual
  carriageway like Bv Artigas is two parallel ways that never touch, so ways are
  joined when their vertices land in the same ~100 m cell.
- Distances are plain euclidean, but only ever taken *within one corridor*, and
  that is what lets them stand in for distance along the road without a routing
  graph. Sensors on the far side of the block are not candidates because they
  are on a different corridor, not because the geometry says so.

**Three reach models**, because "how much of the avenue may one sensor speak
for" is a question about what you want to see, not one with a correct answer:
*blend* is the surface's Gaussian confined to the road, smooth and interpolating;
*nearest* hands the stretch to the closest sensor outright at the midpoint, so
every painted metre is one real measurement; *stub* paints only 250 m either side
and leaves the rest bare, which is the most literal thing the data supports.
All three share the geometry and differ only in how a chunk picks its value, and
all three feed opacity from the same kernel weight so the confidence channel
keeps meaning one thing across them.

Roads are drawn as ~60 m chunks, each its own two-point `PathLayer` path so it
can carry its own colour. Those want **butt caps, not rounded**: at city zoom a
60 m chunk is about as long as the line is wide, so rounded caps turn every one
of them into a circle and the avenue renders as a string of beads.

Two things fall out of this for free. Cell colours come from the **same**
`colors.sequential()` / `colors.diverging()` the dots use, so the two layers
cannot drift onto different scales — a bug that had to be fixed twice while the
`HeatmapLayer` maintained its own `colorDomain`. And `vs. its own typical` can
now be drawn as a surface: a cell holding a mean *signed* deviation is
meaningful, where a density cloud could not carry a sign at all.

## Caveats

- 11 days of one winter month is not a seasonal baseline.
- Sensors sit at intersections on monitored corridors, so coverage is not
  uniform across the city — absence of dots is not absence of congestion.
- The surface is an average, so it softens the extremes: cell values span
  0.25–0.68 (p5–p95) where raw sensors span 0.09–0.93. The dots carry the real
  measurements.
- It bleeds over water near the Rambla — a coastal sensor supports cells up to
  1.2 km offshore and there is no coastline polygon here to mask against. The
  street layer does not have this problem, since it only paints roads.
- The street layer's distances are euclidean within a corridor, which stands in
  for distance along the road only while the road does not double back on itself
  inside the cutoff. The Rambla rounding Punta Carretas is the case where it
  does, and a sensor there reaches slightly across the point rather than around
  it.
- 14 of the 406 mappable sensors match no road and appear only as dots. They are
  mostly slip roads and tunnel approaches that the feed names as streets.
- `data/streets.parquet` is a snapshot of OSM taken in May 2026. Nothing breaks
  as the city changes; corridors just stop reflecting recent roadworks.
- The basemap tiles come from CARTO's CDN, so the map needs network access
  (no API key required). The road geometry does not — it ships with the repo.
