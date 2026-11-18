"""Tests for the take2 command line (SPEC.md section 8).

No test touches the network: ``fetch_airspaces`` is replaced by a fake that returns the
fictional fixture, or the real one runs over an ``httpx.MockTransport``. The API key is a
made-up string.
"""

import json
import re
from collections.abc import Callable
from importlib.metadata import entry_points
from pathlib import Path

import httpx
import pytest
import typer
from typer.testing import CliRunner

from take2 import cli
from take2.io import openaip
from take2.io.openaip import (
    ATTRIBUTION,
    OpenAipFetchError,
    load_openaip_file,
    parse_openaip_airspaces,
)

FIXTURE = Path(__file__).parent / "fixtures" / "openaip_airspaces.json"
FAKE_KEY = "Zq8Wv3Lm7Tx2Rk9Pd4Hs6Jy1Nc5Bg0Fa"  # made up; shares no 6 characters with fixtures
runner = CliRunner()

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def plain(text: str) -> str:
    """Remove ANSI styling: typer forces colours when GITHUB_ACTIONS is set (as on CI)."""
    return _ANSI_ESCAPE_RE.sub("", text)


type FetchCalls = list[tuple[str, str]]


def fixture_items() -> list[dict[str, object]]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["items"]


def run(*args: str):  # noqa: ANN201 - typer's Result type is not part of its public API
    return runner.invoke(cli.app, list(args))


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty current directory and no key in the environment."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAIP_API_KEY", raising=False)
    return tmp_path


@pytest.fixture
def with_key(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAIP_API_KEY", FAKE_KEY)


@pytest.fixture
def fake_fetch(monkeypatch: pytest.MonkeyPatch) -> FetchCalls:
    """Replace the download with the fixture's items; record (country, key) of each call."""
    calls: FetchCalls = []

    def fetch(country: str, api_key: str, **kwargs: object) -> list[dict[str, object]]:
        calls.append((country, api_key))
        return fixture_items()

    monkeypatch.setattr(cli, "fetch_airspaces", fetch)
    return calls


def failing_fetch(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    def fetch(country: str, api_key: str, **kwargs: object) -> list[dict[str, object]]:
        raise error

    monkeypatch.setattr(cli, "fetch_airspaces", fetch)


def serve(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]):
    """Keep the real fetch_airspaces, but give its own httpx client a mock transport."""
    real_client = httpx.Client

    def make_client(**kwargs: object) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(openaip.httpx, "Client", make_client)


# --- the app ----------------------------------------------------------------------


def test_help_lists_fetch_airspace() -> None:
    result = run("--help")

    assert result.exit_code == 0
    assert "fetch-airspace" in plain(result.output)


def test_fetch_airspace_help_documents_its_options() -> None:
    result = run("fetch-airspace", "--help")

    assert result.exit_code == 0
    assert "--country" in plain(result.output)
    assert "--output" in plain(result.output)


def test_tracebacks_never_show_local_variables() -> None:
    # A local variable of the command holds the API key; rich tracebacks could print it.
    assert cli.app.pretty_exceptions_show_locals is False


def test_take2_is_installed_as_a_console_script() -> None:
    (script,) = entry_points(group="console_scripts", name="take2")

    assert script.value == "take2.cli:app"


# --- fetch-airspace: success ------------------------------------------------------


@pytest.mark.usefixtures("with_key")
def test_fetch_airspace_saves_the_items_to_data_country_json(
    workdir: Path, fake_fetch: FetchCalls
) -> None:
    result = run("fetch-airspace", "--country", "RO")

    assert result.exit_code == 0, result.output
    assert fake_fetch == [("RO", FAKE_KEY)]
    saved = load_openaip_file(workdir / "data" / "ro.json")
    assert [a.name for a in saved.airspaces] == [
        "FICTIONAL ALPHA CTR",
        "FICTIONAL BRAVO TMA",
        "FICTIONAL CHARLIE TMZ",
        "FICTIONAL ECHO P",
    ]
    assert saved.skipped == 1
    document = json.loads((workdir / "data" / "ro.json").read_text(encoding="utf-8"))
    assert document["items"] == fixture_items()


@pytest.mark.usefixtures("with_key")
def test_fetch_airspace_reports_the_count_the_skips_and_the_attribution(
    fake_fetch: FetchCalls,
) -> None:
    result = run("fetch-airspace", "--country", "RO")

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        f"Saved 5 openAIP airspaces for RO to {Path('data', 'ro.json')}.",
        "1 item cannot be used by Take2 and will be skipped.",
        "  item 4 ('FICTIONAL DELTA BROKEN') skipped: geometry type 'Point' is not supported "
        "(only Polygon)",
        ATTRIBUTION,
    ]
    assert result.stderr == ""


@pytest.mark.usefixtures("with_key")
def test_fetch_airspace_reports_airspaces_read_with_a_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = fixture_items()[:2]
    items[0]["icaoClass"] = 7  # not an openAIP class code
    items[1]["type"] = "TMA"  # not a code at all
    monkeypatch.setattr(cli, "fetch_airspaces", lambda country, api_key: items)

    result = run("fetch-airspace", "--country", "RO")

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[1] == "2 items will be used with a default for a field Take2 cannot read."
    assert lines[2] == (
        "  item 1 ('FICTIONAL ALPHA CTR'): icaoClass is not a known openAIP code (7), "
        "read as unclassified"
    )
    assert lines[3].startswith("  item 2 ('FICTIONAL BRAVO TMA'): type is not")
    assert lines[4] == ATTRIBUTION


@pytest.mark.usefixtures("with_key")
def test_fetch_airspace_shows_at_most_five_problems(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken = [{"_id": f"broken-{n}", "name": f"FICTIONAL {n}"} for n in range(8)]
    monkeypatch.setattr(cli, "fetch_airspaces", lambda country, api_key: broken)

    result = run("fetch-airspace", "--country", "RO")

    assert result.exit_code == 0, result.output
    problem_lines = [line for line in result.stdout.splitlines() if line.startswith("  item ")]
    assert len(problem_lines) == 5
    assert "  ... and 3 more" in result.stdout.splitlines()


@pytest.mark.usefixtures("with_key")
def test_the_hidden_problem_count_includes_skipped_and_defaulted_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template = fixture_items()[1]  # the TMA
    broken = [{"_id": f"broken-{n}", "name": f"FICTIONAL {n}"} for n in range(4)]
    odd = [{**template, "_id": f"odd-{n}", "icaoClass": 7} for n in range(4)]
    monkeypatch.setattr(cli, "fetch_airspaces", lambda country, api_key: broken + odd)

    result = run("fetch-airspace", "--country", "RO")

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[1:3] == [
        "4 items cannot be used by Take2 and will be skipped.",
        "4 items will be used with a default for a field Take2 cannot read.",
    ]
    assert len([line for line in lines if line.startswith("  item ")]) == 5
    assert "  ... and 3 more" in lines


@pytest.mark.usefixtures("with_key")
def test_an_unencodable_name_is_saved_and_reported_without_a_crash(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = {"_id": "broken", "name": "FICTIONAL" + chr(0xD800)}  # no limits, no geometry
    monkeypatch.setattr(cli, "fetch_airspaces", lambda country, api_key: [broken])

    result = run("fetch-airspace", "--country", "RO")

    assert result.exit_code == 0, result.output
    assert "  item 1 ('FICTIONAL?') skipped: " in result.stdout
    assert (workdir / "data" / "ro.json").is_file()


@pytest.mark.usefixtures("with_key")
def test_the_country_is_upper_cased_and_the_file_name_lower_cased(
    workdir: Path, fake_fetch: FetchCalls
) -> None:
    result = run("fetch-airspace", "--country", "ro")

    assert result.exit_code == 0, result.output
    assert fake_fetch == [("RO", FAKE_KEY)]
    assert (workdir / "data" / "ro.json").is_file()


@pytest.mark.usefixtures("with_key", "fake_fetch")
def test_output_option_chooses_the_file_and_creates_its_directories(workdir: Path) -> None:
    target = workdir / "elsewhere" / "nested" / "romania.json"

    result = run("fetch-airspace", "--country", "RO", "--output", str(target))

    assert result.exit_code == 0, result.output
    assert len(load_openaip_file(target).airspaces) == 4
    assert not (workdir / "data").exists()
    assert str(target) in result.stdout


@pytest.mark.usefixtures("with_key", "fake_fetch")
def test_an_existing_file_is_replaced_and_no_temporary_file_is_left(workdir: Path) -> None:
    (workdir / "data").mkdir()
    (workdir / "data" / "ro.json").write_text("old", encoding="utf-8")

    result = run("fetch-airspace", "--country", "RO")

    assert result.exit_code == 0, result.output
    assert len(load_openaip_file(workdir / "data" / "ro.json").airspaces) == 4
    assert [p.name for p in (workdir / "data").iterdir()] == ["ro.json"]


def test_the_key_can_come_from_dot_env_in_the_current_directory(
    workdir: Path, fake_fetch: FetchCalls
) -> None:
    (workdir / ".env").write_text(f'OPENAIP_API_KEY="{FAKE_KEY}"\n', encoding="utf-8")

    result = run("fetch-airspace", "--country", "RO")

    assert result.exit_code == 0, result.output
    assert fake_fetch == [("RO", FAKE_KEY)]
    assert FAKE_KEY not in result.output


# --- fetch-airspace: failures -----------------------------------------------------


def assert_clean_failure(result, *, expected: str) -> None:  # noqa: ANN001
    """Exit code 1, the message on stderr, no traceback and never the key."""
    assert result.exit_code == 1, result.output
    assert expected in result.stderr
    assert result.stderr.startswith("Error: ")
    assert "Traceback" not in result.output
    assert FAKE_KEY not in result.output


def test_a_missing_key_exits_1_with_instructions(workdir: Path, fake_fetch: FetchCalls) -> None:
    result = run("fetch-airspace", "--country", "RO")

    assert_clean_failure(result, expected="no openAIP API key found")
    assert "OPENAIP_API_KEY" in result.stderr
    assert ".env" in result.stderr
    assert fake_fetch == []
    assert list(workdir.iterdir()) == []


@pytest.mark.usefixtures("with_key")
@pytest.mark.parametrize("country", ["ROU", "R", "../x", "R1"])
def test_an_invalid_country_exits_1_before_any_download(
    workdir: Path, fake_fetch: FetchCalls, country: str
) -> None:
    result = run("fetch-airspace", "--country", country)

    assert_clean_failure(result, expected="two-letter ISO 3166-1 country code")
    assert fake_fetch == []
    assert list(workdir.iterdir()) == []


def test_country_is_required(workdir: Path) -> None:
    result = run("fetch-airspace")

    assert result.exit_code == 2  # usage error
    assert "--country" in plain(result.output)


@pytest.mark.usefixtures("with_key")
@pytest.mark.parametrize("status", [401, 403, 404])
def test_a_rejected_key_exits_1_with_the_hint_once_and_saves_nothing(
    workdir: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    serve(monkeypatch, lambda request: httpx.Response(status))  # the real fetch_airspaces

    result = run("fetch-airspace", "--country", "RO")

    assert_clean_failure(result, expected=f"HTTP {status}")
    assert result.stderr.count("check OPENAIP_API_KEY") == 1
    assert list(workdir.iterdir()) == []


@pytest.mark.usefixtures("with_key")
@pytest.mark.parametrize(
    "failure",
    ["invalid country", "unreadable .env", "download", "unusable key", "empty", "save"],
)
def test_a_failure_exit_carries_no_exception_context(
    workdir: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    # typer.Exit raised inside an except block would keep the caught error (and through
    # its traceback, frames holding the key) as __context__.
    country, output = "RO", None
    if failure == "invalid country":
        country = "ROU"
    elif failure == "unreadable .env":
        monkeypatch.delenv("OPENAIP_API_KEY")
        (workdir / ".env").write_text('OPENAIP_API_KEY="unclosed\n', encoding="utf-8")
    elif failure == "download":
        failing_fetch(monkeypatch, OpenAipFetchError("openAIP request failed with HTTP 500"))
    elif failure == "unusable key":
        failing_fetch(monkeypatch, ValueError("the openAIP API key is empty"))
    elif failure == "empty":
        monkeypatch.setattr(cli, "fetch_airspaces", lambda country, api_key: [])
    else:
        monkeypatch.setattr(cli, "fetch_airspaces", lambda country, api_key: fixture_items())
        output = workdir / "taken"
        output.mkdir()

    with pytest.raises(typer.Exit) as excinfo:
        cli.fetch_airspace(country=country, output=output)

    assert excinfo.value.exit_code == 1
    assert excinfo.value.__context__ is None


@pytest.mark.usefixtures("with_key")
@pytest.mark.parametrize(
    "error",
    [
        OpenAipFetchError("openAIP request failed with HTTP 500", 500),
        OpenAipFetchError("openAIP did not answer in time; try again later"),
        ValueError("the openAIP API key contains spaces, control or non-ASCII characters"),
    ],
    ids=["server error", "timeout", "unusable key"],
)
def test_a_failed_download_exits_1_and_saves_nothing(
    workdir: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    failing_fetch(monkeypatch, error)

    result = run("fetch-airspace", "--country", "RO")

    assert_clean_failure(result, expected=str(error))
    assert "check OPENAIP_API_KEY" not in result.stderr
    assert list(workdir.iterdir()) == []


@pytest.mark.usefixtures("with_key")
def test_an_empty_download_does_not_overwrite_existing_data(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (workdir / "data").mkdir()
    (workdir / "data" / "ro.json").write_text("previous data", encoding="utf-8")
    monkeypatch.setattr(cli, "fetch_airspaces", lambda country, api_key: [])

    result = run("fetch-airspace", "--country", "RO")

    assert_clean_failure(result, expected="openAIP returned no airspaces for RO")
    assert (workdir / "data" / "ro.json").read_text(encoding="utf-8") == "previous data"


@pytest.mark.usefixtures("with_key", "fake_fetch")
def test_a_failed_write_exits_1_and_leaves_no_temporary_file(workdir: Path) -> None:
    target = workdir / "taken"
    target.mkdir()  # a directory where the file should go

    result = run("fetch-airspace", "--country", "RO", "--output", str(target))

    assert_clean_failure(result, expected=f"cannot save {target}")
    assert list(target.iterdir()) == []
    assert [p.name for p in workdir.iterdir()] == ["taken"]


def test_an_unreadable_dot_env_exits_1_without_echoing_it(
    workdir: Path, fake_fetch: FetchCalls
) -> None:
    (workdir / ".env").write_text(f'OPENAIP_API_KEY="{FAKE_KEY}\n', encoding="utf-8")

    result = run("fetch-airspace", "--country", "RO")

    assert_clean_failure(result, expected="no closing quote")
    assert_no_key_fragment(result.output)
    assert fake_fetch == []


# --- the real fetch_airspaces over a mock transport -------------------------------


def assert_no_key_fragment(text: str) -> None:
    """No 6-character piece of the key appears in ``text``."""
    leaked = [FAKE_KEY[i : i + 6] for i in range(len(FAKE_KEY) - 5) if FAKE_KEY[i : i + 6] in text]
    assert not leaked, f"key fragments shown: {leaked[:3]}"


def paged(
    items: list[dict[str, object]], per_page: int
) -> Callable[[httpx.Request], httpx.Response]:
    """A handler serving ``items`` like the openAIP API, ``per_page`` items per page."""
    pages = [items[i : i + per_page] for i in range(0, len(items), per_page)] or [[]]

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        body: dict[str, object] = {"page": page, "items": pages[page - 1]}
        if page < len(pages):
            body["nextPage"] = page + 1
        return httpx.Response(200, json=body)

    return handler


def refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError(f"connection refused for {FAKE_KEY}")


REAL_FETCH_CASES = {
    "success over three pages": (paged(fixture_items(), 2), 0),
    "401": (lambda request: httpx.Response(401), 1),
    "404": (lambda request: httpx.Response(404), 1),
    "connect error quoting the key": (refuse, 1),
}


@pytest.mark.usefixtures("with_key")
@pytest.mark.parametrize(
    ("handler", "exit_code"), REAL_FETCH_CASES.values(), ids=REAL_FETCH_CASES.keys()
)
def test_the_key_is_never_shown_or_saved(
    workdir: Path,
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
    exit_code: int,
) -> None:
    serve(monkeypatch, handler)

    result = run("fetch-airspace", "--country", "RO")

    assert result.exit_code == exit_code, result.output
    assert_no_key_fragment(result.output)  # stdout and stderr together
    written = [path for path in workdir.rglob("*") if path.is_file()]
    assert len(written) == (1 if exit_code == 0 else 0)
    for path in written:
        assert_no_key_fragment(path.read_bytes().decode("utf-8", "replace"))


@pytest.mark.usefixtures("with_key")
def test_download_save_and_load_give_the_same_airspaces_as_the_items(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(monkeypatch, paged(fixture_items(), 3))

    result = run("fetch-airspace", "--country", "RO")

    assert result.exit_code == 0, result.output
    loaded = load_openaip_file(workdir / "data" / "ro.json")
    assert loaded == parse_openaip_airspaces(fixture_items())
