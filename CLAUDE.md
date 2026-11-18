# Take2: instructions for AI coding agents

Take2 is a VFR airspace-infringement **training** tool (not for navigation): load a route, set the
day's QNH, and see which airspaces the route crosses and where it breaks the "Take 2" margin
(2 NM laterally, 200 ft vertically).

## Source of truth

- [SPEC.md](SPEC.md) defines scope, formulas, reference values and milestones. Read the relevant
  sections before each task. Section 13 lists the rules for AI agents; they all apply.
- Work one milestone at a time (SPEC.md section 11). Do not add features from later milestones;
  write new ideas into `ROADMAP.md` instead.

## Commands

```sh
uv sync                         # install Python 3.12 and dependencies
uv run pytest                   # all tests
uv run ruff check .             # lint
uv run ruff format .            # format (CI runs `ruff format --check .`)
```

Before every commit, run the same three checks as CI: `ruff check`, `ruff format --check`, `pytest`.

## Rules

- **Stay inside the project.** The main agent and every subagent work only inside this
  repository (including its `.claude/worktrees/`). Do not read, create or change files
  elsewhere on the machine; put scratch files in a git-ignored folder inside the repository.
  Make no network calls unless the owner asks for one (for example `take2 fetch-airspace`).

- **Tests first** for any calculation (altimetry, geometry, analysis): write the tests from the
  SPEC.md reference values, watch them fail, then implement.
- **Never change a reference value** from SPEC.md to make a test pass. Report the discrepancy.
- **Units in names**: `alt_ft`, `dist_nm`, `qnh_hpa`, `ground_elev_ft`. Type hints and short
  docstrings everywhere.
- **Tests never depend on real data.** Use small fictional fixtures in `tests/fixtures/`.
  Downloaded openAIP data lives in `data/` (git-ignored). No API keys, no real personal data.
- **Aviation uncertainty** goes into `docs/LIMITATIONS.md`; do not guess silently.
- **Conventional Commits**, small commits (e.g. `feat(geo): densify route legs`).
- Everything in the repository (code, comments, docs, commit messages) is in English.

## Conventions already in place

- Domain models are pydantic v2 in `src/take2/models.py`; they validate their own invariants.
- Unit conversions live in `src/take2/altimetry.py` (`FT_PER_M`, `FT_PER_FL`) and
  `src/take2/geo.py` (`M_PER_NM`); reuse them.
- Geometry: distances along the route are WGS84 geodesics; lateral distances are measured in
  `geo.LocalProjection` (azimuthal equidistant, centred on the route midpoint).
- Import errors are domain exceptions that subclass `ValueError` and name the failing line or
  item (see `RouteImportError` in `src/take2/io/routes.py`).
- Files from users are untrusted. XML goes through `routes._parse_xml` (an expat check with
  no DTD, bounded depth and element count, then ElementTree on the same text). Do not add
  parsers that rewrite or re-read input after the check (this is why gpxpy was removed).
- `SPEC.md` is excluded from `ruff format`, so its snippets stay as written.
- CI pins `astral-sh/setup-uv` by commit SHA: it publishes no moving major tag such as `v10`.
