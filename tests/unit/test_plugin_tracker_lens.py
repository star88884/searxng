# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring,disable=missing-class-docstring,invalid-name,protected-access

import concurrent.futures
import json
import pathlib
import shutil
import socket
import tempfile
import threading
import time
from unittest import mock

import flask
from curl_cffi.requests.exceptions import ConnectionError as CurlConnectionError

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


PUBLIC_ADDRESS = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
PRIVATE_ADDRESS = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]


def response(status=200, content_type="text/html; charset=utf-8", text=PAGE, location=None):
    headers = {"content-type": content_type}
    if location is not None:
        headers["location"] = location
    return mock.Mock(status_code=status, headers=headers, text=text)


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
        self.getaddrinfo = mock.patch("socket.getaddrinfo", return_value=PUBLIC_ADDRESS).start()
        self.addCleanup(mock.patch.stopall)

        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

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
        """init EFFECTS, "cannot be read": a missing file gives false and an empty HOSTMAP."""
        self.hostmap_file(None)
        self.assertFalse(self.plugin.init(self.app))
        self.assertEqual(tracker_lens.HOSTMAP, {})

    def test_init_returns_false_when_hostmap_malformed(self):
        """init EFFECTS, "cannot be parsed": text that is not JSON gives false and an empty HOSTMAP."""
        self.hostmap_file("{not json")
        self.assertFalse(self.plugin.init(self.app))
        self.assertEqual(tracker_lens.HOSTMAP, {})

    def test_init_returns_false_when_hostmap_empty(self):
        """init EFFECTS, "holds no entries": an empty JSON object gives false."""
        self.hostmap_file("{}")
        self.assertFalse(self.plugin.init(self.app))
        self.assertEqual(tracker_lens.HOSTMAP, {})

    def test_init_returns_false_when_hostmap_is_a_list(self):
        """init EFFECTS, "cannot be parsed" (amendment 4): a JSON list is not a host map and gives false."""
        self.hostmap_file(json.dumps(list(HOSTMAP.items())))
        self.assertFalse(self.plugin.init(self.app))
        self.assertEqual(tracker_lens.HOSTMAP, {})

    def test_init_returns_false_when_hostmap_entry_malformed(self):
        """init EFFECTS, "an entry whose value is not [owner, categories]" (amendment 19): each gives false."""
        for value in ("Google", ["Google"], [1, ["Analytics"]], ["Google", ["Analytics", 2]]):
            self.hostmap_file(json.dumps({**HOSTMAP, "bad.example": value}))
            self.assertFalse(self.plugin.init(self.app))
            self.assertEqual(tracker_lens.HOSTMAP, {})

    def test_init_loads_hostmap(self):
        """init EFFECTS, "Else sets HOSTMAP from the file": true, the three entries, an empty CACHE, a POOL."""
        self.hostmap_file(json.dumps(HOSTMAP))
        tracker_lens.HOSTMAP.clear()
        self.assertTrue(self.plugin.init(self.app))
        self.assertEqual(tracker_lens.HOSTMAP, HOSTMAP)
        self.assertEqual(tracker_lens.CACHE, {})
        self.assertIsInstance(tracker_lens.POOL, concurrent.futures.ThreadPoolExecutor)
        self.addCleanup(tracker_lens.POOL.shutdown)

    # registrable

    def test_registrable_keeps_three_labels_for_second_level_suffix(self):
        """registrable EFFECTS, "its last three": www.bbc.co.uk gives bbc.co.uk."""
        self.assertEqual(tracker_lens.registrable("www.bbc.co.uk"), "bbc.co.uk")

    # scan

    def test_scan_returns_cached_report_without_fetch(self):
        """scan EFFECTS, "url is in CACHE": the cached Report comes back and no GET is sent."""
        report = Report(True, PAGE_TRACKERS, 3, "")
        with tracker_lens.LOCK:
            tracker_lens.CACHE[PAGE_URL] = report
            tracker_lens.STAMP[PAGE_URL] = time.monotonic()
        self.assertIs(tracker_lens.scan(PAGE_URL), report)
        self.get.assert_not_called()

    def test_scan_refetches_expired_cache_entry(self):
        """scan EFFECTS, "younger than CACHE_TTL": an older entry is fetched again and replaced."""
        with tracker_lens.LOCK:
            tracker_lens.CACHE[PAGE_URL] = Report(False, [], 0, "timeout")
            tracker_lens.STAMP[PAGE_URL] = time.monotonic() - tracker_lens.CACHE_TTL - 1
        self.assertEqual(tracker_lens.scan(PAGE_URL), Report(True, PAGE_TRACKERS, 3, ""))
        self.assertEqual(tracker_lens.CACHE[PAGE_URL], Report(True, PAGE_TRACKERS, 3, ""))
        self.get.assert_called_once()

    def test_scan_unsafe_host_is_not_fetched(self):
        """scan EFFECTS, "its host is unsafe": loopback, private and link-local IP literals are never fetched."""
        for url in ("http://127.0.0.1/admin", "http://10.0.0.8/", "http://169.254.169.254/latest", "http://[::1]/"):
            self.assertEqual(tracker_lens.scan(url), Report(False, [], 0, "unsafe host"))
            self.assertEqual(tracker_lens.CACHE[url], Report(False, [], 0, "unsafe host"))
        self.get.assert_not_called()

    def test_scan_localhost_is_unsafe_and_not_fetched(self):
        """scan EFFECTS, "its host is unsafe (localhost ...)" (amendment 12): no GET and no name lookup."""
        for url in ("http://localhost/admin", "http://LOCALHOST:8888/", "http://db.localhost/", "http://localhost./"):
            self.assertEqual(tracker_lens.scan(url), Report(False, [], 0, "unsafe host"))
        self.get.assert_not_called()
        self.getaddrinfo.assert_not_called()

    def test_scan_name_resolving_to_private_address_is_unsafe(self):
        """scan EFFECTS, "a name resolving to an address that is ... private" (amendment 12): no GET."""
        self.getaddrinfo.return_value = PUBLIC_ADDRESS + PRIVATE_ADDRESS
        self.assertEqual(tracker_lens.scan("https://intranet.example/"), Report(False, [], 0, "unsafe host"))
        self.assertEqual(tracker_lens.CACHE["https://intranet.example/"], Report(False, [], 0, "unsafe host"))
        self.getaddrinfo.assert_called_once_with("intranet.example", None)
        self.get.assert_not_called()

    def test_scan_unresolvable_name_is_connect_and_not_fetched(self):
        """scan EFFECTS, "a host name does not resolve" (amendment 13): "connect", and no GET."""
        self.getaddrinfo.side_effect = socket.gaierror(8, "nodename nor servname provided, or not known")
        self.assertEqual(tracker_lens.scan("https://no-such-name.example/"), Report(False, [], 0, "connect"))
        self.get.assert_not_called()

    def test_scan_redirect_to_link_local_address_is_unsafe(self):
        """scan EFFECTS, "that hop is never fetched" (amendment 14): a redirect to 169.254.169.254 or file: stops."""
        for location in ("http://169.254.169.254/latest/meta-data/", "file:///etc/passwd"):
            self.get.reset_mock()
            self.get.return_value = response(status=302, location=location)
            url = "https://public.example/?to=" + location
            self.assertEqual(tracker_lens.scan(url), Report(False, [], 0, "unsafe host"))
            self.get.assert_called_once()

    def test_scan_four_redirects_gives_redirects(self):
        """scan EFFECTS, "still redirecting after MAX_REDIRECTS redirects" (amendment 14): "redirects", 4 GETs."""
        self.get.side_effect = [response(status=301, location=f"https://hop{i}.example/") for i in range(1, 6)]
        self.assertEqual(tracker_lens.scan("https://hop0.example/"), Report(False, [], 0, "redirects"))
        self.assertEqual(tracker_lens.CACHE["https://hop0.example/"], Report(False, [], 0, "redirects"))
        self.assertEqual(
            [call.args[0] for call in self.get.call_args_list],
            [f"https://hop{i}.example/" for i in range(tracker_lens.MAX_REDIRECTS + 1)],
        )

    def test_scan_one_redirect_still_scans(self):
        """scan EFFECTS, "p is the last hop" (amendments 14, 15): one redirect still scans, against the final page."""
        page = PAGE.replace("</head>", '<script src="https://static.augusta.edu/app.js"></script></head>')
        self.get.side_effect = [response(status=301, location=PAGE_URL), response(text=page)]
        self.assertEqual(tracker_lens.scan("https://short.example/a"), Report(True, PAGE_TRACKERS, 3, ""))
        self.assertEqual(tracker_lens.CACHE["https://short.example/a"], Report(True, PAGE_TRACKERS, 3, ""))
        self.assertEqual([call.args[0] for call in self.get.call_args_list], ["https://short.example/a", PAGE_URL])
        self.assertEqual(
            [call.args[0] for call in self.getaddrinfo.call_args_list], ["short.example", "www.augusta.edu"]
        )

    def test_scan_timeout_gives_unfetched_report(self):
        """scan EFFECTS, "a GET times out": "timeout", cached, and on_result still keeps the result."""
        self.get.side_effect = searx.network.Timeout("Timeout")
        self.assertEqual(tracker_lens.scan(PAGE_URL), Report(False, [], 0, "timeout"))
        self.assertEqual(tracker_lens.CACHE[PAGE_URL], Report(False, [], 0, "timeout"))
        with self.app.test_request_context():
            self.assertTrue(self.plugin.on_result(sxng_request, None, main_result(PAGE_URL)))

    def test_scan_connect_error_gives_unfetched_report(self):
        """scan EFFECTS, "a GET cannot connect" (amendment 17): a curl_cffi ConnectionError gives "connect"."""
        self.get.side_effect = CurlConnectionError("refused")
        with self.assertNoLogs("searx.plugins.tracker_lens", level="ERROR"):
            self.assertEqual(tracker_lens.scan(PAGE_URL), Report(False, [], 0, "connect"))
        self.assertEqual(tracker_lens.CACHE[PAGE_URL], Report(False, [], 0, "connect"))

    def test_scan_other_exception_is_error_and_logged(self):
        """scan EFFECTS, "Any other failure" (amendment 17): "error", cached, logged once with the URL."""
        self.get.side_effect = RuntimeError("parser bug")
        with self.assertLogs("searx.plugins.tracker_lens", level="ERROR") as logs:
            self.assertEqual(tracker_lens.scan(PAGE_URL), Report(False, [], 0, "error"))
        self.assertEqual(tracker_lens.CACHE[PAGE_URL], Report(False, [], 0, "error"))
        self.assertEqual(len(logs.output), 1)
        self.assertIn(PAGE_URL, logs.output[0])
        self.assertIn("parser bug", logs.output[0])

    def test_scan_bad_status_gives_unfetched_report(self):
        """scan EFFECTS, "the status of the last answer is not 2xx": 404 gives "http 404"."""
        self.get.return_value = response(status=404)
        self.assertEqual(tracker_lens.scan(PAGE_URL), Report(False, [], 0, "http 404"))

    def test_scan_non_html_gives_unfetched_report(self):
        """scan EFFECTS, "its Content-Type is not HTML": a JSON answer gives "not html"."""
        self.get.return_value = response(content_type="application/json", text="{}")
        self.assertEqual(tracker_lens.scan(PAGE_URL), Report(False, [], 0, "not html"))

    def test_scan_matches_third_party_hosts(self):
        """scan EFFECTS, last case (the page was read): two known trackers out of three third parties, one GET."""
        report = tracker_lens.scan(PAGE_URL)
        self.assertEqual(report, Report(True, PAGE_TRACKERS, 3, ""))
        self.assertEqual(tracker_lens.CACHE[PAGE_URL], report)
        self.get.assert_called_once_with(
            PAGE_URL, timeout=tracker_lens.FETCH_TIMEOUT, allow_redirects=False, raise_for_httperror=False
        )

    def test_scan_evicts_oldest_when_full(self):
        """scan EFFECTS, "When CACHE is full the oldest entry is evicted first": CACHE_MAX + 1 scans."""
        urls = [f"https://site{i}.example/" for i in range(tracker_lens.CACHE_MAX + 1)]
        for url in urls:
            tracker_lens.scan(url)
        self.assertEqual(len(tracker_lens.CACHE), tracker_lens.CACHE_MAX)
        self.assertNotIn(urls[0], tracker_lens.CACHE)
        self.assertIn(urls[1], tracker_lens.CACHE)
        self.assertIn(urls[-1], tracker_lens.CACHE)

    # on_result

    def test_on_result_without_url_changes_nothing(self):
        """on_result EFFECTS, "result.url is None": returns true, no table, no GET."""
        with self.app.test_request_context():
            self.assertTrue(self.plugin.on_result(sxng_request, None, Result()))
            self.assertFalse(hasattr(sxng_request, "tracker_lens"))
        self.get.assert_not_called()

    def test_on_result_non_http_changes_nothing(self):
        """on_result EFFECTS, "scheme is not http or https": an ftp result returns true and queues nothing."""
        with self.app.test_request_context():
            self.assertTrue(self.plugin.on_result(sxng_request, None, main_result("ftp://files.example/pub/x")))
            self.assertFalse(hasattr(sxng_request, "tracker_lens"))
        self.get.assert_not_called()

    def test_on_result_records_future_and_keeps_result(self):
        """on_result EFFECTS, "sets request.tracker_lens[k] := the Future": true, one job, key without query."""
        pool = mock.Mock(spec=["submit"])
        pool.submit.return_value = concurrent.futures.Future()
        self.setattr4test(tracker_lens, "POOL", pool)
        with self.app.test_request_context():
            result = main_result("https://www.augusta.edu/about/?utm_source=x")
            self.assertTrue(self.plugin.on_result(sxng_request, None, result))
            self.assertIs(sxng_request.tracker_lens["www.augusta.edu/about/"], pool.submit.return_value)
        pool.submit.assert_called_once_with(tracker_lens.scan, result.url)

    def test_on_result_same_key_submits_one_scan(self):
        """on_result EFFECTS, "one scan per key per request": two results for one page queue one job."""
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
        """post_search EFFECTS, "request has no tracker_lens attribute": returns None and adds no table."""
        with self.app.test_request_context():
            self.assertIsNone(self.plugin.post_search(sxng_request, None))
            self.assertFalse(hasattr(sxng_request, "tracker_lens"))

    def test_post_search_fills_finished_reports(self):
        """post_search EFFECTS, "its scan finished and returned": the Future is replaced by its Report."""
        report = Report(True, PAGE_TRACKERS, 3, "")
        done = concurrent.futures.Future()
        done.set_result(report)
        with self.app.test_request_context():
            sxng_request.tracker_lens = {"www.augusta.edu/": done}
            self.assertIsNone(self.plugin.post_search(sxng_request, None))
            self.assertEqual(sxng_request.tracker_lens, {"www.augusta.edu/": report})

    def test_post_search_turns_raised_scan_into_error(self):
        """post_search EFFECTS, "its scan finished by raising" (amendment 18): "error", logged, no Future left."""
        raised = concurrent.futures.Future()
        raised.set_exception(AssertionError("broken invariant"))
        with self.app.test_request_context():
            sxng_request.tracker_lens = {"www.augusta.edu/": raised}
            with self.assertLogs("searx.plugins.tracker_lens", level="ERROR") as logs:
                self.assertIsNone(self.plugin.post_search(sxng_request, None))
            self.assertEqual(sxng_request.tracker_lens, {"www.augusta.edu/": Report(False, [], 0, "error")})
        self.assertEqual(len(logs.output), 1)
        self.assertIn("www.augusta.edu/", logs.output[0])

    def test_post_search_marks_unfinished_as_timeout(self):
        """post_search EFFECTS, "it is still running": "timeout" after BUDGET seconds, no Future left."""
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
        """template EFFECTS, "renders nothing": no table, or no key for the result, leaves the card as upstream."""
        result = main_result(PAGE_URL)
        with self.app.test_request_context():
            without_table = self.render(result)
            sxng_request.tracker_lens = {}
            without_key = self.render(result)
        self.assertNotIn("tracker", without_table)
        self.assertEqual(without_table, without_key)

    def test_template_renders_not_checked_badge(self):
        """template EFFECTS, "tl.fetched is false": the badge reads "trackers: not checked", no panel."""
        result = main_result(PAGE_URL)
        with self.app.test_request_context():
            sxng_request.tracker_lens = {"www.augusta.edu/": Report(False, [], 0, "timeout")}
            html = self.render(result)
        self.assertIn('<details class="tracker_lens">', html)
        self.assertIn("trackers: not checked", html)
        self.assertNotIn("<p>", html)
        self.assertNotIn("<li>", html)

    def test_template_renders_no_known_trackers_without_panel(self):
        """template EFFECTS, "0 reads no known trackers in the page HTML" (amendment 20): badge, no panel."""
        result = main_result(PAGE_URL)
        with self.app.test_request_context():
            sxng_request.tracker_lens = {"www.augusta.edu/": Report(True, [], 0, "")}
            html = self.render(result)
        self.assertIn('<details class="tracker_lens">', html)
        self.assertIn("no known trackers in the page HTML", html)
        self.assertNotIn("<p>", html)
        self.assertNotIn("<li>", html)

    def test_template_renders_badge_and_panel(self):
        """template EFFECTS, "a panel titled Who is tracking you here": "2 trackers", one line per tracker."""
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
        """REPRESENTATION INVARIANT: after a whole search every table value is a Report and CACHE maps via HOSTMAP."""
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
        """site_trackers EFFECTS: fetched pages only, in table order, each host list sorted (the graph's input)."""
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
