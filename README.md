# Take2

Take2 is an open-source training tool that helps VFR pilots (and flight-sim pilots) avoid
airspace infringements. Load a route, set the day's QNH, and Take2 shows which airspaces you
pass, converts flight-level-based limits to the real altitude of the day, and warns you when
you do not keep the "Take 2" safety margin (2 NM laterally, 200 ft vertically).

> **Not for navigation.** Take2 is a training and learning tool only. Airspace data may be
> incomplete or wrong. Always use official AIP data and approved navigation tools for real
> flights.

**Status:** early development (v0.1 in progress). See [SPEC.md](SPEC.md) for the plan.

## Route files

Take2 reads routes from:

- **GPX** 1.0 or 1.1: route points if the file has a route, otherwise track points.
  Elevations (metres) are converted to feet.
- **MSFS `.pln`**: the `ATCWaypoint` elements of the flight plan. An altitude of
  `+000000.00` means "not set".
- **CSV**: header `name,lat,lon,alt_ft`, decimal degrees, altitude in feet AMSL.
  `alt_ft` may be left empty.

Waypoints without an altitude get the cruise altitude you enter. A route needs at least two
waypoints. Route files are limited to 10 MB, 50,000 waypoints and 1,000 NM.

See [docs/LIMITATIONS.md](docs/LIMITATIONS.md) for what Take2 does not model yet.

## Airspace data (openAIP)

Take2 uses airspace data from [openAIP](https://www.openaip.net), which needs a free openAIP
account and its API key.

1. Copy `.env.example` to `.env` and put your key after `OPENAIP_API_KEY=`. You can also set
   the `OPENAIP_API_KEY` environment variable, which is used instead of `.env`. `.env` is
   git-ignored: never commit a key.
2. Download the airspaces of Romania:

   ```sh
   uv run take2 fetch-airspace --country RO
   ```

   This saves `data/ro.json` (also git-ignored); `--output PATH` saves elsewhere. The command
   prints how many airspaces it saved, lists the ones Take2 cannot use (for example a
   geometry other than a polygon) and the ones it reads with a default (for example an
   unknown airspace class). The file is readable only by you. Run the command again to
   refresh the data.

openAIP data is maintained by its community and is not the official AIP; see
[docs/LIMITATIONS.md](docs/LIMITATIONS.md).

## Checking a route

```sh
uv run take2 check route.gpx --qnh 998 --cruise 4500
```

`--qnh` is the QNH of the day in hPa (850 to 1100), `--cruise` the altitude in ft AMSL used for
waypoints without one, and `--airspace PATH` another airspace file (default `data/ro.json`).
The command prints one line per event along the route, then a summary:

| Event | Meaning |
|---|---|
| INFRINGEMENT | inside airspace that needs a clearance (classes A to D, P, R) |
| WARNING | not inside, but within the Take 2 margins (2 NM laterally, 200 ft vertically) |
| REQUIREMENT | inside a TMZ or RMZ (transponder or radio required) |
| INFO | inside a danger area or ATZ |

Flight-level limits are converted with the QNH you give, so a low QNH brings a flight-level
base down. A `~` after a vertical distance means a ground-referenced limit was used with the
terrain assumed at 0 ft.

## Development

Requirements: [uv](https://docs.astral.sh/uv/). uv installs Python 3.12 automatically.

```sh
uv sync                     # create .venv and install dependencies
uv run pytest               # run the tests
uv run ruff check .         # lint
uv run ruff format .        # format
```

## Data attribution

Airspace data © openAIP contributors (openaip.net), CC BY-NC 4.0.

## Licence

Code: MIT, see [LICENSE](LICENSE).
