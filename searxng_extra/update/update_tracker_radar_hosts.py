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

# NUMBERING KEY for the code sketch comments (CoDAT "Sketch" format, CSX.Y.Z).
#   X = the class, Y = the function in file order, Z = the step inside it.
#   "# CSX.Y: name - purpose" opens a body and is followed by the list of steps.
#   Each step is then repeated, in more detail, directly above the code that does it.
#   This script has no classes, so every number starts with CS1.
#
#   CS1  module-level functions
#        CS1.1 sparse_checkout    CS1.2 build_host_map
#        CS1.3 the script body under "if __name__ == '__main__'", numbered as a function

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from searx.data import data_dir

DATA_FILE = data_dir / 'tracker_radar_hosts.json'  # The file this script writes and tracker_lens.init reads
REPO_URL = 'https://github.com/duckduckgo/tracker-radar'  # Where Tracker Radar is cloned from
DOMAINS_DIR = Path('domains') / 'US'  # The one folder of Tracker Radar the host map is built from


def sparse_checkout(target: Path) -> Path:
    """
    REQUIRES: target is an existing, writable directory; ``git`` is on the PATH and
       GitHub can be reached.
    MODIFIES: the file system below target
    EFFECTS: Downloads only the ``domains/US`` folder of Tracker Radar, at its newest
       commit, into target/tracker-radar and returns that directory. Raises
       subprocess.CalledProcessError if a git command fails.
    HELPS: the script body, when no checkout path is given on the command line
    """
    # CS1.1: sparse_checkout - Fetches one folder of Tracker Radar without downloading the whole repository.
    # CS1.1.1: Clone the repository without any of its files.
    # CS1.1.2: Tell git that only domains/US is wanted.
    # CS1.1.3: Check the files out, which downloads just that folder.

    # CS1.1.1: --filter=blob:none and --no-checkout fetch no file contents, and --depth 1 fetches
    #          only the newest commit: the full repository is hundreds of MB. check=True makes a
    #          failed git command raise, which stops the script.
    repo = target / 'tracker-radar'
    subprocess.run(
        ['git', 'clone', '--filter=blob:none', '--no-checkout', '--depth', '1', REPO_URL, str(repo)],
        check=True,
    )

    # CS1.1.2: sparse-checkout limits the working tree to the named folder.
    subprocess.run(['git', '-C', str(repo), 'sparse-checkout', 'set', str(DOMAINS_DIR)], check=True)

    # CS1.1.3: Only now are file contents downloaded, and only for domains/US.
    subprocess.run(['git', '-C', str(repo), 'checkout'], check=True)
    return repo


def build_host_map(domains_dir: Path) -> dict[str, list]:
    """
    REQUIRES: domains_dir is a directory of Tracker Radar domain files, one JSON
       object per file.
    EFFECTS: Returns the host map described in the module docstring for the domain
       files in domains_dir: {domain: [owner display name or "", categories]} for
       every file that has an owner name or at least one category. Reads the
       files and changes nothing.
    HELPS: the script body; the result is what tracker_lens.init loads as HOSTMAP
    """
    # CS1.2: build_host_map - Boils the per-domain files down to the three fields the lens needs.
    # CS1.2.1: Read each domain file, in name order.
    # CS1.2.2: Take the owner's display name and the categories, with empty defaults.
    # CS1.2.3: Skip a domain that has neither.
    # CS1.2.4: Store the entry under the domain name.

    # CS1.2.1: Sorted, so that two runs over the same data build the same map.
    hosts: dict[str, list] = {}
    for path in sorted(domains_dir.glob('*.json')):
        with path.open(encoding='utf-8') as f:
            entry = json.load(f)

        # CS1.2.2: "or" supplies the default when a field is missing or null: the owner may be absent,
        #          empty, or have no displayName.
        owner = (entry.get('owner') or {}).get('displayName') or ''
        categories = entry.get('categories') or []

        # CS1.2.3: Tracker Radar lists every third-party domain its crawl saw. One with no known owner
        #          and no category is not an identified tracker.
        if not owner and not categories:
            continue  # Not an identified tracker

        # CS1.2.4: The file's own "domain" field is the key; the file name is the fallback.
        hosts[entry.get('domain') or path.stem] = [owner, categories]
    return hosts


if __name__ == '__main__':
    # CS1.3: script body - Builds the host map and writes it where the plugin reads it.
    # CS1.3.1: Build the map from a checkout named on the command line, or from a fresh one.
    # CS1.3.2: Write the map as compact JSON.
    # CS1.3.3: Say how many hosts were written.

    # CS1.3.1: A fresh checkout goes into a temporary directory, which is deleted when the with
    #          block ends.
    if len(sys.argv) > 1:
        host_map = build_host_map(Path(sys.argv[1]) / DOMAINS_DIR)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            host_map = build_host_map(sparse_checkout(Path(tmp)) / DOMAINS_DIR)

    # CS1.3.2: sort_keys keeps the file stable from run to run (small diffs in git), the separators
    #          leave out spaces to keep it small, and ensure_ascii=False keeps owner names readable.
    with DATA_FILE.open('w', encoding='utf-8') as out:
        json.dump(host_map, out, separators=(',', ':'), sort_keys=True, ensure_ascii=False)

    # CS1.3.3: The count is the quick check that the run worked (8,845 in the file in the repo).
    print(f"{len(host_map)} hosts written to {DATA_FILE}")
