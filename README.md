# Take2

Take2 is an open-source training tool that helps VFR pilots (and flight-sim pilots) avoid
airspace infringements. Load a route, set the day's QNH, and Take2 shows which airspaces you
pass, converts flight-level-based limits to the real altitude of the day, and warns you when
you do not keep the "Take 2" safety margin (2 NM laterally, 200 ft vertically).

> **Not for navigation.** Take2 is a training and learning tool only. Airspace data may be
> incomplete or wrong. Always use official AIP data and approved navigation tools for real
> flights.

**Status:** early development (v0.1 in progress). See [SPEC.md](SPEC.md) for the plan.

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
