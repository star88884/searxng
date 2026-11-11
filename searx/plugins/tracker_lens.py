# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=global-statement
"""
OVERVIEW: tracker_lens is a SearXNG plugin. It has a state as follows:
1. HOSTMAP, a map from a registrable domain (for example google-analytics.com) to a
   pair (owner, categories), loaded once at start-up from the file
   searx/data/tracker_radar_hosts.json. That file is built once, offline, by the
   developer, from DuckDuckGo Tracker Radar domains/US/<domain>.json, keeping only
   domain, owner.displayName and categories, and dropping every domain that has
   neither an owner name nor a category: Tracker Radar records every third-party
   domain its crawl saw, and a domain with no owner and no category is not an
   identified tracker. HOSTMAP is an implicit input of every scan and is never
   written at run time.
   (Decision, with the tradeoff: the full Tracker Radar clone is tens of thousands of
   per-domain files, some over 10 MB each, and would need git at run time; one raw
   GitHub fetch per host would add a network round trip per host and a dependency on
   GitHub being up during a search. The flattened file is a few MB, works offline,
   is versioned with the repo, and the lookup is one dictionary read.)
2. CACHE, a map from a page URL to its Report, shared by every request, holding at
   most CACHE_MAX entries, each kept for at most CACHE_TTL seconds.
3. POOL, a pool of POOL_SIZE worker threads that run scans in the background.
4. One table per search request, request.tracker_lens, a map from a result key
   (host plus path of the result URL, see key(result)) to either the Future of a
   scan still running (PENDING) or a finished Report. The table lives on the Flask
   request object and not on the result object, because Result is a msgspec.Struct
   with fixed fields and a new attribute cannot be set on it, and because one
   request owns one search, so the table dies with the request.
A Report is a record (fetched, trackers, third_party, error):
   fetched     boolean, true iff the page was fetched and parsed
   trackers    list of (host, owner, categories): the third-party script and iframe
               hosts of the page whose registrable domain is in HOSTMAP
   third_party number of distinct third-party hosts seen, matched or not
   error       "" when fetched, else one of "unsafe host", "redirects", "timeout",
               "connect", "http <status>", "not html", "error"
Constants: FETCH_TIMEOUT = 1.5 s, MAX_BYTES = 262144, MAX_REDIRECTS = 3, BUDGET = 1.5 s,
   POOL_SIZE = 1, CACHE_MAX = 2048, CACHE_TTL = 3600 s.
The plugin never drops a result and never changes one. Its only output is the
table the template reads to draw a badge and a panel on each result card.
It extends SearXNG's tracker_url_remover (searx/data/tracker_patterns.py), which
cleans tracking parameters out of result URLs with ClearURLs rules. The lens does
not reuse it: it looks at the hosts a page loads, not at the URL's parameters.

REPRESENTATION INVARIANT:
   HOSTMAP is non-empty iff init(app) returned true
   |CACHE| <= CACHE_MAX
   (A u : u in CACHE : CACHE[u] is a Report)
   (A u : u in CACHE and CACHE[u].fetched :
        (A (h,o,c) : (h,o,c) in CACHE[u].trackers :
              registrable(h) in HOSTMAP and (o,c) = HOSTMAP[registrable(h)]))
   (A u : u in CACHE and not CACHE[u].fetched : CACHE[u].trackers = [] and CACHE[u].error != "")
   (A k : k in request.tracker_lens : request.tracker_lens[k] is a Future or a Report)
   after post_search(request, search) returns:
   (A k : k in request.tracker_lens : request.tracker_lens[k] is a Report)
"""

# NUMBERING KEY for the code sketch comments (CoDAT "Sketch" format, CSX.Y.Z).
#   X = the class, Y = the function or method in file order, Z = the step inside it.
#   "# CSX.Y: name - purpose" opens a body and is followed by the list of steps.
#   Each step is then repeated, in more detail, directly above the code that does it.
#   A line of the form "# //{...}" is an intermediate assertion in the style of the
#   Engine example: what must be true at that point. The assert under it checks it.
#
#   CS1  module-level functions
#        CS1.1 _check_rep            CS1.5 _third_party_hosts
#        CS1.2 registrable           CS1.6 _fetch
#        CS1.3 key                   CS1.7 scan
#        CS1.4 _unsafe               CS1.8 site_trackers
#   CS2  class SXNGPlugin
#        CS2.1 __init__    CS2.2 init    CS2.3 on_result    CS2.4 post_search
#   CS3  class _SrcParser
#        CS3.1 __init__    CS3.2 handle_starttag
#   CS4  class Report: a record of four fields with no methods, so it has no steps

import collections
import concurrent.futures
import html.parser
import ipaddress
import itertools
import json
import logging
import socket
import threading
import time
import typing as t
from urllib.parse import urljoin, urlsplit

from curl_cffi.requests.exceptions import ConnectionError as CurlConnectionError
from flask_babel import gettext  # pyright: ignore[reportUnknownVariableType]

import searx.network
from searx.data import data_dir

from . import Plugin, PluginInfo

if t.TYPE_CHECKING:
    import flask
    from searx.search import SearchWithPlugins
    from searx.extended_types import SXNG_Request
    from searx.result_types import Result
    from searx.plugins import PluginCfg

FETCH_TIMEOUT = 1.5  # Seconds one GET may take before its scan answers "timeout"
MAX_BYTES = 262144  # Characters of a page that are parsed. WARNING: bounds the parsing, not the download
MAX_REDIRECTS = 3  # Redirects one scan follows before it answers "redirects"
BUDGET = 1.5  # Seconds post_search waits for scans; after that a card reads "trackers: not checked"
POOL_SIZE = 1  # Worker threads that scan. WARNING: 1 on purpose: a failed fetch closes the client all fetches share
CACHE_MAX = 2048  # Most Reports CACHE holds; the oldest is evicted first
CACHE_TTL = 3600  # Seconds a cached Report is trusted before its page is scanned again

HOSTMAP_FILE = data_dir / "tracker_radar_hosts.json"  # Read once, by init
"""The file HOSTMAP is loaded from."""

SECOND_LEVEL = frozenset(
    {
        "ac.uk",
        "co.uk",
        "gov.uk",
        "org.uk",
        "com.au",
        "net.au",
        "org.au",
        "co.jp",
        "co.kr",
        "co.nz",
        "co.za",
        "co.in",
        "com.br",
        "com.mx",
        "com.ar",
        "com.cn",
        "com.hk",
        "com.sg",
        "com.tw",
        "com.tr",
    }
)  # Suffixes with three-label sites. WARNING: a short list, not the Public Suffix List
"""The known second-level suffixes of registrable(host)."""

HOSTMAP: dict[str, list[t.Any]] = {}  # Registrable domain -> [owner, categories]; filled by init, then only read
CACHE: "collections.OrderedDict[str, Report]" = collections.OrderedDict()  # Page URL -> its Report, oldest first
STAMP: dict[str, float] = {}  # Page URL -> when its CACHE entry was written
"""The time each CACHE entry was written, by URL, for the CACHE_TTL rule."""
POOL: concurrent.futures.ThreadPoolExecutor | None = None  # Runs the scans in the background; None until init
LOCK = threading.Lock()  # Guards CACHE, STAMP and every request table: several threads share them
ACTIVE = False  # True iff the lens is on; _check_rep compares it with HOSTMAP
"""What init(app) returned."""

log = logging.getLogger("searx.plugins.tracker_lens")  # The same logger the plugin object uses as self.log


class Report(t.NamedTuple):
    """
    OVERVIEW: The immutable answer of one scan. See the module OVERVIEW for the
       meaning of each field.
    """

    fetched: bool  # True iff the page was fetched and parsed
    trackers: list[tuple[str, str, list[str]]]  # (host, owner, categories) of each known tracker host, ordered by host
    third_party: int  # Number of distinct third-party hosts seen, known or not
    error: str  # "" when fetched, else why not: "timeout", "connect", "http 404", ...


def _check_rep(table: dict[str, t.Any] | None = None) -> None:
    """
    EFFECTS: Raises AssertionError when the REPRESENTATION INVARIANT of the module
       does not hold for HOSTMAP, CACHE and, when given, the table
       request.tracker_lens; else returns None. To stay cheap, the clauses over
       CACHE are checked on at most 64 of its entries, the oldest first.
    HELPS: init, scan and post_search, which call it before they return
    """
    # CS1.1: _check_rep - Checks the representation invariant; the first clause that fails stops the caller.
    # CS1.1.1: Check that the host map is loaded exactly when the lens is on.
    # CS1.1.2: Under the lock, check the size clauses and copy out what the slower checks need.
    # CS1.1.3: Check each sampled cache entry against the host map.
    # CS1.1.4: Check the request table, when one was given.

    # CS1.1.1: First invariant line: HOSTMAP is non-empty iff init returned true.
    assert bool(HOSTMAP) == ACTIVE

    # CS1.1.2: The lock is held only for two cheap checks and two copies, so no other thread waits
    #          while entries are walked. WARNING: only the 64 oldest cache entries are sampled, to
    #          keep the check cheap enough to run on every scan.
    with LOCK:
        assert len(CACHE) <= CACHE_MAX
        assert CACHE.keys() == STAMP.keys()
        sample = list(itertools.islice(CACHE.values(), 64))  # The 64 oldest Reports
        values = list(table.values()) if table is not None else []  # A copy: the table may change while it is checked

    # CS1.1.3: A fetched Report may list only hosts that are in the host map, with the map's owner
    #          and categories. An unfetched Report lists nothing and says why.
    for report in sample:
        assert isinstance(report, Report)
        if report.fetched:
            for host, owner, cats in report.trackers:
                assert registrable(host) in HOSTMAP
                assert list(HOSTMAP[registrable(host)]) == [owner, cats]
        else:
            assert report.trackers == [] and report.error != ""

    # CS1.1.4: Before post_search a value may still be a Future. It is never anything else.
    for value in values:
        assert isinstance(value, (concurrent.futures.Future, Report))


def registrable(host: str) -> str:
    """
    REQUIRES: host is a lower-case DNS host name without a port.
    EFFECTS: Returns the registrable domain of host: its last two labels, or its last
       three when the last two are a known second-level suffix (co.uk, com.au and the
       like). Tracker Radar files are keyed by registrable domain, so this is the
       lookup key into HOSTMAP. Example: registrable("ajax.googleapis.com") =
       "googleapis.com".
    HELPS: scan(url)
    """
    # CS1.2: registrable - Cuts a host name down to the domain its owner registered, the key of HOSTMAP.
    # CS1.2.1: Drop a trailing dot.
    # CS1.2.2: Return an IP address as it is.
    # CS1.2.3: Keep the last two labels, or the last three under a known second-level suffix.

    # CS1.2.1: "example.com." and "example.com" name the same host.
    host = host.rstrip(".")

    # CS1.2.2: An address has no labels to cut: 10.0.0.8 must not become "0.8". ip_address raises
    #          ValueError for anything that is not an address, and then the name rule below applies.
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass  # Not an address: go on to the name rule

    # CS1.2.3: www.bbc.co.uk gives bbc.co.uk because nobody owns co.uk itself; ajax.googleapis.com
    #          gives googleapis.com.
    labels = host.split(".")
    n = 3 if ".".join(labels[-2:]) in SECOND_LEVEL else 2
    return ".".join(labels[-n:])


def key(result) -> str:
    """
    REQUIRES: result.parsed_url is not None
    EFFECTS: Returns result.parsed_url.netloc + result.parsed_url.path. The query
       string and the fragment are left out on purpose: a later plugin may strip
       tracking parameters out of result.url (tracker_url_remover does, and plugin
       order is not fixed), and the key must be the same string when on_result
       writes it and when the template reads it.
    HELPS: SXNGPlugin.on_result(request, search, result); macros.html result_sub_header
    """
    # CS1.3: key - Names a result by host plus path, the string the request table is indexed by.
    # CS1.3.1: Join the host part and the path; leave the query string and the fragment out.

    # CS1.3.1: Another plugin may rewrite the query string between on_result and the template, so
    #          only the part of the URL that stays the same is used.
    return result.parsed_url.netloc + result.parsed_url.path


class _SrcParser(html.parser.HTMLParser):
    """
    OVERVIEW: Collects the src attribute of every script and iframe tag fed to it.
       It has one piece of state, srcs: the src values in the order the tags appear.
    """

    # CONSTRUCTORS

    def __init__(self) -> None:
        """
        EFFECTS: Makes this an empty collector: srcs = [].
        """
        # CS3.1: __init__ - Starts a parser that has seen no tags yet.
        # CS3.1.1: Let HTMLParser set itself up.
        # CS3.1.2: Start the list of src values empty.

        # CS3.1.1: The parent class keeps its own parsing state and must be set up first.
        super().__init__()

        # CS3.1.2: One entry per script or iframe tag that has a src, in page order.
        self.srcs: list[str] = []  # The src values collected so far

    # METHODS

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """
        REQUIRES: tag is a lower-case tag name and attrs its (name, value) pairs, as
           HTMLParser passes them for each opening tag it reads.
        MODIFIES: this
        EFFECTS: If tag is script or iframe and has a non-empty src attribute, appends
           the value of its first src to srcs; else changes nothing.
        HELPS: _third_party_hosts(url, body), through HTMLParser.feed(body)
        """
        # CS3.2: handle_starttag - Called by HTMLParser for each opening tag; keeps the src of scripts and iframes.
        # CS3.2.1: Ignore every tag that is not a script or an iframe.
        # CS3.2.2: Keep the first non-empty src of the tag and stop looking.

        # CS3.2.1: Only these two tags make a browser load code or a page from another host.
        if tag in ("script", "iframe"):
            # CS3.2.2: A browser uses the first src when a tag repeats the attribute, so the scan
            #          does too. A tag without a src (an inline script) loads nothing and adds
            #          nothing.
            for name, value in attrs:
                if name == "src" and value:
                    self.srcs.append(value)
                    break  # First src only


def _unsafe(host: str) -> bool:
    """
    REQUIRES: host is a lower-case host name or IP literal without a port.
    EFFECTS: Returns true iff host is localhost or a name under .localhost, or is an
       IP literal that is, or a name that resolves (system resolver,
       socket.getaddrinfo) to at least one address that is, loopback, private,
       link-local, reserved, multicast or unspecified. Such a host points into the
       server's own machine or network, and the lens must not fetch it on a
       user's behalf. Raises socket.gaierror when host is a name that does not
       resolve.
    HELPS: _fetch(url)
    """
    # CS1.4: _unsafe - Says whether a host points into the server's own machine or network.
    # CS1.4.1: Drop a trailing dot.
    # CS1.4.2: Answer yes for a localhost name without asking the resolver.
    # CS1.4.3: Turn the host into the list of addresses it stands for.
    # CS1.4.4: Answer yes if any of those addresses is in a range the lens must not fetch.

    # CS1.4.1: "localhost." is localhost.
    host = host.rstrip(".")

    # CS1.4.2: The name localhost always means this machine, whatever a resolver is set up to say.
    if host == "localhost" or host.endswith(".localhost"):
        return True

    # CS1.4.3: An IP literal stands for itself (a "%zone" suffix on an IPv6 address is cut off
    #          first). Anything else is a name, and the system resolver is asked for every address
    #          it has. A name that does not resolve raises socket.gaierror here, which scan turns
    #          into "connect". WARNING: the fetch resolves the name again later, so an answer that
    #          changes in between is not caught (DNS rebinding, a known limit written in the spec).
    try:
        ips = [ipaddress.ip_address(host.split("%")[0])]
    except ValueError:
        ips = [ipaddress.ip_address(info[4][0].split("%")[0]) for info in socket.getaddrinfo(host, None)]

    # CS1.4.4: One bad address is enough: a name with a public and a private address could still be
    #          fetched at the private one.
    return any(
        ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        for ip in ips
    )


def _third_party_hosts(url: str, body: str) -> set[str]:
    """
    REQUIRES: url is an absolute http or https URL.
    EFFECTS: Returns the set H of scan's EFFECTS, with url as the page p: the host of
       every script and iframe src in body, resolved against url, whose registrable
       domain differs from that of url. A src that resolves to no host, or to a
       scheme other than http or https, is left out.
    HELPS: scan(url)
    """
    # CS1.5: _third_party_hosts - Lists the other sites a page pulls scripts and frames from.
    # CS1.5.1: Collect every script and iframe src in the body.
    # CS1.5.2: Work out the page's own registrable domain.
    # CS1.5.3: Resolve each src against the page URL and keep its host when it is another domain's.

    # CS1.5.1: The parser copes with broken HTML. close() makes it finish a tag it was still
    #          holding.
    parser = _SrcParser()
    parser.feed(body)
    parser.close()

    # CS1.5.2: Everything under the page's own domain is first party and is not counted.
    own = registrable(urlsplit(url).hostname or "")

    # CS1.5.3: urljoin turns "/a.js" and "//cdn.example/a.js" into full URLs the way a browser
    #          would. A src that cannot be parsed, has no host (data:, javascript:) or is not
    #          http(s) is skipped. A set keeps each host once however many tags name it.
    hosts: set[str] = set()
    for src in parser.srcs:
        try:
            parts = urlsplit(urljoin(url, src.strip()))
        except ValueError:
            continue  # A src that cannot be parsed is skipped
        host = (parts.hostname or "").rstrip(".")
        if parts.scheme in ("http", "https") and host and registrable(host) != own:
            hosts.add(host)
    return hosts


def _fetch(url: str) -> tuple[str, str, t.Any]:
    """
    REQUIRES: url is an absolute URL.
    EFFECTS: Asks for the page at url and follows its redirects by hand, at most
       MAX_REDIRECTS of them. A hop is one URL of that chain: url first, then the
       Location of each redirect answer (status 301, 302, 303, 307 or 308),
       resolved against the hop that sent it. Returns (error, p, resp):
       ("unsafe host", p, None) if hop p is not an http or https URL or
          _unsafe(host(p)); p is not fetched, and no later hop is;
       ("redirects", p, None) if MAX_REDIRECTS redirects were followed and the
          answer of the last one is again a redirect, to p;
       ("", p, resp) otherwise, where resp is the first answer that is not a
          redirect and p is the hop that gave it.
       Each hop is one GET that waits at most FETCH_TIMEOUT seconds. Raises what
       searx.network.get and _unsafe raise.
    IMPL SKETCH:
       p := url
       repeat MAX_REDIRECTS + 1 times:
          if scheme(p) notin {http, https} or _unsafe(host(p)) then
             return ("unsafe host", p, null)
          //{p is an http(s) URL whose host passed the unsafe-host check}
          resp := searx.network.get(p, timeout := FETCH_TIMEOUT, allow_redirects := false,
                                    raise_for_httperror := false)
          if resp is not a redirect with a Location then return ("", p, resp)
          p := urljoin(p, resp.Location)
       //{MAX_REDIRECTS + 1 hops were fetched and every answer was a redirect}
       return ("redirects", p, null)
    HELPS: scan(url)
    """
    # CS1.6: _fetch - Gets a page, following its redirects one checked hop at a time.
    # CS1.6.1: Start at the URL that was asked for.
    # CS1.6.2: Refuse a hop that is not http(s) or whose host is unsafe.
    # CS1.6.3: Send one GET for the hop, with automatic redirects switched off.
    # CS1.6.4: Hand the answer back unless it is a redirect.
    # CS1.6.5: Move to the hop the redirect names and go round again.
    # CS1.6.6: Give up when the allowed redirects are used up.

    # CS1.6.1: p is the hop about to be fetched. The loop body runs at most MAX_REDIRECTS + 1 times:
    #          once for the first page and once per redirect.
    p = url
    for _ in range(MAX_REDIRECTS + 1):
        # CS1.6.2: The check runs before every GET, not only the first, so a public page cannot
        #          bounce the server to an internal address or to a file: URL.
        parts = urlsplit(p)
        if parts.scheme not in ("http", "https") or _unsafe(parts.hostname or ""):
            return "unsafe host", p, None
        # //{p is an http(s) URL whose host passed the unsafe-host check}
        assert parts.scheme in ("http", "https")

        # CS1.6.3: allow_redirects=False makes the network layer hand back the 3xx answer itself, so
        #          the next hop returns here to be checked. raise_for_httperror=False does the same
        #          for 4xx and 5xx answers: a 403 comes back as an answer, not as an exception.
        resp = searx.network.get(p, timeout=FETCH_TIMEOUT, allow_redirects=False, raise_for_httperror=False)

        # CS1.6.4: A redirect is one of the five redirect statuses together with a Location header.
        #          Anything else is the final answer, good or bad, and scan judges it.
        location = resp.headers.get("location")
        if resp.status_code not in (301, 302, 303, 307, 308) or not location:
            return "", p, resp  # The final answer and the hop that gave it

        # CS1.6.5: Location may be relative ("/login"); urljoin resolves it against the hop that
        #          sent it.
        p = urljoin(p, location)
    # //{MAX_REDIRECTS + 1 hops were fetched and every answer was a redirect}

    # CS1.6.6: Reached only when every one of the MAX_REDIRECTS + 1 answers was a redirect. p is the
    #          next hop, which is not fetched.
    return "redirects", p, None


def scan(url: str) -> Report:
    """
    REQUIRES: url is an absolute http or https URL as normalized by
       Result.normalize_result_fields(); HOSTMAP is loaded (init returned true).
    MODIFIES: CACHE, STAMP
    EFFECTS: If url is in CACHE and its entry is younger than CACHE_TTL, returns that
       Report and fetches nothing. Else asks for the page with _fetch(url), which
       follows at most MAX_REDIRECTS redirects and checks every hop, and:
       if a hop is not an http or https URL or its host is unsafe (localhost, or an
          address, or a name resolving to an address, that is loopback, private,
          link-local, reserved, multicast or unspecified), returns
          Report(false, [], 0, "unsafe host") and caches it; that hop is never fetched;
       else if the page is still redirecting after MAX_REDIRECTS redirects, the same
          with "redirects";
       else if a GET times out (FETCH_TIMEOUT seconds per hop), the same with
          "timeout"; if a host name does not resolve or a GET cannot connect, the
          same with "connect";
       else if the status of the last answer is not 2xx, returns
          Report(false, [], 0, "http <status>") and caches it; if its Content-Type is
          not HTML, the same with "not html";
       else returns Report(true, T, |H|, "") and caches it, where p is the last hop
          (the URL that answered), body is the first MAX_BYTES characters of the
          answer's text, and
          H = { host(s) : s is the src of a <script> or <iframe> tag in body,
                resolved against p, and registrable(host(s)) != registrable(host(p)) }
          T = [ (h, o, c) : h in H, registrable(h) in HOSTMAP, (o, c) = HOSTMAP[registrable(h)] ]
                ordered by h.
       Any other failure returns Report(false, [], 0, "error"), caches it, and is
       logged once with url.
       MAX_BYTES bounds what is read out of the body, not the download: the whole
       body is downloaded, and FETCH_TIMEOUT is the only bound on that.
       When CACHE is full the oldest entry is evicted first. Never raises: every
       failure becomes a Report with fetched = false. (A failed assertion is not a
       failure of the page. It means this module broke its own invariant, so it is
       raised to be seen, and post_search reports that scan as "error".)
    IMPL SKETCH:
       if url in CACHE and age(CACHE[url]) < CACHE_TTL then return CACHE[url]
       (error, p, resp) := _fetch(url)
          // Timeout gives r := Report(false, [], 0, "timeout"); a name that does not
          // resolve or a connection error gives "connect"; any other exception gives
          // "error" and is logged
       if error != "" then r := Report(false, [], 0, error)
       else if resp.status not in 2xx then r := Report(false, [], 0, "http " + status)
       else if "html" notin resp.content_type then r := Report(false, [], 0, "not html")
       else
          body := first MAX_BYTES characters of resp.text
          //{body is the head of an html page that answered 2xx}
          H := { host of urljoin(p, src) : (script or iframe, src) in html.parser(body) }
               minus { x : registrable(x) = registrable(host(p)) }
          T := [ (x, HOSTMAP[registrable(x)]) : x in sorted(H) if registrable(x) in HOSTMAP ]
          r := Report(true, T, |H|, "")
       //{r.fetched implies (A (x,o,c) : (x,o,c) in r.trackers : (o,c) = HOSTMAP[registrable(x)])}
       with LOCK: CACHE[url] := r; while |CACHE| > CACHE_MAX evict the oldest entry
       return r
    HELPS: SXNGPlugin.on_result(request, search, result), which runs scan inside POOL
    """
    # CS1.7: scan - Produces the Report for one page, from the cache or by fetching and reading the page.
    # CS1.7.1: Return the cached Report when there is one young enough.
    # CS1.7.2: Fetch the page through _fetch.
    # CS1.7.3: Turn a refused hop, a bad status or a non-HTML answer into an unfetched Report.
    # CS1.7.4: Read the page: find its third-party hosts and look each one up in the host map.
    # CS1.7.5: Turn every exception into an unfetched Report with a label that says what happened.
    # CS1.7.6: Store the Report in the cache and evict the oldest entry when the cache is over its limit.
    # CS1.7.7: Check the invariant and return the Report.

    # CS1.7.1: The lock is held only to read the cache. An entry older than CACHE_TTL counts as
    #          missing, so its page is scanned again and the entry replaced.
    with LOCK:
        cached = CACHE.get(url)
        if cached is not None and time.monotonic() - STAMP[url] >= CACHE_TTL:
            cached = None  # Too old: treat it as missing
    if cached is not None:
        _check_rep()
        return cached

    # CS1.7.2: Everything that can fail sits inside this try, which is how scan keeps its promise
    #          never to raise. _fetch returns a label when it refused a hop or ran out of redirects,
    #          else the answer and p, the URL that gave it.
    try:
        error, p, resp = _fetch(url)

        # CS1.7.3: Each case names what went wrong. The card then reads "trackers: not checked".
        if error:
            r = Report(False, [], 0, error)
        else:
            content_type = resp.headers.get("content-type", "")
            if not 200 <= resp.status_code < 300:
                r = Report(False, [], 0, f"http {resp.status_code}")
            elif "html" not in content_type.lower():
                r = Report(False, [], 0, "not html")
            else:
                # CS1.7.4: p, not url, is the page: after a redirect the scripts belong to the site
                #          that answered. Only a host whose registrable domain is in HOSTMAP becomes
                #          a tracker, while len(hosts) counts every third party, known or not.
                body = resp.text[:MAX_BYTES]  # WARNING: the whole body was downloaded; this bounds parsing
                # //{body is the head of an html page that answered 2xx}
                assert 200 <= resp.status_code < 300 and "html" in content_type.lower() and len(body) <= MAX_BYTES
                hosts = _third_party_hosts(p, body)
                trackers = [
                    (x, HOSTMAP[registrable(x)][0], HOSTMAP[registrable(x)][1])
                    for x in sorted(hosts)
                    if registrable(x) in HOSTMAP
                ]
                r = Report(True, trackers, len(hosts), "")

    # CS1.7.5: The order matters. A connect time-out is both a time-out and a connection error, and
    #          "timeout" is tried first. A name that does not resolve (socket.gaierror from _unsafe)
    #          counts as "connect". A failed assert is passed on untouched: it is a fault in this
    #          module, not a property of the page. Anything else is "error", and the log line names
    #          the URL and the exception.
    except searx.network.Timeout:
        r = Report(False, [], 0, "timeout")
    except (CurlConnectionError, socket.gaierror):
        r = Report(False, [], 0, "connect")
    except AssertionError:
        raise  # A broken invariant must be seen, not hidden
    except Exception as exc:  # pylint: disable=broad-except
        log.error("tracker lens scan of %s failed: %r", url, exc)
        r = Report(False, [], 0, "error")
    # //{r.fetched implies (A (x,o,c) : (x,o,c) in r.trackers : (o,c) = HOSTMAP[registrable(x)])}
    assert not r.fetched or all(list(HOSTMAP[registrable(x)]) == [o, c] for x, o, c in r.trackers)

    # CS1.7.6: pop first, so that a URL scanned again moves to the young end of the ordered map.
    #          popitem(last=False) removes the oldest entry, and STAMP loses the same key so the two
    #          maps keep the same keys.
    with LOCK:
        CACHE.pop(url, None)
        CACHE[url] = r
        STAMP[url] = time.monotonic()
        while len(CACHE) > CACHE_MAX:
            oldest, _ = CACHE.popitem(last=False)
            STAMP.pop(oldest, None)

    # CS1.7.7: _check_rep takes the lock itself, so it is called after the lock is released.
    _check_rep()
    return r


def site_trackers(table: dict[str, Report]) -> dict[str, list[str]]:
    """
    REQUIRES: table is a request.tracker_lens table after post_search returned:
       every value is a Report.
    EFFECTS: Returns {key: [host for (host, owner, cats) in report.trackers]} for
       every key of table whose Report has fetched = true, keys in table order, each
       list sorted. Performs no I/O and changes nothing. The result is the mapping
       tracker_graph.build_tracker_graph takes: website identifier to its tracker
       identifiers.
    HELPS: searx.tracker_graph.build_tracker_graph(site_trackers(request.tracker_lens))
    """
    # CS1.8: site_trackers - Reshapes a finished table into the mapping the graph view takes.
    # CS1.8.1: For every fetched page, list its tracker hosts in sorted order.

    # CS1.8.1: Unfetched pages are left out, so the graph never draws a page that was not read as a
    #          page without trackers. Owner and categories are dropped: the graph needs only the
    #          hosts.
    return {k: sorted(host for host, _, _ in report.trackers) for k, report in table.items() if report.fetched}


@t.final
class SXNGPlugin(Plugin):
    """
    OVERVIEW: The one plugin object SearXNG builds from settings.yml. It holds the
       module state (HOSTMAP, CACHE, POOL) and receives the three calls SearXNG
       makes: init once at start-up, on_result once per result of every search,
       post_search once per search after the engines are joined.
    """

    id = "tracker_lens"  # The name SearXNG knows the plugin by: settings, preferences, logger

    # CONSTRUCTORS

    def __init__(self, plg_cfg: "PluginCfg") -> None:
        """
        EFFECTS: Sets id and info (name "Tracker lens", description, preference
           section "privacy", so the user can switch it off in Preferences).
           Loads nothing: see init.
        """
        # CS2.1: __init__ - Builds the plugin object SearXNG lists. It does no work yet.
        # CS2.1.1: Let the Plugin base class set itself up.
        # CS2.1.2: Describe the plugin for the Preferences page.

        # CS2.1.1: The base class keeps the settings entry and creates self.log.
        super().__init__(plg_cfg)

        # CS2.1.2: gettext marks the two texts for translation. The "privacy" section is where a
        #          user finds the switch that turns the lens off.
        self.info = PluginInfo(
            id=self.id,
            name=gettext("Tracker lens"),
            description=gettext("Shows which trackers each result page loads, before you click it."),
            preference_section="privacy",
        )

    # METHODS

    def init(self, app: "flask.Flask") -> bool:
        """
        MODIFIES: HOSTMAP, CACHE, STAMP, POOL, ACTIVE
        EFFECTS: If searx/data/tracker_radar_hosts.json cannot be read, cannot be
           parsed, holds no entries, or holds an entry whose value is not
           [owner, categories] with owner a string and categories a list of strings,
           logs the reason and returns false, so PluginStorage removes the plugin
           and SearXNG runs exactly as before (no badge, no panel, no fetch). Else
           sets HOSTMAP from the file, sets CACHE empty, starts POOL with POOL_SIZE
           workers, and returns true.
           A missing list switches the lens off; it never runs with an empty map,
           because an empty map would draw "no known trackers in the page HTML" on
           every card, and that would be false.
        IMPL SKETCH:
           try HOSTMAP := json.load(file) except (OSError, ValueError) as e: log(e); return false
           if (E d : d in HOSTMAP : HOSTMAP[d] is not [string, list of strings]) then
              log(d); return false
           if HOSTMAP = {} then log("empty host map"); return false
           //{HOSTMAP != {} /\\ (A d : d in HOSTMAP : HOSTMAP[d] = [string, list of strings])}
           CACHE := {}; POOL := ThreadPoolExecutor(POOL_SIZE)
           return true
        HELPS: PluginStorage.init(app), called once when the web application starts
        """
        # CS2.2: init - Loads the host map and starts the worker pool; returns false to switch the lens off.
        # CS2.2.1: Empty the module state, so the invariant holds whatever happens next.
        # CS2.2.2: Read the host map file and check every entry.
        # CS2.2.3: Switch the lens off when the file cannot be used.
        # CS2.2.4: Switch the lens off when the map is empty.
        # CS2.2.5: Start the worker pool and mark the lens as on.

        # CS2.2.1: "global" is needed because this method gives POOL and ACTIVE new values; HOSTMAP,
        #          CACHE and STAMP are only changed in place. Starting from empty means a second
        #          call can never leave half of an old map behind.
        global POOL, ACTIVE
        ACTIVE = False
        HOSTMAP.clear()
        with LOCK:
            CACHE.clear()
            STAMP.clear()

        # CS2.2.2: The file must hold a JSON object whose every value is [owner, categories].
        #          Anything else raises here and is handled as one case below. HOSTMAP is filled
        #          only after every entry has passed.
        try:
            with HOSTMAP_FILE.open(encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise TypeError("not a JSON object")
            for domain, value in data.items():
                if not (
                    isinstance(value, list)
                    and len(value) == 2
                    and isinstance(value[0], str)
                    and isinstance(value[1], list)
                    and all(isinstance(c, str) for c in value[1])
                ):
                    raise TypeError(f"entry {domain!r} is not [owner, categories]")
            HOSTMAP.update(data)

        # CS2.2.3: Unreadable (OSError), not JSON (ValueError) or the wrong shape (TypeError): log
        #          the reason, leave HOSTMAP empty and return false. PluginStorage then drops the
        #          plugin and every page renders as it does upstream.
        except (OSError, ValueError, TypeError) as exc:
            HOSTMAP.clear()
            self.log.error("tracker lens is off, host map %s: %s", HOSTMAP_FILE, exc)
            _check_rep()
            return False

        # CS2.2.4: An empty map would label every page as free of known trackers, which would be
        #          false.
        if not HOSTMAP:
            self.log.error("tracker lens is off, host map %s: empty host map", HOSTMAP_FILE)
            _check_rep()
            return False
        # //{HOSTMAP != {} /\ (A d : d in HOSTMAP : HOSTMAP[d] = [string, list of strings])}
        assert HOSTMAP and all(isinstance(o, str) and isinstance(c, list) for o, c in HOSTMAP.values())

        # CS2.2.5: A pool left over from an earlier init is told to stop. ACTIVE is set last, when
        #          everything else is in place.
        if POOL is not None:
            POOL.shutdown(wait=False)  # Do not wait for scans of the old pool
        POOL = concurrent.futures.ThreadPoolExecutor(POOL_SIZE, thread_name_prefix="tracker_lens")
        ACTIVE = True
        _check_rep()
        return True

    def on_result(self, request: "SXNG_Request", search: "SearchWithPlugins", result: "Result") -> bool:
        """
        REQUIRES: init(app) returned true. request is the real Flask request of the
           search in progress (SearchWithPlugins passes request._get_current_object(),
           one object shared by every engine thread). result has been normalized:
           if result.url is set then result.parsed_url is set.
        MODIFIES: request (attribute tracker_lens), POOL (one queued job), CACHE
           (through scan, later, in a worker)
        EFFECTS: If result.url is None (answers, suggestions, corrections, infoboxes
           and engine_data items can have no url) or result.parsed_url.scheme is not
           http or https or result.parsed_url has no host, returns true and changes
           nothing. Else let k = key(result):
           if request has no tracker_lens attribute, sets request.tracker_lens := {};
           if k is not in request.tracker_lens, sets request.tracker_lens[k] := the
           Future of scan(result.url) submitted to POOL; otherwise changes nothing
           (one scan per key per request, however many engines return that page).
           Returns true in every case: the lens never drops a result and never
           changes one. Does not wait for the scan. Never raises.
        IMPL SKETCH:
           if result.url = null or result.parsed_url.scheme notin {http, https}
              or result.parsed_url has no host then return true
           //{result.url is an absolute http(s) url}
           k := key(result)
           with LOCK:
              if request has no tracker_lens then request.tracker_lens := {}
              if k notin request.tracker_lens then
                 request.tracker_lens[k] := POOL.submit(scan, result.url)
           //{k in request.tracker_lens
              /\\ (request.tracker_lens[k] is a Future => a scan of that page is queued or running)}
           return true
        HELPS: PluginStorage.on_result(request, search, result), which
           ResultContainer.extend calls for each result inside the engine's thread
        """
        # CS2.3: on_result - Queues one background scan per result page; never changes or drops the result.
        # CS2.3.1: Leave alone a result that has no http(s) URL with a host.
        # CS2.3.2: Work out the result's key.
        # CS2.3.3: Under the lock, create the request's table if needed and queue a scan if the key is new.
        # CS2.3.4: Keep the result.

        # CS2.3.1: Answers, suggestions and infoboxes pass through here too and may have no URL.
        #          getattr with a default reads an attribute that may be missing without raising.
        url = getattr(result, "url", None)
        parsed_url = getattr(result, "parsed_url", None)
        if url is None or parsed_url is None or parsed_url.scheme not in ("http", "https") or not parsed_url.hostname:
            return True
        # //{result.url is an absolute http(s) url}
        assert parsed_url.scheme in ("http", "https") and parsed_url.netloc

        # CS2.3.2: Two results for the same page (from two engines, or differing only in the query
        #          string) share one key, and so one scan.
        k = key(result)
        assert POOL is not None  # REQUIRES: init(app) returned true

        # CS2.3.3: Every engine thread calls on_result with the same request object, so looking in
        #          the table and writing to it must happen as one step: hence the lock. submit
        #          returns at once with a Future; the scan itself runs later, in the pool's worker
        #          thread.
        with LOCK:
            table = getattr(request, "tracker_lens", None)
            if table is None:
                table = {}
                request.tracker_lens = table  # A Flask request object accepts new attributes
            if k not in table:
                table[k] = POOL.submit(scan, url)  # A Future: the scan's answer, not ready yet
        # //{k in request.tracker_lens
        #    /\ (request.tracker_lens[k] is a Future => a scan of that page is queued or running)}
        assert k in request.tracker_lens

        # CS2.3.4: True tells SearXNG to keep the result. The lens never returns false.
        return True

    def post_search(self, request: "SXNG_Request", search: "SearchWithPlugins") -> None:
        """
        REQUIRES: the same request object that on_result received during this search.
        MODIFIES: request.tracker_lens
        EFFECTS: If request has no tracker_lens attribute (no result had a usable
           url), returns None and changes nothing. Else waits until every scan in
           request.tracker_lens has finished or BUDGET seconds have passed since this
           call began, whichever comes first. Then, for each k in the table: if its
           scan finished and returned, request.tracker_lens[k] := that Report; if its
           scan finished by raising (scan raises only a failed assertion),
           request.tracker_lens[k] := Report(false, [], 0, "error") and the exception
           is logged with k; if it is still running,
           request.tracker_lens[k] := Report(false, [], 0, "timeout"), and the scan is
           left to finish inside POOL so that CACHE is filled for the next search.
           Returns None: adds no results. On return no Future remains in the table,
           whatever the scans did.
        IMPL SKETCH:
           if request has no tracker_lens then return null
           deadline := now + BUDGET
           for (k, f) in request.tracker_lens:
              if f is a Future then
                 if f has not finished by deadline then
                    request.tracker_lens[k] := Report(false, [], 0, "timeout")
                 else if f raised then
                    log(k, the exception); request.tracker_lens[k] := Report(false, [], 0, "error")
                 else request.tracker_lens[k] := f.result()
           //{(A k : k in request.tracker_lens : request.tracker_lens[k] is a Report)}
           return null
        HELPS: PluginStorage.post_search(request, search), which SearchWithPlugins.search
           calls after the engine threads are joined and before the container is closed
        """
        # CS2.4: post_search - Waits a bounded time for the scans, then leaves only Reports in the table.
        # CS2.4.1: Return at once when no result was queued.
        # CS2.4.2: Fix the deadline and collect the scans that are still running.
        # CS2.4.3: Wait for them, but not past the deadline.
        # CS2.4.4: Replace every Future in the table by a Report.
        # CS2.4.5: Check the invariant, table included, and return.

        # CS2.4.1: No table means on_result saw no usable URL in this search.
        table = getattr(request, "tracker_lens", None)
        if table is None:
            _check_rep()
            return None

        # CS2.4.2: time.monotonic() is a clock that never jumps backwards, so the wait is BUDGET
        #          seconds whatever happens to the wall clock. The list is built under the lock
        #          because an engine thread may still be adding to the table.
        deadline = time.monotonic() + BUDGET
        with LOCK:
            pending = [f for f in table.values() if isinstance(f, concurrent.futures.Future)]

        # CS2.4.3: wait() returns when all the scans are done or the time is up, whichever is first.
        #          It is called outside the lock, so scans and engine threads are not held up
        #          meanwhile.
        concurrent.futures.wait(pending, timeout=max(0.0, deadline - time.monotonic()))

        # CS2.4.4: Three cases per Future. Not done: the card reads "not checked" now, and the scan
        #          keeps running to fill the cache for the next search. Done by raising (only a
        #          failed assert inside scan does that): log it and report "error", so the template
        #          never meets a Future. Done normally: take its Report. list(...) copies the items
        #          because the loop writes to the table it walks.
        with LOCK:
            for k, f in list(table.items()):
                if isinstance(f, concurrent.futures.Future):
                    if not f.done():
                        table[k] = Report(False, [], 0, "timeout")
                    elif f.exception() is not None:
                        self.log.error("tracker lens scan of %s raised: %r", k, f.exception())
                        table[k] = Report(False, [], 0, "error")
                    else:
                        table[k] = f.result()
            # //{(A k : k in request.tracker_lens : request.tracker_lens[k] is a Report)}
            assert all(isinstance(v, Report) for v in table.values())

        # CS2.4.5: Returning None tells SearXNG that the plugin adds no results of its own.
        _check_rep(table)
        return None
