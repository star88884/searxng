# SearXNG detecting trackers (CSCI 7130 team project)

Tanya Starker, Allen Donald and Chrishun Smith

This branch (`demo/all-three`) extends [SearXNG](https://github.com/searxng/searxng) so that the results page
shows, before any click, which known trackers each result page loads, which results share trackers, and lets
the user hide results by rule. The search engines are not changed: our code works only on results the engines
have already returned.

The `master` branch of this fork is unmodified SearXNG at commit `54ca1d9cf`, so the difference between `master`
and `demo/all-three` is our work.

## The three modules

| Module | Owner | What it does | Files |
|---|---|---|---|
| Tracker Lens | Allen Donald | Fetches each result page, finds the known tracker hosts in its HTML, and shows a badge and a "Who is tracking you here" panel under the result | `searx/plugins/tracker_lens.py`, `searx/data/tracker_radar_hosts.json`, `searxng_extra/update/update_tracker_radar_hosts.py`, `searx/templates/simple/macros.html` (badge block), `searx/settings.yml` (switches the plugin on), `tests/unit/test_plugin_tracker_lens.py` |
| User Rules / Filter | Chrishun Smith | Hides results by two address rules, `blocked_hosts` and `max_trackers` | `searx/search_filter.py`, `searx/webapp.py` (reads the rules), `tests/unit/test_search_filter.py` |
| Graph View | Tanya Starker | Draws the pages of the current search as a graph, joining two pages that load a tracker host in common | `searx/tracker_graph.py`, `client/simple/src/js/plugin/TrackerGraph.ts`, `client/simple/src/js/router.ts`, `searx/templates/simple/results.html`, `searx/webapp.py` (builds the graph data), `client/simple/package.json` (Cytoscape.js), `tests/test_tracker_graph.py` |

The modules share one table: for each result page, the Tracker Lens stores a Report (fetched, trackers,
third-party host count, error). The Filter keeps the Reports of the results it did not hide, and Graph View is
built from those.

## Run it

```bash
git clone https://github.com/star88884/searxng.git
cd searxng && git checkout demo/all-three
make install                                          # builds the Python environment in local/py3
SEARXNG_DEBUG=1 local/py3/bin/python -m searx.webapp  # then open http://127.0.0.1:8888
```

## Run the tests

```bash
SEARXNG_DEBUG=1 local/py3/bin/python -m nose2 -s tests/unit test_plugin_tracker_lens   # 37 tests
SEARXNG_DEBUG=1 local/py3/bin/python -m nose2 -s tests/unit test_search_filter         # 13 tests
local/py3/bin/python -m pytest -q tests/test_tracker_graph.py                          # 5 tests
```

No test uses the network. The drawing code in `TrackerGraph.ts` has no automated test.

## The two address rules

Add them to the address of a search, for example
`http://127.0.0.1:8888/search?q=python+tutorial&blocked_hosts=w3schools.com&max_trackers=2`

- `blocked_hosts`: site names separated by commas. A result whose host is one of them, or a subdomain of one,
  is hidden.
- `max_trackers`: a whole number. A result is hidden when its page was read and loads more than that many known
  tracker hosts. A page that could not be read is never hidden by this rule, because its count is unknown.
  A value that is not a whole number of 1 to 9 digits is ignored.

## Open source we used (not our contribution)

| What | Licence | How we used it |
|---|---|---|
| SearXNG, base commit `54ca1d9cf` | GNU AGPL v3 | The search engine we extended: everything outside the files listed above |
| DuckDuckGo Tracker Radar | CC BY-NC-SA 4.0 | Source of `tracker_radar_hosts.json`. The data is theirs; the script that extracts it is ours |
| Cytoscape.js 3.34.4 | MIT | Draws the graph |
| curl_cffi 0.16.3 | MIT | Already a SearXNG dependency; the Lens imports one exception class from it |
| Flask-Babel 4.0.0 | BSD-3-Clause | Already a SearXNG dependency; marks the Lens's two texts for translation |
| Python standard library | PSF License | `html.parser`, `ipaddress`, `socket`, `concurrent.futures`, `json` and others |

The Tracker Radar licence allows non-commercial use with attribution. Commercial use of this extension would
need the list's terms settled with DuckDuckGo first.

## Known limits

- The Lens reads the `script` and `iframe` tags of the HTML the server fetched. Trackers added by JavaScript
  after the page loads, image pixels, and pages that refuse the fetch are not seen, so its count is a lower bound.
- Scans run one at a time, so a first search shows many badges as "not checked"; a repeat search fills them in.
- The rules live in the address. A new query typed into the search box does not carry them over.
- Pages are told apart by host and path, so results that differ only after the `?` share one Report.
- A result whose address has no host name gets no badge.
- `registrable` uses a short hand-kept list of two-part suffixes, not the Public Suffix List.
