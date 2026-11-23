# SPDX-License-Identifier: AGPL-3.0-or-later
"""OVERVIEW: User Rules / Filter. Hides search results by two user rules: a list
of blocked hostnames and a limit on how many known tracker hosts a result page
may load. It reads the table Tracker Lens leaves on the request and makes no
network calls of its own.

Logic by Chrishun Smith (CSCI 7130), fitted to the Tracker Lens data contract:
the table is indexed by result.parsed_url.netloc + result.parsed_url.path, and a
Report lists its trackers as (host, owner, categories) tuples.
"""


def is_blocked(hostname, blocked):
    """EFFECTS: Returns true iff hostname is a nonempty entry of blocked or a subdomain of one."""
    if not hostname:
        return False
    return any(hostname == b or hostname.endswith("." + b) for b in blocked if b)


def filter_results(results, blocked_hosts, tracker_reports, max_trackers=None):
    """
    REQUIRES: every result has a parsed_url attribute (a ParseResult or None);
       tracker_reports maps a Tracker Lens key to a Report, with no Future left.
    EFFECTS: Raises ValueError if max_trackers is neither None nor a nonnegative
       int. Otherwise returns (visible_results, visible_tracker_reports).
       visible_results is results in the given order, without those whose
       hostname is blocked and, if max_trackers is not None, without those whose
       fetched Report lists more than max_trackers distinct tracker hosts.
       visible_tracker_reports is the entries of tracker_reports that belong to
       the results kept. A result with no parsed_url is kept. A result with no
       Report, or a Report that was not fetched, is never hidden by max_trackers:
       its tracker count is unknown. Modifies nothing.
    """
    if max_trackers is not None:
        if type(max_trackers) is not int or max_trackers < 0:  # pylint: disable=unidiomatic-typecheck
            raise ValueError("max_trackers must be a nonnegative integer or None")

    blocked = {host.strip().lower() for host in blocked_hosts}
    visible_results = []
    visible_tracker_reports = {}

    for result in results:
        if result.parsed_url is None:
            visible_results.append(result)
            continue

        if is_blocked(result.parsed_url.hostname, blocked):
            continue

        report_key = result.parsed_url.netloc + result.parsed_url.path
        report = tracker_reports.get(report_key)

        if max_trackers is not None and report is not None and report.fetched:
            unique_trackers = {host for host, _owner, _categories in report.trackers}

            if len(unique_trackers) > max_trackers:
                continue

        visible_results.append(result)

        if report is not None:
            visible_tracker_reports[report_key] = report

    return visible_results, visible_tracker_reports
