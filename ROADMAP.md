# Roadmap

Milestones 1 to 7 are done: altimetry, route import, geometry, the openAIP loader, the
analysis, the profile data and the `take2 check` command. This file lists what is left for
v0.1 and the ideas planned after it. [SPEC.md](SPEC.md) is the full plan (sections 11, 12
and 14).

## Remaining milestones for v0.1

### Milestone 8: Web app

A simple Streamlit app in `app/streamlit_app.py`, run with
`uv run streamlit run app/streamlit_app.py`:

- sidebar: upload a route, QNH, cruise altitude;
- map (`streamlit-folium`): the route, the airspaces coloured by kind, the events
  highlighted;
- vertical profile chart (`plotly`): the aircraft altitude and the band of each airspace,
  converted with the day's QNH;
- events table;
- "QNH effect" panel: for each flight-level-based limit, the real altitude today versus at
  1013.25 hPa;
- footer: "Not for navigation" and the openAIP attribution.

### Milestone 9: Scenarios

Three training scenarios with fictional airspace in `scenarios/*.yaml`, loaded by
`scenarios.py`:

1. **Low QNH under a flight-level base:** TMA base FL65, QNH 993, planned 6000 ft.
2. **Clipping a CTR corner:** the route passes 1.2 NM from a CTR boundary at 2500 ft.
3. **Climbing too early under a TMA:** departure under a TMA with base 4500 ft MSL, planned
   cruise 5500 ft.

The scenarios can be played from the CLI (`take2 scenario list`, `take2 scenario run ...`)
and from the web app (question, answer, explanation).

### Milestone 10: Docs and release

- README with screenshots;
- `docs/METHOD.md` (formulas and assumptions), `docs/LIMITATIONS.md`,
  `docs/DATA_LICENSES.md`;
- tag `v0.1.0`.

## After v0.1

- Terrain elevation (Copernicus DEM) for GND-referenced limits and a terrain line in the
  profile.
- Climb and descent with aircraft performance and wind: suggested top of climb and top of
  descent points that respect Take 2.
- Temperature correction for altitudes.
- Little Navmap `.lnmpln` import.
- "Plan it yourself" quiz: the user marks climb and descent points, Take2 grades the plan.
- Debrief: compare a flown simulator track (GPX) with the plan and list where the margins
  were lost.
- More countries (Bulgaria, Hungary, then Europe).
- PDF briefing export.
- More scenarios based on the causal factors in UK CAA CAP3277.
- Romanian and English interface.

Each item has a GitHub issue.
