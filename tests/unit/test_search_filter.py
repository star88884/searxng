# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring,missing-class-docstring

import unittest
from collections import namedtuple
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlparse

import babel

import searx.search
from searx.plugins.tracker_lens import Report as LensReport
from searx.result_types import MainResult
from searx.search_filter import filter_results, is_blocked
from tests import SearxTestCase

Report = namedtuple("Report", "fetched trackers")  # The two Tracker Lens Report fields the filter reads


def _result(title, url):
    return SimpleNamespace(title=title, parsed_url=urlparse(url) if url else None)


def _trackers(*hosts):
    return [(host, "Owner", ["Advertising"]) for host in hosts]


class TestSearchFilter(unittest.TestCase):

    def test_sample_data(self):
        """Chrishun's sample: a blocked host and a page over the tracker limit are hidden."""
        results = [
            _result("Python lessons", "https://learn.example/python"),
            _result("Blocked website", "https://www.blocked.example/python"),
            _result("Many trackers", "https://tracking.example/python"),
            _result("Scan timed out", "https://unknown.example/python"),
            _result("No url", None),
        ]
        reports = {
            "learn.example/python": Report(True, _trackers("analytics.example", "ads.example")),
            "www.blocked.example/python": Report(True, []),
            "tracking.example/python": Report(True, _trackers("analytics.example", "ads.example", "metrics.example")),
            "unknown.example/python": Report(False, []),
        }

        visible, visible_reports = filter_results(results, {" Blocked.example "}, reports, 2)
        self.assertEqual([r.title for r in visible], ["Python lessons", "Scan timed out", "No url"])
        self.assertEqual(set(visible_reports), {"learn.example/python", "unknown.example/python"})

        # No rules: nothing is hidden.
        self.assertEqual(filter_results(results, [], reports)[0], results)

    def test_invalid_max_trackers(self):
        for bad in (-1, 1.5, True, "2"):
            with self.assertRaises(ValueError):
                filter_results([], [], {}, bad)

    def test_is_blocked(self):
        """A host is blocked by its own entry or a parent's, not by a child's or a look-alike's."""
        self.assertTrue(is_blocked("example.com", {"example.com"}))
        self.assertTrue(is_blocked("www.example.com", {"example.com"}))
        self.assertFalse(is_blocked("example.com", {"www.example.com"}))
        self.assertFalse(is_blocked("example.com", {"ample.com"}))
        self.assertFalse(is_blocked(None, {"example.com"}))

    def test_empty_entry_blocks_nothing(self):
        """When no hosts are given the filter receives one empty entry; it must hide nothing."""
        results = [_result("Host written with a trailing dot", "https://example.com./")]
        for blocked_hosts in ([""], [" "], ["other.example", ""]):
            self.assertEqual(filter_results(results, blocked_hosts, {})[0], results)

    def test_limit(self):
        """The limit counts distinct tracker hosts: a page at the limit stays, a page over it goes."""
        page = _result("Page", "https://user@page.example:8443/a;b?c#d")
        key = "user@page.example:8443/a"  # netloc + path, the string tracker_lens.key(result) builds
        for hosts, limit, kept in (
            (("a.x", "b.x"), 2, True),
            (("a.x", "b.x", "c.x"), 2, False),
            (("a.x", "a.x", "a.x"), 1, True),
            ((), 0, True),
            (("a.x",), 0, False),
            (("a.x",), None, True),
        ):
            visible, visible_reports = filter_results([page], [], {key: Report(True, _trackers(*hosts))}, limit)
            self.assertEqual(visible, [page] if kept else [], (hosts, limit))
            self.assertEqual(list(visible_reports), [key] if kept else [], (hosts, limit))

    def test_unknown_count(self):
        """A page with no Report is never over the limit; a blocked host is hidden all the same."""
        results = [_result("No report", "https://a.example/"), _result("Blocked, no report", "https://b.example/")]
        visible, visible_reports = filter_results(results, ["b.example"], {}, 0)
        self.assertEqual([r.title for r in visible], ["No report"])
        self.assertEqual(visible_reports, {})

    def test_modifies_nothing(self):
        results = [_result("A", "https://a.example/"), _result("B", "https://b.example/")]
        blocked_hosts = [" B.example "]
        reports = {"a.example/": Report(True, _trackers("a.x"))}
        filter_results(results, blocked_hosts, reports, 0)
        self.assertEqual([r.title for r in results], ["A", "B"])
        self.assertEqual(blocked_hosts, [" B.example "])
        self.assertEqual(reports, {"a.example/": Report(True, _trackers("a.x"))})


class TestSearchFilterHook(SearxTestCase):
    """The rules as searx/webapp.py search() reads them from the request. The search itself is mocked."""

    def setUp(self):
        super().setUp()
        results = [
            MainResult(title="First", url="http://first.test.xyz", content="one", engine="startpage"),
            MainResult(title="Second", url="http://second.test.xyz", content="two", engine="youtube"),
        ]
        for result in results:
            result.normalize_result_fields()
        # What Tracker Lens leaves on the request; None stands for a request it left nothing on.
        self.table = {
            "first.test.xyz": LensReport(True, _trackers("a.example", "b.example"), 2, ""),
            "second.test.xyz": LensReport(True, [], 0, ""),
        }

        def search_mock(search_self, *args):  # pylint: disable=unused-argument
            search_self.result_container = Mock(
                get_ordered_results=lambda: results,
                answers={},
                corrections=set(),
                suggestions=set(),
                infoboxes=[],
                unresponsive_engines=set(),
                get_timings=lambda: [],
                redirect_url=None,
                engine_data={},
            )
            search_self.search_query.locale = babel.Locale.parse("en-US", sep='-')
            if self.table is not None:
                search_self.request.tracker_lens = self.table

        self.setattr4test(searx.search.Search, 'search', search_mock)

    def test_hook_no_rules(self):
        page = self.client.get('/search?q=test').data
        self.assertIn(b'first.test.xyz', page)
        self.assertIn(b'second.test.xyz', page)

    def test_hook_blocked_host_hidden(self):
        """A blocked host is gone from the result list and from the graph data."""
        page = self.client.get('/search?q=test&blocked_hosts=first.test.xyz').data
        self.assertNotIn(b'first.test.xyz', page)
        self.assertIn(b'second.test.xyz', page)

    def test_hook_over_limit_hidden(self):
        """A page over the tracker limit is gone from the result list and from the graph data."""
        page = self.client.post('/search', data={'q': 'test', 'max_trackers': '1'}).data
        self.assertNotIn(b'first.test.xyz', page)
        self.assertIn(b'second.test.xyz', page)

    def test_hook_bad_limit_ignored(self):
        page = self.client.get('/search?q=test&max_trackers=abc').data
        self.assertIn(b'first.test.xyz', page)
        self.assertIn(b'second.test.xyz', page)

    def test_hook_long_limit_ignored(self):
        """int() raises on a string of more than 4300 digits; the search must still answer."""
        response = self.client.get('/search?q=test&max_trackers=' + '9' * 5000)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'first.test.xyz', response.data)

    def test_hook_without_tracker_lens(self):
        """Tracker Lens left nothing on the request: no error, and blocked hosts still work."""
        self.table = None
        response = self.client.get('/search?q=test&max_trackers=0&blocked_hosts=first.test.xyz')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'first.test.xyz', response.data)
        self.assertIn(b'second.test.xyz', response.data)
