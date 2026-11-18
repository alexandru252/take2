# Take2 - Specification for v0.1

Take2 is an open-source training tool that helps VFR pilots (and flight-sim pilots) avoid airspace infringements. You load a route, set the day's QNH, and Take2 shows which airspaces you pass, draws the vertical profile, converts flight-level-based limits to the real altitude of the day, and warns you when you do not keep the "Take 2" safety margin (2 NM laterally, 200 ft vertically). It also contains short training scenarios with explanations.

> **Not for navigation.** Take2 is a training and learning tool only. Airspace data may be incomplete or wrong. Always use official AIP data and approved navigation tools for real flights.

---

## 1. Why this matters

- Airspace infringements are a recurring safety problem in European general aviation (UK CAA reported 1,136 infringements in 2025, CAP3277).
- The main causal factors are poor planning, weak threat and error management and altimetry mistakes. One classic trap: an airspace base defined as a flight level (for example FL65) is **lower** than its number suggests when the QNH is low.
- Commercial planners (SkyDemon, EasyVFR) warn pilots in flight, but there is no free, open tool to **practise** this planning step and understand the QNH effect.

## 2. Users

- Student pilots (PPL/LAPL) learning airspace and altimetry.
- Private pilots refreshing their planning.
- Flight instructors who want ready-made exercises.
- Flight-sim pilots (MSFS, X-Plane) who fly VFR and want realism.

## 3. Scope of v0.1

**In scope**
1. One country: Romania (data from openAIP).
2. Route input: GPX file, MSFS `.pln` file, or a simple CSV (`name,lat,lon,alt_ft`).
3. A cruise altitude (used when the route file has no altitudes) and the QNH of the day.
4. Map with the route and the airspaces it crosses or comes close to.
5. Vertical profile chart: aircraft altitude along the route plus the vertical band of each relevant airspace, converted with the day's QNH.
6. List of events along the route: infringements, margin warnings, requirements (TMZ/RMZ).
7. "QNH effect" panel: for each flight-level-based limit, show the real altitude today versus at 1013.25 hPa.
8. Three training scenarios with fictional airspace and explanations.
9. Command line interface and a simple web interface (Streamlit).
10. Unit tests for all calculations, CI on GitHub.

**Out of scope for v0.1** (see Roadmap)
- Terrain elevation, climb and descent performance, wind, temperature correction.
- Countries other than Romania.
- NOTAMs, real-time data, user accounts, mobile app.
- Any use as a navigation tool.

## 4. Domain background

**Take 2 rule.** Plan to stay at least 2 NM laterally away from controlled airspace boundaries and at least 200 ft below (or above) its vertical limits.

**Vertical limits** can be expressed as:
- feet above mean sea level (`MSL`), read on the QNH,
- flight level (`STD`, pressure altitude on 1013.25 hPa),
- feet above ground (`GND`/`SFC`).

**Flight level versus QNH.** A flight level is a pressure altitude. With the altimeter on QNH, the indicated altitude of a given flight level changes with the QNH. When QNH is below 1013.25 hPa, a flight-level base is **lower** in altitude than its nominal value.

## 5. Data sources and licences

| Source | Use | Licence / access |
|---|---|---|
| openAIP (openaip.net) | Airspace polygons and vertical limits for Romania | CC BY-NC 4.0, attribution required; free account and API key |
| Fictional airspace (in this repo) | Tests and scenarios | Our own, MIT |

Rules:
- The API key is read from the environment variable `OPENAIP_API_KEY` (file `.env`, never committed).
- Downloaded openAIP data goes into `data/` which is listed in `.gitignore`. Tests never depend on real data.
- The web interface and README show: "Airspace data © openAIP contributors (openaip.net), CC BY-NC 4.0".
- Check the current openAIP documentation (docs.openaip.net) for the exact endpoint, authentication header, pagination and the meaning of the `unit` and `referenceDatum` codes before writing the loader.

## 6. Domain model (`src/take2/models.py`)

Use pydantic v2 models with explicit units in field names.

```python
class LimitReference(str, Enum):
    GND = "GND"   # above ground / surface
    MSL = "MSL"   # above mean sea level
    STD = "STD"   # flight level (1013.25 hPa)

class VerticalLimit(BaseModel):
    value: float            # number as published (feet, metres or FL number)
    unit: Literal["FT", "M", "FL"]
    reference: LimitReference

class Airspace(BaseModel):
    id: str
    name: str
    type: str               # CTR, TMA, CTA, ATZ, TMZ, RMZ, P, R, D, ...
    icao_class: str | None  # "A".."G" or None
    lower: VerticalLimit
    upper: VerticalLimit
    polygon_lonlat: list[tuple[float, float]]   # WGS84, closed ring
    fictional: bool = False

class Waypoint(BaseModel):
    name: str
    lat: float
    lon: float
    alt_ft: float | None = None   # planned altitude AMSL on QNH

class Route(BaseModel):
    waypoints: list[Waypoint]

class Event(BaseModel):
    airspace_id: str
    airspace_name: str
    kind: Literal["INFRINGEMENT", "WARNING", "REQUIREMENT", "INFO"]
    start_nm: float          # distance along route
    end_nm: float
    min_lateral_nm: float    # 0 when horizontally inside
    min_vertical_ft: float   # 0 when inside the vertical band
    approximate: bool = False   # true if a GND-referenced limit was used without terrain data
```

## 7. Algorithms

### 7.1 Altimetry (`altimetry.py`)

Pressure altitude of a pressure level (ISA, troposphere):

```
H(p) = 145366.45 * (1 - (p / 1013.25) ** 0.190284)     # feet, p in hPa
```

An altimeter set to QNH indicates `H(p) - H(QNH)`. Therefore the altitude on QNH of a flight level is:

```
alt_on_qnh_ft(FL) = FL * 100 - H(QNH)
```

Conversions:
- `M` to `FT`: multiply by 3.28084.
- `MSL` limits are already altitudes on QNH.
- `GND` limits: in v0.1 assume ground elevation 0 ft unless a value is provided; mark the result as `approximate = True`. A lower limit of `GND`/`SFC` with value 0 means the airspace reaches the ground.

Functions:
- `pressure_altitude_of_level_ft(p_hpa) -> float`
- `fl_to_altitude_on_qnh_ft(fl, qnh_hpa) -> float`
- `limit_to_altitude_ft(limit, qnh_hpa, ground_elev_ft=0.0) -> tuple[float, bool]` (value, approximate)

**Reference values for tests** (tolerance ±0.5 ft):

| QNH (hPa) | H(QNH) ft | FL65 on QNH | FL95 on QNH |
|---|---|---|---|
| 1033.25 | -541.7 | 7041.7 | 10041.7 |
| 1023.00 | -265.1 | 6765.1 | 9765.1 |
| 1013.25 | 0.0 | 6500.0 | 9500.0 |
| 1003.00 | 281.0 | 6219.0 | 9219.0 |
| 1000.00 | 363.6 | 6136.4 | 9136.4 |
| 993.00 | 557.3 | 5942.7 | 8942.7 |
| 983.00 | 836.0 | 5664.0 | 8664.0 |

Property tests: `fl_to_altitude_on_qnh_ft` decreases when QNH decreases; at QNH 1013.25 it equals `FL * 100` exactly.

### 7.2 Route import (`io/routes.py`)

- **GPX** (library `gpxpy`): use route points if present, otherwise track points; elevation in metres converted to feet.
- **MSFS `.pln`** (XML): read `ATCWaypoint` elements; parse `WorldPosition`, format like `N44° 34' 21.56",E26° 5' 5.06",+004500.00` (degrees, minutes, seconds; altitude in feet).
- **CSV**: header `name,lat,lon,alt_ft` (`alt_ft` may be empty).
- If a waypoint has no altitude, use the cruise altitude given by the user.
- Validate: at least 2 waypoints, latitude in [-90, 90], longitude in [-180, 180].

### 7.3 Geometry (`geo.py`)

- Use `pyproj` and `shapely` 2.
- Project route and airspaces into a local **azimuthal equidistant** CRS centred on the route's mid point (`+proj=aeqd +lat_0=... +lon_0=... +datum=WGS84 +units=m`). Accurate enough for routes up to about 500 km.
- Densify the route: one sample every 0.25 NM along each leg (geodesic interpolation with `pyproj.Geod`), keeping cumulative distance in NM.
- Altitude along a leg: v0.1 uses the altitude of the leg's start waypoint (flat legs; climbs and descents come in v0.2).
- Lateral distance to an airspace: 0 if the point is inside the polygon, otherwise the distance to the polygon boundary in NM (1 NM = 1852 m).

**Reference values for tests** (WGS84 geodesic, tolerance ±1 m):
- 26.0E, 44.0N to 26.0E, 45.0N: 111,122.0 m (60.001 NM).
- 26.0E, 44.5N to 27.0E, 44.5N: 79,529.1 m (42.942 NM).

### 7.4 Analysis (`analysis.py`)

For each sample point (position, altitude `a` in ft on QNH) and each airspace with real limits `L` (lower) and `U` (upper) of the day:

- `d_h` = lateral distance in NM (0 when inside the polygon).
- `d_v` = 0 if `L <= a <= U`, otherwise `L - a` (below) or `a - U` (above), in ft.
- **INSIDE** if `d_h == 0` and `d_v == 0`.
- **CLOSE** if not inside but within the expanded volume: `d_h <= 2.0` NM and `L - 200 <= a <= U + 200`.
- Otherwise **CLEAR**.

Map states to event kinds using `config/severity.yaml`:

```yaml
clearance_required:      # INSIDE -> INFRINGEMENT, CLOSE -> WARNING
  classes: [A, B, C, D]
  types: [P, R]
requirement:             # INSIDE -> REQUIREMENT (transponder or radio), CLOSE ignored
  types: [TMZ, RMZ]
info:                    # INSIDE -> INFO, CLOSE ignored
  types: [D, ATZ]
```

Margins (2 NM, 200 ft) are configurable but default to the Take 2 values. Consecutive samples with the same airspace and the same kind are merged into one `Event`.

**Reference test cases** (fictional airspace, square 20 km x 20 km centred on 44.5N, 26.0E, class D TMA, lower FL65, upper FL195; straight route through the centre at 6000 ft):

| QNH | Base on QNH | Expected |
|---|---|---|
| 1013.25 | 6500.0 ft | no event (500 ft below) |
| 1000.00 | 6136.4 ft | WARNING, min_vertical_ft ≈ 136.4 |
| 993.00 | 5942.7 ft | INFRINGEMENT |

Lateral cases (fictional CTR, class D, SFC to 3500 ft MSL; route parallel to one side at 2500 ft):
- offset 1.5 NM outside: WARNING, `min_lateral_nm` ≈ 1.5;
- offset 2.5 NM outside: no event.

### 7.5 Profile data (`profile.py`)

Return data ready for a chart:
- aircraft line: list of `(distance_nm, alt_ft)`;
- for each airspace that is inside or within 2 NM horizontally for part of the route: segments `(start_nm, end_nm, lower_ft, upper_ft, kind)`;
- the margin band (200 ft) drawn under each lower limit.

### 7.6 Scenarios (`scenarios.py`, files in `scenarios/*.yaml`)

Each scenario contains: `id`, `title`, `learning_goal`, `qnh_hpa`, `route`, `airspaces` (fictional, `fictional: true`), `question` (`multiple_choice` or `numeric` with tolerance), `answer`, `explanation` (markdown).

The first three scenarios:
1. **Low QNH under a flight-level base.** TMA base FL65, QNH 993, planned 6000 ft. Question: is 6000 ft below the TMA today? Answer: no, the base is about 5943 ft; with the Take 2 margin the highest planned altitude is about 5740 ft, so plan 5500 ft.
2. **Clipping a CTR corner.** Route passes 1.2 NM from a CTR boundary at 2500 ft (CTR SFC to 3500 ft). Question: does the route respect Take 2? Answer: no, move the turning point so the route stays at least 2 NM away.
3. **Climbing too early under a TMA.** Departure under a TMA with base 4500 ft MSL, planned cruise 5500 ft. Question: until where must you stay at or below 4300 ft? Answer: until 2 NM beyond the TMA lateral boundary.

## 8. Interfaces

**CLI** (`typer`):
```
take2 fetch-airspace --country RO           # downloads openAIP data into data/
take2 check route.gpx --qnh 998 --cruise 4500 [--airspace data/ro.json]
take2 scenario list
take2 scenario run low-qnh-fl-base
```
`check` prints a table of events and a short summary.

**Web app** (`app/streamlit_app.py`):
- sidebar: upload route, QNH, cruise altitude, data source (Romania or a scenario);
- map (`streamlit-folium`): route, airspaces coloured by kind, events highlighted;
- profile chart (`plotly`);
- events table;
- QNH effect panel;
- scenario mode: question, answer, reveal explanation;
- footer: "Not for navigation" and openAIP attribution.

## 9. Tech stack

- Python 3.12, environment and dependencies with `uv`.
- `pydantic`, `shapely`, `pyproj`, `gpxpy`, `pyyaml`, `typer`, `streamlit`, `streamlit-folium`, `folium`, `plotly`, `httpx`.
- Quality: `pytest`, `hypothesis`, `ruff` (lint and format).
- GitHub Actions: run `ruff check`, `ruff format --check` and `pytest` on every push and pull request.

## 10. Repository structure

```
take2/
  src/take2/
    __init__.py
    models.py
    altimetry.py
    geo.py
    analysis.py
    profile.py
    scenarios.py
    cli.py
    io/
      openaip.py
      routes.py
  app/streamlit_app.py
  config/severity.yaml
  scenarios/                 # three YAML scenarios
  tests/
    fixtures/                # small fictional GPX, PLN, CSV and airspace files
    test_altimetry.py
    test_routes.py
    test_geo.py
    test_openaip.py
    test_analysis.py
    test_profile.py
    test_scenarios.py
  docs/
    METHOD.md                # formulas and assumptions
    LIMITATIONS.md
    DATA_LICENSES.md
  .github/workflows/ci.yml
  .gitignore                 # includes data/ and .env
  pyproject.toml
  README.md
  SPEC.md
  ROADMAP.md
  LICENSE                    # MIT
```

## 11. Milestones (each one = a few small commits)

1. **Setup:** `pyproject.toml`, `uv`, `ruff`, `pytest`, CI workflow, `.gitignore`, MIT licence, README skeleton.
2. **Altimetry:** `altimetry.py` with the reference tests from 7.1.
3. **Models and route import:** `models.py`, `io/routes.py`, fixtures and tests.
4. **Geometry:** projection, densification, lateral distance, tests from 7.3.
5. **openAIP loader:** `io/openaip.py` plus `fetch-airspace`; tests on a small fictional fixture in openAIP format.
6. **Analysis:** states, severity config, events, tests from 7.4.
7. **Profile and CLI:** `profile.py`, `take2 check`.
8. **Web app:** map, profile, events, QNH panel.
9. **Scenarios:** three YAML files, scenario mode in CLI and web app.
10. **Docs and release:** README with screenshots, METHOD, LIMITATIONS, DATA_LICENSES, ROADMAP; tag `v0.1.0`; open GitHub issues for the roadmap items.

## 12. Definition of done for v0.1

- All tests pass in CI; all reference values in this spec are covered by tests.
- `take2 check` works on a real route around Bucharest with real openAIP data.
- The web app runs locally with `uv run streamlit run app/streamlit_app.py`.
- Three scenarios can be played in the web app.
- README explains the problem, shows screenshots, how to install and run, the method, the limitations and the data attribution.
- No API keys, no personal data and no full real datasets in the repository.

## 13. Rules for the AI coding agent

1. Read this file before each task and work one milestone at a time.
2. For any calculation (altimetry, geometry, analysis), write the tests first, using the reference values from this spec. Never change a reference value to make a test pass; report the discrepancy instead.
3. Small commits with clear messages (Conventional Commits, for example `feat(altimetry): convert FL to altitude on QNH`).
4. Put units in names (`alt_ft`, `dist_nm`, `qnh_hpa`). Type hints and short docstrings everywhere.
5. No secrets in the repository; no real personal data (no aircraft registrations, no pilot names).
6. Do not add features outside the current milestone. Write new ideas into `ROADMAP.md`.
7. Keep README and docs updated when behaviour changes.
8. When something about aviation rules is uncertain, say so in `docs/LIMITATIONS.md` instead of guessing.

## 14. Roadmap after v0.1 (to become GitHub issues)

- Terrain elevation (Copernicus DEM) for GND-referenced limits and a terrain line in the profile.
- Climb and descent with aircraft performance and wind: suggested top of climb and top of descent points that respect Take 2.
- Temperature correction for altitudes.
- Little Navmap `.lnmpln` import.
- "Plan it yourself" quiz: the user marks climb and descent points, Take2 grades the plan.
- Debrief: compare a flown simulator track (GPX) with the plan and list where the margins were lost.
- More countries (Bulgaria, Hungary, then Europe).
- PDF briefing export.
- More scenarios based on the causal factors in UK CAA CAP3277.
- Romanian and English interface.
