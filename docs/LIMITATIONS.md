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
- **Unknown type codes are kept** as `TYPE_<n>` (for example `TYPE_37`). Such an airspace is
  loaded, but no rule in the severity configuration matches it.
- **Class 8 means unclassified.** openAIP code 8 ("Unclassified / Special Use Airspace")
  becomes "no ICAO class", so only the airspace type decides whether a clearance is needed.
- **Skipped items.** An item is skipped, counted and reported (the first 20 are described)
  instead of failing the whole file when:
  - it is not a JSON object, or `_id` or `name` is missing or not a non-empty string;
  - `type` or `icaoClass` is missing or not a whole number of at least 0 (`true`/`false`
    do not count), `type` is 1000 or more, or `icaoClass` is not 0 to 6 or 8;
  - `lowerLimit` or `upperLimit` is missing, its `value` is not a finite number, its `unit`
    is not 0 (metres), 1 (feet) or 6 (flight level), or its `referenceDatum` is not
    0 (GND), 1 (MSL) or 2 (STD);
  - the geometry is not a single-ring `Polygon`, a position is not exactly `[lon, lat]`,
    a coordinate is out of range, or the closed ring has fewer than 4 positions;
  - its `_id` was already seen earlier in the file.
- **Strict JSON.** A file or API page with `NaN`, `Infinity`, a number too large for a
  float, or nesting deeper than Python's JSON parser allows is rejected as a whole.
- **Open question: unlimited upper limits.** How openAIP writes "UNL" is not verified.
  Take2 uses the value exactly as published.
- **API behaviour seen on 2026-11-18:** the airspaces endpoint answers HTTP 403 without a
  key and HTTP 404 ("Failed to load user permissions") for a key it does not know, so Take2
  treats a 404 as a probable key problem. A redirect is never followed, so the key is never
  sent to another host.

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
| openAIP download: pages | 100 (1,000 items requested per page) |
| openAIP download: one page | 50 MB; compressed responses are refused |
| openAIP download: all pages together | 100 MB |
| openAIP download: time for one page | 120 s |
| openAIP request timeouts | 10 s to connect, 30 s per read or write |
| `.env` file | 1 MB |
| openAIP API key | 512 visible ASCII characters, no spaces |
