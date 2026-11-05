#!/usr/bin/env python
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Flatten the DuckDuckGo `Tracker Radar`_ domain files into one host map for
the ``tracker_lens`` plugin.

Output file: :origin:`searx/data/tracker_radar_hosts.json`, a map from a
registrable domain to ``[owner, categories]``.

OVERVIEW: Tracker Radar keeps one JSON file per third-party domain its crawl
   saw, under ``domains/US/<domain>.json``, with the owner, the categories and
   crawl statistics. The plugin needs only three fields of each file, and only
   for domains that are identified trackers.
REQUIRES: ``git`` on the PATH and network access to GitHub, or a path to an
   existing Tracker Radar checkout as the first command line argument.
MODIFIES: ``searx/data/tracker_radar_hosts.json``; a temporary directory that
   is removed before the script exits.
EFFECTS: Writes a JSON object with one entry per Tracker Radar domain file whose
   owner has a display name or whose category list is not empty:
   ``{domain: [owner display name or "", categories]}``. Every domain with
   neither an owner name nor a category is left out. Keys are sorted. Nothing
   else of Tracker Radar is kept.

.. _Tracker Radar: https://github.com/duckduckgo/tracker-radar

"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from searx.data import data_dir

DATA_FILE = data_dir / 'tracker_radar_hosts.json'
REPO_URL = 'https://github.com/duckduckgo/tracker-radar'
DOMAINS_DIR = Path('domains') / 'US'


def sparse_checkout(target: Path) -> Path:
    """EFFECTS: Checks out only ``domains/US`` of Tracker Radar below target
    and returns the repository directory."""
    repo = target / 'tracker-radar'
    subprocess.run(
        ['git', 'clone', '--filter=blob:none', '--no-checkout', '--depth', '1', REPO_URL, str(repo)],
        check=True,
    )
    subprocess.run(['git', '-C', str(repo), 'sparse-checkout', 'set', str(DOMAINS_DIR)], check=True)
    subprocess.run(['git', '-C', str(repo), 'checkout'], check=True)
    return repo


def build_host_map(domains_dir: Path) -> dict[str, list]:
    """EFFECTS: Returns the host map described in the module docstring for the
    domain files in domains_dir."""
    hosts: dict[str, list] = {}
    for path in sorted(domains_dir.glob('*.json')):
        with path.open(encoding='utf-8') as f:
            entry = json.load(f)
        owner = (entry.get('owner') or {}).get('displayName') or ''
        categories = entry.get('categories') or []
        if not owner and not categories:
            continue
        hosts[entry.get('domain') or path.stem] = [owner, categories]
    return hosts


if __name__ == '__main__':
    if len(sys.argv) > 1:
        host_map = build_host_map(Path(sys.argv[1]) / DOMAINS_DIR)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            host_map = build_host_map(sparse_checkout(Path(tmp)) / DOMAINS_DIR)
    with DATA_FILE.open('w', encoding='utf-8') as out:
        json.dump(host_map, out, separators=(',', ':'), sort_keys=True, ensure_ascii=False)
    print(f"{len(host_map)} hosts written to {DATA_FILE}")
