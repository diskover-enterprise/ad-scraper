"""Regression tests for the fail-closed server-side media fetch boundary and
Higgsfield request_id validation (Stage 2B-3A).

Root causes covered
-------------------
- /img and /vid authorized URLs with a substring check (`"fbcdn.net" in url`),
  so any host with "fbcdn.net" anywhere in the URL was fetched server-side.
- _fetch_media (used by /analyze and /analyze/beats) had no URL check at all.
- urllib's default opener followed redirects (and file:/ftp: URLs) without
  revalidating the target.
- /vid read unbounded bodies; responses were `Cache-Control: public` with no
  nosniff.
- /generate/{image,video}/status interpolated a caller-supplied request_id
  into an authenticated upstream URL path.

Why these tests load source text instead of `import app`
----------------------------------------------------------
Same reason as tests/test_auth_state.py: app.py contains a pre-existing PEP
701 f-string that only Python 3.12+ can parse. The real shipped functions are
extracted from app.py's source and exec'd into an isolated namespace.

Network safety
--------------
Every test replaces the single network seam (`_media_open` for media,
`urllib.request.urlopen` for Higgsfield) with an in-memory fake. The fbcdn
hostnames below are string fixtures only and are never contacted. The only
"real opener" test uses a file:// URL to a temp file this test creates, to
prove the opener itself has no file handler. No Facebook, fbcdn, Railway,
internal address, or real credential is ever used.
"""
from __future__ import annotations

import io
import ipaddress
import json
import re
import tempfile
import types
import unittest
import urllib.error
import urllib.request
from http.client import HTTPMessage
from pathlib import Path
from urllib.parse import urljoin, urlsplit

try:
    import flask
except ImportError:  # route-level tests are skipped without Flask
    flask = None

APP_PY = Path(__file__).resolve().parent.parent / "app.py"
APP_SRC = APP_PY.read_text(encoding="utf-8")

MIB = 1024 * 1024

# Realistic-looking but fake fbcdn URLs (signatures are made up; never fetched).
IMG_URL = ("https://scontent-lax3-1.xx.fbcdn.net/v/t39.30808-6/123456789_987654321_n.jpg"
           "?stp=dst-jpg_s600x600&_nc_cat=1&ccb=1-7&_nc_sid=aa1b2c&oh=00_AfFAKEsig123&oe=66A1B2C3")
IMG_URL_2 = ("https://scontent-lax3-2.xx.fbcdn.net/v/t39.30808-6/123456789_987654321_n.jpg"
             "?_nc_cat=1&oh=00_AfFAKEsig456&oe=66A1B2C3")
VID_URL = ("https://video-lax3-1.xx.fbcdn.net/v/t42.1790-2/111222333_n.mp4"
           "?_nc_cat=100&ccb=1-7&_nc_sid=55e6f7&efg=eyJ2ZW5jb2RlX3RhZyI6InN2ZV9zZCJ9&oh=00_AfFAKEvid&oe=66A1B2C3")


def extract_block(start_pattern: str) -> str:
    """Extract one top-level source block starting at the first line matching
    start_pattern, running until the next column-0 line."""
    lines = APP_SRC.splitlines()
    pat = re.compile(start_pattern)
    start = next((i for i, line in enumerate(lines) if pat.match(line)), None)
    if start is None:
        raise AssertionError(f"pattern {start_pattern!r} not found in app.py")
    end = len(lines)
    for j in range(start + 1, len(lines)):
        line = lines[j]
        if line.strip() == "":
            continue
        if line[0] not in (" ", "\t") and not line.lstrip().startswith((")", "]", "}")):
            end = j
            break
    return "\n".join(lines[start:end])


def extract_region(start_prefix: str, end_prefix: str) -> str:
    """Extract every line from the one starting with start_prefix up to (not
    including) the next line starting with end_prefix."""
    lines = APP_SRC.splitlines()
    start = next((i for i, l in enumerate(lines) if l.startswith(start_prefix)), None)
    if start is None:
        raise AssertionError(f"region start {start_prefix!r} not found in app.py")
    end = next((j for j in range(start + 1, len(lines)) if lines[j].startswith(end_prefix)), None)
    if end is None:
        raise AssertionError(f"region end {end_prefix!r} not found in app.py")
    return "\n".join(lines[start:end])


# ── Fakes ────────────────────────────────────────────────────────────────────

class FakeResponse:
    """Minimal stand-in for http.client.HTTPResponse. Refuses unbounded reads
    so the tests also prove the boundary always reads in bounded chunks."""

    def __init__(self, status=200, headers=None, body=b"", stream_size=None):
        self.status = status
        self.headers = HTTPMessage()
        for k, v in (headers or {}).items():
            self.headers[k] = v
        self._body = io.BytesIO(body)
        self._stream_remaining = stream_size
        self.bytes_read = 0
        self.closed = False

    def read(self, n=-1):
        if n is None or n < 0:
            raise AssertionError("unbounded read() on media response")
        if self._stream_remaining is not None:
            k = min(n, self._stream_remaining)
            self._stream_remaining -= k
            self.bytes_read += k
            return b"\0" * k
        data = self._body.read(n)
        self.bytes_read += len(data)
        return data

    def close(self):
        self.closed = True


def ok(content_type, body=b"", content_length="auto", stream_size=None):
    headers = {}
    if content_type is not None:
        headers["Content-Type"] = content_type
    if content_length == "auto":
        content_length = str(stream_size if stream_size is not None else len(body))
    if content_length is not None:
        headers["Content-Length"] = content_length
    return lambda: FakeResponse(200, headers, body, stream_size)


def redirect(location, code=302):
    headers = {"Location": location} if location is not None else {}
    return lambda: FakeResponse(code, headers)


class FakeNetwork:
    """Replaces _media_open. Maps exact URL -> response factory and records
    every URL requested. Unknown URLs raise (and are recorded)."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []
        self.responses = []

    def __call__(self, url, timeout):
        self.calls.append(url)
        if url not in self.routes:
            raise AssertionError(f"unexpected media request: {url}")
        resp = self.routes[url]()
        self.responses.append(resp)
        return resp


def load_media_namespace(network=None):
    """Real media boundary + /img, /vid, _fetch_media from app.py, with the
    network seam (_media_open) replaced by `network` when given."""
    ns = {
        "re": re, "ipaddress": ipaddress, "urllib": urllib,
        "urlsplit": urlsplit, "urljoin": urljoin,
    }
    # exec runs the exact shipped source (see module docstring).
    exec(extract_region("# ── Safe media fetch boundary", '@app.route("/img")'), ns)  # noqa: S102
    exec(extract_block(r"^def proxy_img\("), ns)  # noqa: S102
    exec(extract_block(r"^def proxy_vid\("), ns)  # noqa: S102
    exec(extract_block(r"^def _fetch_media\("), ns)  # noqa: S102
    if network is not None:
        ns["_media_open"] = network
    if flask is not None:
        ns["app"] = flask.Flask("media-boundary-test")
        ns["request"] = flask.request
    return ns


# ── URL policy ───────────────────────────────────────────────────────────────

class TestValidateMediaUrl(unittest.TestCase):

    def setUp(self):
        self.ns = load_media_namespace()
        self.validate = self.ns["validate_media_url"]
        self.Error = self.ns["MediaFetchError"]

    def test_accepts_realistic_urls_and_preserves_them_exactly(self):
        for url in (
            IMG_URL,
            IMG_URL_2,
            VID_URL,
            "https://scontent.fsyd3-1.fna.fbcdn.net/v/t1.6435-9/1_n.jpg?oh=00_AB&oe=1",
            "https://SCONTENT-LAX3-1.XX.FBCDN.NET/v/t39/1_n.jpg?oh=00_AB&oe=1",
            "https://scontent-lax3-1.xx.fbcdn.net:443/v/1_n.jpg?oh=00_AB&oe=1",
            "https://fbcdn.net/x.jpg",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.validate(url), url)

    def test_rejects_disallowed_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / "harmless_fixture.txt"
            fixture.write_text("harmless local fixture", encoding="utf-8")
            file_url = fixture.as_uri()
            bad = [
                # wrong scheme
                "http://scontent-lax3-1.xx.fbcdn.net/v/1_n.jpg",
                file_url,
                "file:///etc/passwd?fbcdn.net",
                "ftp://scontent.xx.fbcdn.net/1_n.jpg",
                "data:image/png;base64,AAAA#fbcdn.net",
                "javascript:alert(1)//fbcdn.net",
                "//scontent.xx.fbcdn.net/1_n.jpg",
                "https:scontent.xx.fbcdn.net/1_n.jpg",
                # lookalike hosts
                "https://fbcdn.net.evil.com/1_n.jpg",
                "https://scontent.xx.fbcdn.net.evil.com/1_n.jpg",
                "https://evilfbcdn.net/1_n.jpg",
                "https://xfbcdn.net/1_n.jpg",
                # fbcdn.net only in path/query/fragment
                "https://example.com/fbcdn.net/1_n.jpg",
                "https://example.com/1_n.jpg?u=https://scontent.xx.fbcdn.net/",
                "https://example.com/#.fbcdn.net",
                # userinfo
                "https://fbcdn.net@example.com/1_n.jpg",
                "https://scontent.xx.fbcdn.net@example.com/1_n.jpg",
                "https://user:pass@scontent.xx.fbcdn.net/1_n.jpg",
                "https://@scontent.xx.fbcdn.net/1_n.jpg",
                # ports
                "https://scontent.xx.fbcdn.net:8443/1_n.jpg",
                "https://scontent.xx.fbcdn.net:80/1_n.jpg",
                "https://scontent.xx.fbcdn.net:0443/1_n.jpg",
                "https://scontent.xx.fbcdn.net:/1_n.jpg",
                # IP literals
                "https://127.0.0.1/1_n.jpg",
                "https://169.254.169.254/latest/meta-data/?fbcdn.net",
                "https://10.0.0.1/fbcdn.net/1_n.jpg",
                "https://[::1]/1_n.jpg",
                "https://[::ffff:127.0.0.1]/fbcdn.net",
                # malformed / ambiguous
                "",
                "https://",
                "https:///1_n.jpg",
                "https:\\\\scontent.xx.fbcdn.net\\1_n.jpg",
                "https://scontent.xx.fbcdn.net\\@example.com/1_n.jpg",
                "https://example.com\\.fbcdn.net/1_n.jpg",
                "https://fbcdn.net./1_n.jpg",
                "https://a..fbcdn.net/1_n.jpg",
                "https://.fbcdn.net/1_n.jpg",
                "https://-a.fbcdn.net/1_n.jpg",
                "https://sc_ontent.fbcdn.net/1_n.jpg",
                "https://[scontent.xx.fbcdn.net]/1_n.jpg",
                " https://scontent.xx.fbcdn.net/1_n.jpg",
                "https://scontent.xx.fbcdn.net/1_n.jpg\n",
                "https://scontent.xx.fbcdn.net\t/1_n.jpg",
                "https://scontent.xx.fbcdn.net/1 n.jpg",
                "https://scontent.xx.fbcdn.net/1_n.jpg\x00",
                "https://scontent.xx.fbcdn.nét/1_n.jpg",
                "https://scｏntent.xx.fbcdn.net/1_n.jpg",
            ]
            for url in bad:
                with self.subTest(url=url):
                    with self.assertRaises(self.Error):
                        self.validate(url)

    def test_rejects_non_string(self):
        for url in (None, 123, b"https://scontent.xx.fbcdn.net/1_n.jpg", ["https://fbcdn.net/"]):
            with self.subTest(url=url):
                with self.assertRaises(self.Error):
                    self.validate(url)


class TestRealOpenerHasNoFileHandler(unittest.TestCase):
    """Defense in depth: even if validation were bypassed, the real opener
    used by _media_open has no file handler. Uses a local temp fixture only."""

    def test_file_url_fixture_is_refused_by_opener(self):
        ns = load_media_namespace()
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / "harmless_fixture.txt"
            fixture.write_text("harmless local fixture", encoding="utf-8")
            with self.assertRaises(urllib.error.URLError):
                ns["_media_open"](fixture.as_uri(), 1)


# ── Fetch, redirect and response policy ──────────────────────────────────────

class TestSafeFetchMedia(unittest.TestCase):

    def test_constants_match_policy(self):
        ns = load_media_namespace()
        self.assertEqual(ns["MEDIA_IMAGE_MAX_BYTES"], 15 * MIB)
        self.assertEqual(ns["MEDIA_VIDEO_MAX_BYTES"], 150 * MIB)
        self.assertEqual(ns["MEDIA_MAX_REDIRECTS"], 3)

    def test_valid_image_accepted_with_signed_query_intact(self):
        net = FakeNetwork({IMG_URL: ok("image/jpeg", b"\xff\xd8JPEG")})
        ns = load_media_namespace(net)
        data, mime = ns["safe_fetch_media"](IMG_URL, "image")
        self.assertEqual((data, mime), (b"\xff\xd8JPEG", "image/jpeg"))
        self.assertEqual(net.calls, [IMG_URL])  # exact URL, query untouched
        self.assertTrue(all(r.closed for r in net.responses))

    def test_valid_video_accepted_and_content_type_params_stripped(self):
        net = FakeNetwork({VID_URL: ok("Video/MP4; codecs=avc1", b"\0\0\0\x18ftypmp42")})
        ns = load_media_namespace(net)
        data, mime = ns["safe_fetch_media"](VID_URL, "video")
        self.assertEqual((data, mime), (b"\0\0\0\x18ftypmp42", "video/mp4"))

    def test_media_kind_accepts_image_or_video(self):
        net = FakeNetwork({IMG_URL: ok("image/webp", b"RIFF"), VID_URL: ok("video/mp4", b"mp4")})
        ns = load_media_namespace(net)
        self.assertEqual(ns["safe_fetch_media"](IMG_URL, "media")[1], "image/webp")
        self.assertEqual(ns["safe_fetch_media"](VID_URL, "media")[1], "video/mp4")

    def test_invalid_url_makes_zero_requests(self):
        net = FakeNetwork()
        ns = load_media_namespace(net)
        for url in ("https://fbcdn.net.evil.com/x.jpg", "http://scontent.xx.fbcdn.net/x.jpg", ""):
            with self.subTest(url=url):
                with self.assertRaises(ns["MediaFetchError"]):
                    ns["safe_fetch_media"](url, "image")
        self.assertEqual(net.calls, [])

    # redirects

    def test_redirect_fbcdn_to_fbcdn_succeeds(self):
        net = FakeNetwork({
            IMG_URL: redirect(IMG_URL_2, 302),
            IMG_URL_2: ok("image/jpeg", b"img"),
        })
        ns = load_media_namespace(net)
        self.assertEqual(ns["safe_fetch_media"](IMG_URL, "image"), (b"img", "image/jpeg"))
        self.assertEqual(net.calls, [IMG_URL, IMG_URL_2])
        self.assertTrue(all(r.closed for r in net.responses))

    def test_relative_redirect_resolved_and_revalidated(self):
        target = "https://scontent-lax3-1.xx.fbcdn.net/v/other_n.jpg?oh=00_X&oe=1"
        net = FakeNetwork({
            IMG_URL: redirect("/v/other_n.jpg?oh=00_X&oe=1", 301),
            target: ok("image/png", b"png"),
        })
        ns = load_media_namespace(net)
        self.assertEqual(ns["safe_fetch_media"](IMG_URL, "image"), (b"png", "image/png"))
        self.assertEqual(net.calls, [IMG_URL, target])

    def test_redirect_outside_fbcdn_fails_closed_without_following(self):
        for location in (
            "https://example.com/x.jpg",
            "https://fbcdn.net.evil.com/x.jpg",
            "http://scontent-lax3-2.xx.fbcdn.net/x.jpg",
            "//example.com/x.jpg",
            "https://127.0.0.1/x.jpg",
            "https://scontent.xx.fbcdn.net:8443/x.jpg",
            "https://fbcdn.net@example.com/x.jpg",
            "file:///etc/passwd",
            "https:\\\\example.com\\x.jpg",
        ):
            for code in (301, 302, 303, 307, 308):
                with self.subTest(location=location, code=code):
                    net = FakeNetwork({IMG_URL: redirect(location, code)})
                    ns = load_media_namespace(net)
                    with self.assertRaises(ns["MediaFetchError"]):
                        ns["safe_fetch_media"](IMG_URL, "image")
                    self.assertEqual(net.calls, [IMG_URL])

    def test_redirect_without_location_fails(self):
        net = FakeNetwork({IMG_URL: redirect(None)})
        ns = load_media_namespace(net)
        with self.assertRaises(ns["MediaFetchError"]):
            ns["safe_fetch_media"](IMG_URL, "image")

    def _chain(self, n_redirects):
        urls = [f"https://scontent-lax3-{i}.xx.fbcdn.net/v/1_n.jpg?oh=00_A&oe=1" for i in range(n_redirects + 1)]
        routes = {urls[i]: redirect(urls[i + 1]) for i in range(n_redirects)}
        routes[urls[-1]] = ok("image/jpeg", b"end")
        return urls, routes

    def test_three_redirects_allowed(self):
        urls, routes = self._chain(3)
        net = FakeNetwork(routes)
        ns = load_media_namespace(net)
        self.assertEqual(ns["safe_fetch_media"](urls[0], "image"), (b"end", "image/jpeg"))
        self.assertEqual(net.calls, urls)

    def test_fourth_redirect_rejected_and_not_followed(self):
        urls, routes = self._chain(4)
        net = FakeNetwork(routes)
        ns = load_media_namespace(net)
        with self.assertRaises(ns["MediaFetchError"]):
            ns["safe_fetch_media"](urls[0], "image")
        self.assertEqual(net.calls, urls[:4])  # 5th URL never requested
        self.assertTrue(all(r.closed for r in net.responses))

    # response policy

    def test_wrong_or_missing_content_type_rejected(self):
        cases = [
            ("image", "text/html"),
            ("image", "text/html; charset=utf-8"),
            ("image", None),
            ("image", ""),
            ("image", "video/mp4"),
            ("image", "image/svg+xml"),
            ("image", "application/octet-stream"),
            ("image", "image/"),
            ("image", "image/jpeg, text/html"),
            ("video", "text/html"),
            ("video", "image/jpeg"),
            ("video", None),
            ("media", "text/html"),
            ("media", "application/json"),
            ("media", "image/svg+xml"),
        ]
        for kind, ct in cases:
            with self.subTest(kind=kind, ct=ct):
                net = FakeNetwork({IMG_URL: ok(ct, b"<html>")})
                ns = load_media_namespace(net)
                with self.assertRaises(ns["MediaFetchError"]):
                    ns["safe_fetch_media"](IMG_URL, kind)

    def test_non_200_rejected(self):
        for status in (204, 206, 304, 403, 404, 500):
            with self.subTest(status=status):
                net = FakeNetwork({IMG_URL: lambda s=status: FakeResponse(s, {"Content-Type": "image/jpeg"}, b"x")})
                ns = load_media_namespace(net)
                with self.assertRaises(ns["MediaFetchError"]):
                    ns["safe_fetch_media"](IMG_URL, "image")

    def test_oversized_content_length_rejected_before_reading(self):
        for kind, url, ct, size in (
            ("image", IMG_URL, "image/jpeg", 15 * MIB + 1),
            ("video", VID_URL, "video/mp4", 150 * MIB + 1),
            ("media", IMG_URL, "image/jpeg", 15 * MIB + 1),
            ("media", VID_URL, "video/mp4", 150 * MIB + 1),
        ):
            with self.subTest(kind=kind, size=size):
                net = FakeNetwork({url: ok(ct, content_length=str(size), stream_size=size)})
                ns = load_media_namespace(net)
                with self.assertRaises(ns["MediaFetchError"]):
                    ns["safe_fetch_media"](url, kind)
                self.assertEqual(net.responses[0].bytes_read, 0)

    def test_malformed_content_length_rejected(self):
        for cl in ("abc", "-1", "1e9", "", "12 34", "١٢"):
            with self.subTest(cl=cl):
                net = FakeNetwork({IMG_URL: ok("image/jpeg", b"x", content_length=cl)})
                ns = load_media_namespace(net)
                with self.assertRaises(ns["MediaFetchError"]):
                    ns["safe_fetch_media"](IMG_URL, "image")

    def test_actual_image_body_over_real_limit_rejected_without_content_length(self):
        net = FakeNetwork({IMG_URL: ok("image/jpeg", content_length=None, stream_size=15 * MIB + 10)})
        ns = load_media_namespace(net)
        with self.assertRaises(ns["MediaFetchError"]):
            ns["safe_fetch_media"](IMG_URL, "image")
        self.assertLessEqual(net.responses[0].bytes_read, 15 * MIB + 1)

    def test_actual_body_exceeding_understated_content_length_rejected(self):
        # Content-Length claims 10 bytes, body is larger than the limit.
        net = FakeNetwork({VID_URL: ok("video/mp4", content_length="10", stream_size=2048)})
        ns = load_media_namespace(net)
        ns["MEDIA_VIDEO_MAX_BYTES"] = 1024  # scaled-down limit; avoids allocating 150 MiB
        with self.assertRaises(ns["MediaFetchError"]):
            ns["safe_fetch_media"](VID_URL, "video")
        self.assertLessEqual(net.responses[0].bytes_read, 1025)

    def test_body_exactly_at_limit_accepted(self):
        net = FakeNetwork({VID_URL: ok("video/mp4", b"v" * 1024)})
        ns = load_media_namespace(net)
        ns["MEDIA_VIDEO_MAX_BYTES"] = 1024
        self.assertEqual(ns["safe_fetch_media"](VID_URL, "video"), (b"v" * 1024, "video/mp4"))


# ── Integration: routes and _fetch_media share the boundary ──────────────────

@unittest.skipIf(flask is None, "flask not installed")
class TestProxyRoutes(unittest.TestCase):

    def call(self, ns, route, url):
        with ns["app"].test_request_context(f"/{route}", query_string={"u": url}):
            return ns[f"proxy_{route}"]()

    def test_img_valid_response_headers(self):
        net = FakeNetwork({IMG_URL: ok("image/jpeg", b"\xff\xd8")})
        ns = load_media_namespace(net)
        resp = self.call(ns, "img", IMG_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_data(), b"\xff\xd8")
        self.assertEqual(resp.mimetype, "image/jpeg")
        self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("private", resp.headers["Cache-Control"])
        self.assertNotIn("public", resp.headers["Cache-Control"])
        self.assertEqual(net.calls, [IMG_URL])

    def test_vid_valid_response_headers(self):
        net = FakeNetwork({VID_URL: ok("video/mp4", b"mp4")})
        ns = load_media_namespace(net)
        resp = self.call(ns, "vid", VID_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "video/mp4")
        self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("private", resp.headers["Cache-Control"])
        self.assertNotIn("public", resp.headers["Cache-Control"])

    def test_invalid_url_is_400_with_zero_requests(self):
        for route in ("img", "vid"):
            for url in ("https://example.com/?fbcdn.net", "https://fbcdn.net@example.com/", "", "http://a.fbcdn.net/x"):
                with self.subTest(route=route, url=url):
                    net = FakeNetwork()
                    ns = load_media_namespace(net)
                    resp = self.call(ns, route, url)
                    self.assertEqual(resp.status_code, 400)
                    self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")
                    self.assertEqual(net.calls, [])

    def test_upstream_policy_failures_are_502(self):
        cases = [
            ("img", IMG_URL, ok("text/html", b"<script>")),
            ("img", IMG_URL, ok("video/mp4", b"x")),
            ("vid", VID_URL, ok("image/jpeg", b"x")),
            ("vid", VID_URL, ok("text/html", b"<script>")),
            ("img", IMG_URL, redirect("https://example.com/x.jpg")),
            ("img", IMG_URL, ok("image/jpeg", content_length=str(15 * MIB + 1), stream_size=15 * MIB + 1)),
        ]
        for route, url, factory in cases:
            with self.subTest(route=route):
                net = FakeNetwork({url: factory})
                ns = load_media_namespace(net)
                resp = self.call(ns, route, url)
                self.assertEqual(resp.status_code, 502)
                self.assertEqual(resp.get_data(), b"")
                self.assertEqual(net.calls, [url])

    def test_routes_delegate_to_shared_boundary(self):
        net = FakeNetwork({IMG_URL: ok("image/jpeg", b"i"), VID_URL: ok("video/mp4", b"v")})
        ns = load_media_namespace(net)
        real, seen = ns["safe_fetch_media"], []
        ns["safe_fetch_media"] = lambda url, kind: (seen.append((url, kind)), real(url, kind))[1]
        self.call(ns, "img", IMG_URL)
        self.call(ns, "vid", VID_URL)
        self.assertEqual(seen, [(IMG_URL, "image"), (VID_URL, "video")])


class TestFetchMediaUsesBoundary(unittest.TestCase):

    def test_fetch_media_delegates_with_kind(self):
        net = FakeNetwork({IMG_URL: ok("image/jpeg", b"i"), VID_URL: ok("video/mp4", b"v")})
        ns = load_media_namespace(net)
        real, seen = ns["safe_fetch_media"], []
        ns["safe_fetch_media"] = lambda url, kind: (seen.append((url, kind)), real(url, kind))[1]
        self.assertEqual(ns["_fetch_media"](VID_URL, "video"), (b"v", "video/mp4"))
        self.assertEqual(ns["_fetch_media"](IMG_URL, "image"), (b"i", "image/jpeg"))
        self.assertEqual(ns["_fetch_media"](IMG_URL), (b"i", "image/jpeg"))
        self.assertEqual(seen, [(VID_URL, "video"), (IMG_URL, "image"), (IMG_URL, "media")])

    def test_fetch_media_rejects_disallowed_url_with_zero_requests(self):
        net = FakeNetwork()
        ns = load_media_namespace(net)
        for url in ("https://example.com/fbcdn.net/x.mp4", "https://127.0.0.1/x.mp4", "file:///etc/passwd"):
            with self.subTest(url=url):
                self.assertEqual(ns["_fetch_media"](url, "video"), (None, None))
        self.assertEqual(net.calls, [])

    def test_fetch_media_rejects_wrong_type_and_offsite_redirect(self):
        net = FakeNetwork({
            VID_URL: ok("text/html", b"<html>"),
            IMG_URL: redirect("https://example.com/x.jpg"),
        })
        ns = load_media_namespace(net)
        self.assertEqual(ns["_fetch_media"](VID_URL, "video"), (None, None))
        self.assertEqual(ns["_fetch_media"](IMG_URL, "image"), (None, None))
        self.assertEqual(net.calls, [VID_URL, IMG_URL])

    def test_gemini_callers_pass_expected_kind(self):
        for fn in ("gemini_analyze", "gemini_analyze_beats"):
            with self.subTest(fn=fn):
                src = extract_block(rf"^def {fn}\(")
                self.assertIn('_fetch_media(vid_urls[0], "video")', src)
                self.assertIn('_fetch_media(img_urls[0], "image")', src)

    def test_no_media_path_bypasses_the_boundary(self):
        for pattern in (r"^def proxy_img\(", r"^def proxy_vid\(", r"^def _proxy_media_route\(",
                        r"^def _fetch_media\("):
            with self.subTest(pattern=pattern):
                src = extract_block(pattern)
                self.assertNotIn("urlopen", src)
                self.assertNotIn("urllib", src)
        self.assertNotRegex(APP_SRC, r"""["']fbcdn\.net["']\s+(not\s+)?in\s""")


# ── Higgsfield request_id ────────────────────────────────────────────────────

class FakeJSONResponse:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def load_higgsfield_namespace():
    """Real status routes with urllib replaced by a recording fake and a
    placeholder (non-secret) auth string."""
    requests_built, opened = [], []

    def fake_request(url, *args, **kwargs):
        requests_built.append(url)
        return urllib.request.Request(url, *args, **kwargs)

    def fake_urlopen(req, timeout=None):
        opened.append(req.full_url)
        return FakeJSONResponse({"status": "queued"})

    fake_urllib = types.SimpleNamespace(
        request=types.SimpleNamespace(Request=fake_request, urlopen=fake_urlopen),
        error=urllib.error,
    )
    ns = {
        "re": re, "json": json, "urllib": fake_urllib,
        "_HF_UA": "test-agent",
        "_higgsfield_auth": lambda: "Key placeholder:placeholder",
        "request": flask.request, "jsonify": flask.jsonify,
    }
    exec(extract_block(r"^HIGGSFIELD_REQUEST_ID_RE = "), ns)  # noqa: S102
    exec(extract_block(r"^def valid_higgsfield_request_id\("), ns)  # noqa: S102
    exec(extract_block(r"^def _hf_extract_image\("), ns)  # noqa: S102
    exec(extract_block(r"^def generate_image_status\("), ns)  # noqa: S102
    exec(extract_block(r"^def generate_video_status\("), ns)  # noqa: S102
    ns["app"] = flask.Flask("higgsfield-test")
    return ns, requests_built, opened


class TestHiggsfieldRequestIdRegex(unittest.TestCase):

    def test_pattern_is_exactly_as_specified(self):
        self.assertIn('HIGGSFIELD_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")', APP_SRC)


@unittest.skipIf(flask is None, "flask not installed")
class TestHiggsfieldStatusRoutes(unittest.TestCase):

    ROUTES = (("generate_image_status", "/generate/image/status"),
              ("generate_video_status", "/generate/video/status"))

    def post(self, ns, fn, path, body):
        with ns["app"].test_request_context(path, method="POST", json=body):
            result = ns[fn]()
        resp, status = result if isinstance(result, tuple) else (result, 200)
        return resp, status

    def test_invalid_request_id_rejected_with_zero_upstream_requests(self):
        invalid = [
            "", "../../v1/admin", "abc/def", "abc/../status", "a" * 65, "abc?x=1", "abc#frag",
            "abc%2F..", "abc\n", "abc\r\nHost: evil", " abc", "abc def", "abc.def", "abc_def",
            "ünicode", "abc\x00", 123, None, ["abc"], {"id": "abc"}, True,
        ]
        for fn, path in self.ROUTES:
            for rid in invalid:
                with self.subTest(route=path, rid=rid):
                    ns, built, opened = load_higgsfield_namespace()
                    resp, status = self.post(ns, fn, path, {"request_id": rid})
                    self.assertEqual(status, 400)
                    self.assertEqual(resp.get_json()["status"], "error")
                    self.assertEqual(built, [])
                    self.assertEqual(opened, [])

    def test_missing_request_id_rejected_with_zero_upstream_requests(self):
        for fn, path in self.ROUTES:
            with self.subTest(route=path):
                ns, built, opened = load_higgsfield_namespace()
                _, status = self.post(ns, fn, path, {})
                self.assertEqual(status, 400)
                self.assertEqual((built, opened), ([], []))

    def test_valid_request_id_builds_exact_upstream_path(self):
        for rid in ("0f2c9a4e-1b2d-4c3e-8f9a-123456789abc", "A" * 64, "x"):
            for fn, path in self.ROUTES:
                with self.subTest(route=path, rid=rid):
                    ns, built, opened = load_higgsfield_namespace()
                    _, status = self.post(ns, fn, path, {"request_id": rid})
                    self.assertEqual(status, 200)
                    expected = f"https://platform.higgsfield.ai/requests/{rid}/status"
                    self.assertEqual(opened, [expected])


if __name__ == "__main__":
    unittest.main()
