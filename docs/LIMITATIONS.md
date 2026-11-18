# Limitations

Take2 is a **training tool, not a navigation tool**. Airspace data may be incomplete or wrong.
Always use official AIP data and approved navigation tools for real flights.

This page lists what the current version does not model, the limits it enforces, and open
questions that are not decided yet.

## Altimetry

- **ISA only, no temperature correction.** Flight levels are converted to altitude on QNH
  with the ISA troposphere formula. In cold air your true altitude is lower than indicated;
  Take2 does not correct for that.
- **No plausibility check on QNH yet.** A QNH typed in the wrong unit (29.92 inHg or
  101325 Pa) is not rejected by the calculation code.

## Ground-referenced limits

- **No terrain data.** A limit referenced to the ground (`GND`/`AGL`) is converted with a
  ground elevation of 0 ft unless one is given, and the result is marked *approximate*.
- **Surface limits are exact.** A lower limit of `GND`/`SFC` with value 0 means the airspace
  reaches the ground wherever the ground is, so it is not marked approximate.
- **Open question:** whether a ground-referenced limit should still count as approximate when
  a ground elevation *is* supplied. This matters once terrain data is added (roadmap).

## Route altitudes

- **Flat legs.** Each leg is flown at the altitude of its start waypoint. Climbs and descents
  are not modelled, and the altitude of the last waypoint is not used.
- **Departure at field elevation.** If the first waypoint carries the airport elevation (for
  example the departure airport of an MSFS `.pln`), the first leg is analysed at that
  elevation, not at the altitude you intend to climb to. How Take2 should treat this is still
  open.
- **MSFS `.pln` altitude 0.** `+000000.00` is read as "no altitude", so the waypoint gets the
  cruise altitude. A waypoint really planned at 0 ft (sea level) cannot be expressed in `.pln`.
- **GPX with several routes.** The points of all routes are joined in file order.
- **GPX contents read.** Only route points (`rte/rtept`) or, without a route, track points
  (`trk/trkseg/trkpt`) of GPX 1.0 or 1.1 are read. Standalone waypoints (`wpt`) are ignored.
- **Numbers must be plain decimals** (for example `44.5`, `-26.1`, `4.45e1`). Values such as
  `4_4.5`, `nan`, `inf` or digits from other scripts are rejected instead of being guessed.

## Geometry

- Distances along the route are WGS84 geodesics. Lateral distances to airspaces are measured
  in a local azimuthal equidistant projection centred on the point halfway along the route.
  Measured error on a 2 NM lateral distance, across the direction to the centre (the worst
  case): 0.6 m at 200 km, 3.3 m at 463 km and 13 m at 926 km from the centre, which is the
  farthest a 1,000 NM route reaches. Routes up to about 500 km (270 NM) stay within 1 m.
- Airspace edges are straight lines between their published vertices in that projection.
  An edge meant to follow a parallel of latitude or a border over a long distance is only as
  accurate as the vertices that describe it.

## openAIP airspace data

- **Not the official AIP.** openAIP data is maintained by its community and can be
  incomplete, outdated or wrong. Download it again before a planning session.
- **Always active.** Take2 does not read activation times, hours of operation, NOTAM
  activation or "on request"/"on demand" flags (`hoursOfOperation`, `activeFrom`,
  `activeUntil`, `byNotam`, `onRequest`, `onDemand`). A TRA, TSA or danger area is treated as
  active all the time.
- **Fields not read:** `upperLimitMax`, `lowerLimitMin`, `activity`, `frequencies`,
  `transponderSettings`, `remarks` and `country`.
- **Only single-ring polygons.** openAIP describes an airspace as a GeoJSON `Polygon` with
  one ring. An item with another geometry type, or with a hole, is skipped. An open ring is
  closed by repeating its first position. Whether a polygon crosses itself is not checked.
- **Skipped only when Take2 cannot know where the airspace is.** An item is skipped,
  counted and reported instead of failing the whole file when:
  - it is not a JSON object;
  - `lowerLimit` or `upperLimit` is missing, its `value` is not a finite number, its `unit`
    is not 0 (metres), 1 (feet) or 6 (flight level), or its `referenceDatum` is not
    0 (GND), 1 (MSL) or 2 (STD);
  - the geometry is not a single-ring `Polygon`, a position is not exactly `[lon, lat]`,
    a coordinate is outside -180 to 180 (lon) or -90 to 90 (lat), or the closed ring has
    fewer than 4 or more than 20,000 positions;
  - it repeats an earlier item exactly (same `_id`, same content).
- **Read with a default rather than dropped.** A field that only describes the airspace
  never makes it vanish (a missing airspace would let a check report a false "clear").
  The item is counted as read with a default and reported:
  - no usable `_id`: `openaip-item-<n>`, from its position in the file;
  - no usable `name`: the id;
  - a missing or invalid `type` (not a whole number from 0 to 999): `OTHER`;
  - an `icaoClass` that is not an openAIP class code (0 to 6, 8): no class;
  - an `_id` already used by a different item: the same id with `#2`, `#3`, ...
- **No class means unclassified.** A missing or null `icaoClass` (optional in the openAIP
  schema) and code 8 ("Unclassified / Special Use Airspace") give "no ICAO class", so only
  the airspace type decides whether a clearance is needed. An unknown class code is treated
  the same way and reported, not guessed: such an airspace may in fact need a clearance.
- **Unknown type codes are kept** as `TYPE_<n>` (for example `TYPE_37`). Such an airspace is
  loaded, but no rule in the severity configuration matches it.
- **Names and ids are cleaned.** They are cut to 256 characters, and control, format,
  surrogate, private-use, unassigned and line or paragraph separator characters become `?`.
  The saved file keeps the raw items exactly as downloaded (written as ASCII JSON).
- **Data files are private** to the user who downloaded them: `take2 fetch-airspace` writes
  them with file mode 0600 (read and write for the owner only).
- **Problem reports.** Every skipped or defaulted item is counted; the first 20 are
  described in the result, and the command line shows the first 5.
- **Strict JSON.** A file or API page with `NaN`, `Infinity`, a number too large for a
  float, or nesting deeper than Python's JSON parser allows is rejected as a whole.
- **Open question: unlimited upper limits.** How openAIP writes "UNL" is not verified.
  Take2 uses the value exactly as published (the test data stands in with FL 999 STD).
- **API behaviour seen on 2026-11-18:** the airspaces endpoint answers HTTP 403 without a
  key and HTTP 404 ("Failed to load user permissions") for a key it does not know, so Take2
  treats a 404 as a probable key problem. A redirect is never followed, so the key is never
  sent to another host.
- **The API key stays out of every message.** It is sent only in the `x-openaip-api-key`
  header, never in a URL; error messages are built by Take2 and redacted, and no network
  exception (which holds the request headers) is attached to them.

## Input limits

These keep the work bounded when a route file or airspace data comes from someone else.

XML is checked with the expat library bundled with Python; version 2.7.2 or later is
required (a test checks this).

| Limit | Value |
|---|---|
| Route file size | 10 MB |
| Waypoints per route | 50,000 |
| Waypoint name | 256 characters |
| XML nesting depth (GPX, `.pln`) | 64 elements |
| XML elements per file (GPX, `.pln`) | 500,000 |
| XML attributes per element (GPX, `.pln`) | 64 |
| Distinct XML element and attribute names (GPX, `.pln`) | 1,000 |
| CSV fields per row | 64 |
| Line breaks inside quoted CSV fields | not allowed |
| DOCTYPE / DTD in GPX or `.pln` | not allowed |
| Route length | 1,000 NM |
| Route samples | 100,000 |
| openAIP file size (`load_openaip_file`) | 100 MB |
| openAIP items per file | 100,000 (100 pages of 1,000) |
| openAIP polygon ring | 20,000 positions after closing |
| openAIP airspace name and id | 256 characters (longer ones are cut) |
| openAIP download: pages | 100 (1,000 items requested per page) |
| openAIP download: items per page | 1,000 |
| openAIP download: one page | 50 MB; compressed responses are refused |
| openAIP download: all pages together | 100 MB |
| openAIP download: time for one page | 120 s |
| openAIP request timeouts | 10 s to connect, 30 s per read or write |
| `.env` file | 1 MB |
| openAIP API key | 512 visible ASCII characters, no spaces |
