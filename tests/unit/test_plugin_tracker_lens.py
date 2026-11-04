# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring,disable=missing-class-docstring,invalid-name,protected-access

import concurrent.futures
import json
import pathlib
import tempfile
import threading
import time
from unittest import mock

import flask

import searx.network
import searx.plugins
import searx.preferences
from searx.extended_types import sxng_request
from searx.plugins import tracker_lens
from searx.plugins.tracker_lens import Report
from searx.result_types import MainResult, Result

from tests import SearxTestCase

HOSTMAP = {
    "google-analytics.com": ["Google", ["Analytics"]],
    "doubleclick.net": ["Google", ["Advertising"]],
    "jsdelivr.net": ["Prospect One", ["CDN"]],
}

PAGE_URL = "https://www.augusta.edu/"
PAGE = """<!doctype html><html><head>
<script src="/static/site.js"></script>
<script src="https://www.google-analytics.com/analytics.js"></script>
<script src="//cdn.unknown-third-party.example/lib.js"></script>
</head><body>
<iframe src="https://ad.doubleclick.net/frame"></iframe>
</body></html>"""
PAGE_TRACKERS = [
    ("ad.doubleclick.net", "Google", ["Advertising"]),
    ("www.google-analytics.com", "Google", ["Analytics"]),
]

MACRO = "{% from 'simple/macros.html' import result_sub_header with context %}{{ result_sub_header(result) }}"


def response(status=200, content_type="text/html; charset=utf-8", text=PAGE):
    return mock.Mock(status_code=status, headers={"content-type": content_type}, text=text)


def main_result(url):
    result = MainResult(title="t", url=url, content="c", engine="startpage")
    result.normalize_result_fields()
    return result


class PluginTrackerLensTest(SearxTestCase):  # pylint: disable=too-many-public-methods

    def setUp(self):
        super().setUp()
        self.storage = searx.plugins.PluginStorage()
        self.storage.load_settings({"searx.plugins.tracker_lens.SXNGPlugin": {"active": True}})
        self.plugin = next(iter(self.storage))
        self.pref = searx.preferences.Preferences(["simple"], ["general"], {}, self.storage)
        self.pref.parse_dict({"locale": "en"})

        tracker_lens.HOSTMAP.clear()
        tracker_lens.HOSTMAP.update(HOSTMAP)
        tracker_lens.ACTIVE = True
        with tracker_lens.LOCK:
            tracker_lens.CACHE.clear()
            tracker_lens.STAMP.clear()
        tracker_lens.POOL = concurrent.futures.ThreadPoolExecutor(2)
        self.addCleanup(tracker_lens.POOL.shutdown)

        self.get = mock.patch("searx.network.get", return_value=response()).start()
        self.addCleanup(mock.patch.stopall)

        self.tmp = pathlib.Path(tempfile.mkdtemp())

    def hostmap_file(self, text):
        path = self.tmp / "tracker_radar_hosts.json"
        if text is not None:
            path.write_text(text, encoding="utf-8")
        self.setattr4test(tracker_lens, "HOSTMAP_FILE", path)
        return path

    def render(self, result):
        sxng_request.preferences = self.pref
        return flask.render_template_string(MACRO, result=result)

    # init

    def test_init_returns_false_when_hostmap_missing(self):
        self.hostmap_file(None)
        self.assertFalse(self.plugin.init(self.app))
        self.assertEqual(tracker_lens.HOSTMAP, {})

    def test_init_returns_false_when_hostmap_malformed(self):
        self.hostmap_file("{not json")
        self.assertFalse(self.plugin.init(self.app))
        self.assertEqual(tracker_lens.HOSTMAP, {})

    def test_init_returns_false_when_hostmap_empty(self):
        self.hostmap_file("{}")
        self.assertFalse(self.plugin.init(self.app))
        self.assertEqual(tracker_lens.HOSTMAP, {})

    def test_init_loads_hostmap(self):
        self.hostmap_file(json.dumps(HOSTMAP))
        tracker_lens.HOSTMAP.clear()
        self.assertTrue(self.plugin.init(self.app))
        self.assertEqual(tracker_lens.HOSTMAP, HOSTMAP)
        self.assertEqual(tracker_lens.CACHE, {})
        self.assertIsInstance(tracker_lens.POOL, concurrent.futures.ThreadPoolExecutor)
        self.addCleanup(tracker_lens.POOL.shutdown)

    # scan

    def test_scan_returns_cached_report_without_fetch(self):
        report = Report(True, PAGE_TRACKERS, 3, "")
        with tracker_lens.LOCK:
            tracker_lens.CACHE[PAGE_URL] = report
            tracker_lens.STAMP[PAGE_URL] = time.monotonic()
        self.assertIs(tracker_lens.scan(PAGE_URL), report)
        self.get.assert_not_called()

    def test_scan_unsafe_host_is_not_fetched(self):
        for url in ("http://127.0.0.1/admin", "http://10.0.0.8/", "http://169.254.169.254/latest", "http://[::1]/"):
            self.assertEqual(tracker_lens.scan(url), Report(False, [], 0, "unsafe host"))
            self.assertEqual(tracker_lens.CACHE[url], Report(False, [], 0, "unsafe host"))
        self.get.assert_not_called()

    def test_scan_timeout_gives_unfetched_report(self):
        self.get.side_effect = searx.network.Timeout("Timeout")
        with self.app.test_request_context():
            self.assertTrue(self.plugin.on_result(sxng_request, None, main_result(PAGE_URL)))
        self.assertEqual(tracker_lens.scan(PAGE_URL), Report(False, [], 0, "timeout"))
        self.assertEqual(tracker_lens.CACHE[PAGE_URL], Report(False, [], 0, "timeout"))

    def test_scan_connect_error_gives_unfetched_report(self):
        self.get.side_effect = ConnectionError("refused")
        self.assertEqual(tracker_lens.scan(PAGE_URL), Report(False, [], 0, "connect"))
        self.assertEqual(tracker_lens.CACHE[PAGE_URL], Report(False, [], 0, "connect"))

    def test_scan_bad_status_gives_unfetched_report(self):
        self.get.return_value = response(status=404)
        self.assertEqual(tracker_lens.scan(PAGE_URL), Report(False, [], 0, "http 404"))

    def test_scan_non_html_gives_unfetched_report(self):
        self.get.return_value = response(content_type="application/json", text="{}")
        self.assertEqual(tracker_lens.scan(PAGE_URL), Report(False, [], 0, "not html"))

    def test_scan_matches_third_party_hosts(self):
        report = tracker_lens.scan(PAGE_URL)
        self.assertEqual(report, Report(True, PAGE_TRACKERS, 3, ""))
        self.assertEqual(tracker_lens.CACHE[PAGE_URL], report)
        self.get.assert_called_once_with(
            PAGE_URL, timeout=tracker_lens.FETCH_TIMEOUT, allow_redirects=True, raise_for_httperror=False
        )

    def test_scan_evicts_oldest_when_full(self):
        urls = [f"https://site{i}.example/" for i in range(tracker_lens.CACHE_MAX + 1)]
        for url in urls:
            tracker_lens.scan(url)
        self.assertEqual(len(tracker_lens.CACHE), tracker_lens.CACHE_MAX)
        self.assertNotIn(urls[0], tracker_lens.CACHE)
        self.assertIn(urls[1], tracker_lens.CACHE)
        self.assertIn(urls[-1], tracker_lens.CACHE)

    # on_result

    def test_on_result_without_url_changes_nothing(self):
        with self.app.test_request_context():
            self.assertTrue(self.plugin.on_result(sxng_request, None, Result()))
            self.assertFalse(hasattr(sxng_request, "tracker_lens"))
        self.get.assert_not_called()

    def test_on_result_non_http_changes_nothing(self):
        with self.app.test_request_context():
            self.assertTrue(self.plugin.on_result(sxng_request, None, main_result("ftp://files.example/pub/x")))
            self.assertFalse(hasattr(sxng_request, "tracker_lens"))
        self.get.assert_not_called()

    def test_on_result_records_future_and_keeps_result(self):
        pool = mock.Mock(spec=["submit"])
        pool.submit.return_value = concurrent.futures.Future()
        self.setattr4test(tracker_lens, "POOL", pool)
        with self.app.test_request_context():
            result = main_result("https://www.augusta.edu/about/?utm_source=x")
            self.assertTrue(self.plugin.on_result(sxng_request, None, result))
            self.assertIs(sxng_request.tracker_lens["www.augusta.edu/about/"], pool.submit.return_value)
        pool.submit.assert_called_once_with(tracker_lens.scan, result.url)

    def test_on_result_same_key_submits_one_scan(self):
        pool = mock.Mock(spec=["submit"])
        pool.submit.return_value = concurrent.futures.Future()
        self.setattr4test(tracker_lens, "POOL", pool)
        with self.app.test_request_context():
            self.assertTrue(
                self.plugin.on_result(sxng_request, None, main_result("https://www.augusta.edu/about/?a=1"))
            )
            self.assertTrue(
                self.plugin.on_result(sxng_request, None, main_result("https://www.augusta.edu/about/?b=2"))
            )
            self.assertEqual(list(sxng_request.tracker_lens), ["www.augusta.edu/about/"])
        self.assertEqual(pool.submit.call_count, 1)

    # post_search

    def test_post_search_without_table_returns_none(self):
        with self.app.test_request_context():
            self.assertIsNone(self.plugin.post_search(sxng_request, None))
            self.assertFalse(hasattr(sxng_request, "tracker_lens"))

    def test_post_search_fills_finished_reports(self):
        report = Report(True, PAGE_TRACKERS, 3, "")
        done = concurrent.futures.Future()
        done.set_result(report)
        with self.app.test_request_context():
            sxng_request.tracker_lens = {"www.augusta.edu/": done}
            self.assertIsNone(self.plugin.post_search(sxng_request, None))
            self.assertEqual(sxng_request.tracker_lens, {"www.augusta.edu/": report})

    def test_post_search_marks_unfinished_as_timeout(self):
        release = threading.Event()
        self.addCleanup(release.set)
        slow = tracker_lens.POOL.submit(release.wait)
        with self.app.test_request_context():
            sxng_request.tracker_lens = {"slow.example/": slow}
            started = time.monotonic()
            self.assertIsNone(self.plugin.post_search(sxng_request, None))
            elapsed = time.monotonic() - started
            self.assertEqual(sxng_request.tracker_lens, {"slow.example/": Report(False, [], 0, "timeout")})
        self.assertGreaterEqual(elapsed, tracker_lens.BUDGET)
        self.assertLess(elapsed, tracker_lens.BUDGET + 0.5)

    # template

    def test_template_renders_nothing_without_table(self):
        result = main_result(PAGE_URL)
        with self.app.test_request_context():
            without_table = self.render(result)
            sxng_request.tracker_lens = {}
            without_key = self.render(result)
        self.assertNotIn("tracker", without_table)
        self.assertEqual(without_table, without_key)

    def test_template_renders_not_checked_badge(self):
        result = main_result(PAGE_URL)
        with self.app.test_request_context():
            sxng_request.tracker_lens = {"www.augusta.edu/": Report(False, [], 0, "timeout")}
            html = self.render(result)
        self.assertIn('<details class="tracker_lens">', html)
        self.assertIn("trackers: not checked", html)
        self.assertNotIn("<p>", html)
        self.assertNotIn("<li>", html)

    def test_template_renders_badge_and_panel(self):
        result = main_result(PAGE_URL)
        with self.app.test_request_context():
            sxng_request.tracker_lens = {"www.augusta.edu/": Report(True, PAGE_TRACKERS, 3, "")}
            html = self.render(result)
        self.assertIn("2 trackers", html)
        self.assertIn("<p>Who is tracking you here</p>", html)
        self.assertEqual(html.count("<li>"), 2)
        self.assertIn("<li>ad.doubleclick.net: Google (Advertising)</li>", html)
        self.assertIn("<li>www.google-analytics.com: Google (Analytics)</li>", html)

    # representation invariant

    def test_rep_invariant_holds_after_search(self):
        results = [
            main_result(PAGE_URL),
            main_result(PAGE_URL + "?ref=duckduckgo"),
            main_result("https://en.wikipedia.org/wiki/Augusta_University"),
            main_result("http://127.0.0.1/"),
            main_result("ftp://files.example/x"),
            Result(),
        ]
        with self.app.test_request_context():
            for result in results:
                self.assertTrue(self.plugin.on_result(sxng_request, None, result))
            self.assertIsNone(self.plugin.post_search(sxng_request, None))
            table = sxng_request.tracker_lens
        self.assertEqual(len(table), 3)
        for value in table.values():
            self.assertIsInstance(value, Report)
        for report in tracker_lens.CACHE.values():
            if report.fetched:
                for host, owner, cats in report.trackers:
                    self.assertEqual(HOSTMAP[tracker_lens.registrable(host)], [owner, cats])
            else:
                self.assertEqual(report.trackers, [])
                self.assertNotEqual(report.error, "")
        tracker_lens._check_rep(table)

    # adapter for the graph view

    def test_site_trackers_feeds_build_tracker_graph(self):
        table = {
            "www.augusta.edu/": Report(True, PAGE_TRACKERS, 3, ""),
            "usnews.com/best-colleges": Report(False, [], 0, "http 403"),
            "en.wikipedia.org/wiki/Augusta_University": Report(True, [], 0, ""),
        }
        self.assertEqual(
            tracker_lens.site_trackers(table),
            {
                "www.augusta.edu/": ["ad.doubleclick.net", "www.google-analytics.com"],
                "en.wikipedia.org/wiki/Augusta_University": [],
            },
        )
        self.assertEqual(
            list(tracker_lens.site_trackers(table)), ["www.augusta.edu/", "en.wikipedia.org/wiki/Augusta_University"]
        )
