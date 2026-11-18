"""Command line interface: ``take2 <command>`` (SPEC.md section 8).

Training tool only, not for navigation. Each command is a function on ``app``; errors the
user can fix are printed as one ``Error: ...`` line on stderr with exit code 1, never as a
traceback.
"""

from pathlib import Path
from typing import Annotated, NoReturn

import typer

from take2.io.openaip import (
    ATTRIBUTION,
    OpenAipFetchError,
    OpenAipFileError,
    fetch_airspaces,
    normalize_country,
    parse_openaip_airspaces,
    read_api_key,
    save_openaip_file,
)

DATA_DIR = Path("data")  # default location of downloaded data, relative to the current dir
MAX_SHOWN_PROBLEMS = 5  # skipped openAIP items listed after a download

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    # Never print local variables in a traceback: one of them can hold the API key.
    pretty_exceptions_show_locals=False,
)


@app.callback()
def main() -> None:
    """Take2: practise VFR route planning around airspace. Training only, not for navigation."""


@app.command("fetch-airspace")
def fetch_airspace(
    country: Annotated[str, typer.Option(help="ISO 3166-1 alpha-2 country code, for example RO.")],
    output: Annotated[
        Path | None,
        typer.Option(help="File to write. Default: data/<country>.json, e.g. data/ro.json."),
    ] = None,
) -> None:
    """Download the airspaces of one country from openAIP into a JSON file.

    The API key is read from the OPENAIP_API_KEY environment variable or from a .env file
    in the current directory.
    """
    try:
        country = normalize_country(country)
    except ValueError as exc:
        _fail(str(exc))
    try:
        api_key = read_api_key()
    except (OSError, ValueError) as exc:
        _fail(f"cannot read the openAIP API key: {exc}")
    if api_key is None:
        _fail(
            "no openAIP API key found. Set OPENAIP_API_KEY in the environment or in a .env "
            "file in the current directory (see .env.example)."
        )

    try:
        items = fetch_airspaces(country, api_key)
    except OpenAipFetchError as exc:
        hint = " (check OPENAIP_API_KEY)" if exc.status_code in (401, 403) else ""
        _fail(f"{exc}{hint}")
    except ValueError as exc:  # an API key that cannot be sent
        _fail(str(exc))
    if not items:
        _fail(f"openAIP returned no airspaces for {country}; nothing was saved")

    path = output if output is not None else DATA_DIR / f"{country.lower()}.json"
    try:
        save_openaip_file(path, items, country=country)
    except OpenAipFileError as exc:
        _fail(f"cannot save {path}: {exc}")
    except OSError as exc:
        _fail(f"cannot save {path}: {exc.strerror or exc}")

    result = parse_openaip_airspaces(items)
    typer.echo(f"Saved {len(items)} openAIP airspaces for {country} to {path}.")
    if result.skipped:
        typer.echo(f"{result.skipped} of them cannot be used by Take2 and will be skipped:")
        for problem in result.problems[:MAX_SHOWN_PROBLEMS]:
            typer.echo(f"  {problem}")
        if result.skipped > MAX_SHOWN_PROBLEMS:
            typer.echo(f"  ... and {result.skipped - MAX_SHOWN_PROBLEMS} more")
    typer.echo(ATTRIBUTION)


def _fail(message: str) -> NoReturn:
    """Print ``Error: message`` on stderr and exit with code 1."""
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code=1)
