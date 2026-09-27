"""Tests for the JSON scrape API (/api/v1/scrapes) and job result retention.

Covers:
- POST /api/v1/scrapes starts a job through the SAME prepare_scrape ->
  launch_scrape_job -> run_job path as the browser POST /start form.
- Invalid JSON input fails cleanly (400, no job, no scrape).
- GET /api/v1/scrapes/<id> reports running / completed / failed; unknown = 404.
- GET /api/v1/scrapes/<id>/result returns the retained normalized ads only
  once completed -- never partial ads for a running job.
- Existing browser routes (/start, /status/<id>, /result/<id>) still behave
  as before.
- Neither the job record nor any API response carries cookie/credential data.

Why these tests load source text instead of `import app`
----------------------------------------------------------
Same reason as tests/test_auth_state.py: app.py contains a pre-existing PEP
701 f-string that only Python 3.12+ can parse. The real shipped functions and
routes are extracted from app.py's source and exec'd into an isolated
namespace bound to a throwaway Flask app.

Network safety
--------------
The only network seams run_job uses (meta_auth_search, api_post,
wait_for_run) are replaced by in-memory fakes; translation and HTML rendering
are stubbed. Background threads are replaced by a fake that runs targets
synchronously (or defers them, to observe a running job). Cookie values are
made-up fixtures. No Apify, Facebook, Anthropic or Railway call is ever made.
"""
from __future__ import annotations

import copy
import json
import re
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote as urlquote, urlparse

try:
    import flask
except ImportError:  # every test here needs Flask's request/jsonify
    flask = None

APP_PY = Path(__file__).resolve().parent.parent / "app.py"
APP_SRC = APP_PY.read_text(encoding="utf-8")

SECRET_COOKIE_VALUE = "xs-TOTALLY-SECRET-SESSION-VALUE-DO-NOT-LEAK"
LOCKED_COOKIES = [
    {"name": "xs", "value": SECRET_COOKIE_VALUE, "domain": ".facebook.com"},
    {"name": "c_user", "value": "999999999", "domain": ".facebook.com"},
]
FORBIDDEN_KEY_RE = re.compile(r"cookie|token|secret|password|credential|auth_header|^xs$", re.I)
# auth_state booleans: they report *whether* cookies/tokens exist, never values.
SAFE_KEYS = {"cookies_stored", "csrf_tokens_missing"}

EXPECTED_AD_KEYS = {
    # normalize_ad()
    "name", "status", "date", "body", "title", "cta", "landing", "lib_url",
    "impressions", "imp_idx", "variants", "plats", "ad_id", "page_id", "page_ads_url",
    # ad_record() additions
    "format", "images", "videos", "gated_type", "translation", "translation_language",
}

VERIFIED_STATE = {
    "cookies_stored": True, "session_valid": True, "session_expired": False,
    "checkpoint_required": False, "csrf_tokens_missing": False, "status": "verified",
}


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
    """Every line from the one starting with start_prefix up to (not
    including) the next line starting with end_prefix."""
    lines = APP_SRC.splitlines()
    start = next((i for i, l in enumerate(lines) if l.startswith(start_prefix)), None)
    if start is None:
        raise AssertionError(f"region start {start_prefix!r} not found in app.py")
    end = next((j for j in range(start + 1, len(lines)) if lines[j].startswith(end_prefix)), None)
    if end is None:
        raise AssertionError(f"region end {end_prefix!r} not found in app.py")
    return "\n".join(lines[start:end])


def make_ad(aid, page_id="111111111", name="Acme Co", video=False):
    """A curious_coder-shaped actor record (all values are fixtures)."""
    snap = {
        "page_name": name,
        "body": {"text": f"Body copy {aid}"},
        "title": {"text": f"Title {aid}"},
        "cta_text": "Shop Now",
        "link_url": "https://example.com/landing",
    }
    if video:
        snap["videos"] = [{
            "video_hd_url": f"https://video.xx.fbcdn.net/{aid}.mp4",
            "video_preview_image_url": f"https://scontent.xx.fbcdn.net/{aid}_thumb.jpg",
        }]
    else:
        snap["images"] = [{"original_image_url": f"https://scontent.xx.fbcdn.net/{aid}.jpg"}]
    return {
        "ad_archive_id": aid,
        "page_id": page_id,
        "page_name": name,
        "is_active": True,
        "start_date": "2026-09-01",
        "publisher_platform": ["facebook", "instagram"],
        "gated_type": "ELIGIBLE",
        "collation_id": "raw-actor-field-not-retained",
        "snapshot": snap,
    }


# Two unique ads plus a duplicate of the first (run_job dedupes by archive ID).
ACTOR_ADS = [make_ad("1001"), make_ad("1002", video=True), make_ad("1001")]


class FakeThreading:
    """Stand-in for the threading module. Search threads inside run_job
    always run synchronously; the job thread started by launch_scrape_job
    (the only daemon=True thread) runs synchronously too unless `defer` is
    set, in which case it is held so a test can observe a running job."""

    def __init__(self, defer=False):
        self.defer = defer
        self.deferred = []
        outer = self

        class Thread:
            def __init__(self, target, args=(), kwargs=None, daemon=None):
                self.target, self.args, self.kwargs, self.daemon = target, args, kwargs or {}, daemon

            def start(self):
                if outer.defer and self.daemon:
                    outer.deferred.append(self)
                else:
                    self.target(*self.args, **self.kwargs)

            def join(self):
                pass

        self.Thread = Thread

    def run_deferred(self):
        while self.deferred:
            t = self.deferred.pop(0)
            t.target(*t.args, **t.kwargs)


def load_api_namespace(locked_cookies=None, actor_ads=ACTOR_ADS, defer=False, viewer_error=None):
    """Real run_job, ad normalization, auth-state combining, job launch, and
    the browser + JSON routes from app.py, exec'd against a test Flask app
    with every network seam faked."""
    calls = {"run_job": [], "meta_auth_search": [], "api_post": []}
    threading_fake = FakeThreading(defer)

    def fake_meta_auth_search(search_urls, cookies_list, count, country, ad_status, log):
        calls["meta_auth_search"].append({
            "urls": [u["url"] for u in search_urls], "cookies": cookies_list, "ad_status": ad_status,
        })
        return copy.deepcopy(list(actor_ads)), dict(VERIFIED_STATE)

    def fake_api_post(path, payload):
        calls["api_post"].append({"path": path, "urls": [u["url"] for u in payload["urls"]]})
        return {"data": {"id": "run-1"}}

    def fake_wait_for_run(run_id, log):
        return copy.deepcopy(list(actor_ads)), {"status": "SUCCEEDED", "status_message": None, "telemetry": None}

    def fake_build_viewer(brand, country, ads):
        if viewer_error:
            raise RuntimeError(viewer_error)
        return f"<html>{len(ads)} ads</html>"

    ns = {
        "re": re, "json": json, "uuid": uuid, "urlquote": urlquote, "urlparse": urlparse,
        "datetime": datetime, "timezone": timezone,
        "threading": threading_fake,
        "jobs": {}, "last_job_id": None,
        "get_locked_cookies": lambda: copy.deepcopy(locked_cookies) if locked_cookies else None,
        "meta_auth_search": fake_meta_auth_search,
        "api_post": fake_api_post,
        "wait_for_run": fake_wait_for_run,
        "translate_ads_bulk": lambda ads, log: None,
        "build_viewer": fake_build_viewer,
        "render_template_string": lambda tpl, **kw: f"progress:{kw['job_id']}:{kw['brand']}",
        "PROGRESS_HTML": "",
        "AUTH_ACTOR_COUNT": 100,
        "META_ACTOR": "fake-meta-actor",
        "request": flask.request, "jsonify": flask.jsonify,
    }
    app = flask.Flask("scrape-api-test")
    app.testing = True
    ns["app"] = app

    # exec runs the exact shipped source (see module docstring).
    for pattern in (
        r"^COUNTRIES = \[",
        r"^AUTH_FAILURE_SIGNATURES = \{",
        r"^AUTH_STATUS_PRECEDENCE = \(",
        r"^def _normalize_telemetry_entries\(",
        r"^def _classify_telemetry_entry\(",
        r"^def classify_auth_state\(",
        r"^def combine_auth_states\(",
        r"^def fb_page_to_adlib_url\(",
        r"^def extract_urls\(",
        r"^def normalize_ad\(",
        r"^def ad_record\(",
        r"^def run_job\(",
        r"^def start\(",
        r"^def status\(",
        r"^def result\(",
    ):
        exec(extract_block(pattern), ns)  # noqa: S102
    exec(extract_region("# ── Scrape job launch", "# ── Routes"), ns)  # noqa: S102
    # The API region carries its own @app.route decorators -> registers on `app`.
    exec(extract_region("# ── JSON scrape API", "if __name__"), ns)  # noqa: S102

    app.add_url_rule("/start", "start", ns["start"], methods=["POST"])
    app.add_url_rule("/status/<job_id>", "status", ns["status"])
    app.add_url_rule("/result/<job_id>", "result", ns["result"])

    real_run_job = ns["run_job"]

    def spy_run_job(*args, **kwargs):
        calls["run_job"].append((args, kwargs))
        return real_run_job(*args, **kwargs)

    ns["run_job"] = spy_run_job  # launch_scrape_job resolves run_job at call time
    return ns, calls, threading_fake


def forbidden_keys(obj, path="$"):
    """Every dict key (recursively) that looks like credential material."""
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k not in SAFE_KEYS and FORBIDDEN_KEY_RE.search(str(k)):
                found.append(f"{path}.{k}")
            found.extend(forbidden_keys(v, f"{path}.{k}"))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            found.extend(forbidden_keys(v, f"{path}[{i}]"))
    return found


VALID_BODY = {
    "page_ids": ["111111111"],
    "page_urls": ["https://www.facebook.com/profile.php?id=222222222"],
    "country": "gb",
    "ad_status": "all",
    "per_page": 3,
}


@unittest.skipIf(flask is None, "flask not installed")
class ApiTestCase(unittest.TestCase):

    def setUpNamespace(self, **kwargs):
        self.ns, self.calls, self.threads = load_api_namespace(**kwargs)
        self.client = self.ns["app"].test_client()

    def api_start(self, body=VALID_BODY):
        return self.client.post("/api/v1/scrapes", json=body)


# 1. JSON start goes through the existing scrape path ─────────────────────────

class TestApiStart(ApiTestCase):

    def test_json_start_creates_job_through_run_job(self):
        self.setUpNamespace(locked_cookies=LOCKED_COOKIES)
        resp = self.api_start()
        self.assertEqual(resp.status_code, 202)
        data = resp.get_json()
        job_id = data["job_id"]
        self.assertEqual(data["status"], "running")
        self.assertEqual(data["status_url"], f"/api/v1/scrapes/{job_id}")
        self.assertEqual(data["result_url"], f"/api/v1/scrapes/{job_id}/result")

        self.assertEqual(len(self.calls["run_job"]), 1)
        args, kwargs = self.calls["run_job"][0]
        self.assertEqual(args, (job_id, "111111111", "GB", [[]], [],
                                ["111111111", "https://www.facebook.com/profile.php?id=222222222"], "all"))
        self.assertEqual(kwargs, {"cookies": LOCKED_COOKIES, "per_page": 3})

        # run_job built the page-search URLs with the requested status/country
        urls = self.calls["meta_auth_search"][0]["urls"]
        self.assertEqual(len(urls), 2)
        for u in urls:
            self.assertIn("active_status=all", u)
            self.assertIn("country=GB", u)
        self.assertTrue(any("view_all_page_id=111111111" in u for u in urls))
        self.assertTrue(any("view_all_page_id=222222222" in u for u in urls))
        self.assertEqual(self.ns["jobs"][job_id]["status"], "done")
        self.assertEqual(self.ns["last_job_id"], job_id)

    def test_json_and_browser_starts_hand_identical_args_to_run_job(self):
        self.setUpNamespace(locked_cookies=LOCKED_COOKIES)
        self.api_start({"page_ids": ["111111111"], "keywords": ["shoes, boots"],
                        "domains": ["https://example.com/x"], "country": "US",
                        "ad_status": "active", "per_page": 2})
        self.client.post("/start", data={
            "pages_bulk": "111111111", "keywords_bulk": "shoes, boots",
            "domains_bulk": "https://example.com/x", "country": "US",
            "ad_status": "active", "per_page": "2", "cookies": "",
        })
        (api_args, api_kwargs), (form_args, form_kwargs) = self.calls["run_job"]
        self.assertEqual(api_args[1:], form_args[1:])  # everything but job_id
        self.assertEqual(api_kwargs, form_kwargs)
        self.assertEqual(api_args[3], [["shoes", "boots"]])
        self.assertEqual(api_args[4], ["example.com"])

    def test_json_start_without_locked_cookies_uses_unauthenticated_actor(self):
        self.setUpNamespace(locked_cookies=None)
        resp = self.api_start({"page_ids": [111111111]})  # integer IDs accepted
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(self.calls["meta_auth_search"], [])
        self.assertEqual(len(self.calls["api_post"]), 1)
        (url,) = self.calls["api_post"][0]["urls"]
        self.assertIn("view_all_page_id=111111111", url)
        self.assertIn("active_status=active", url)  # default status
        self.assertIn("country=US", url)            # default country
        _args, kwargs = self.calls["run_job"][0]
        self.assertIsNone(kwargs["cookies"])


# 2. Invalid input fails cleanly ─────────────────────────────────────────────

class TestApiInvalidInput(ApiTestCase):

    BAD_BODIES = [
        [],
        "just a string",
        {},
        {"page_ids": []},
        {"page_ids": "111111111"},
        {"page_ids": ["abc"]},
        {"page_ids": ["12345"]},
        {"page_ids": [True]},
        {"page_ids": [{"id": "111111111"}]},
        {"page_urls": ["https://example.com/not-a-page"]},
        {"page_urls": [123456789]},
        {"keywords": "shoes"},
        {"page_ids": ["111111111"], "country": "ZZ"},
        {"page_ids": ["111111111"], "country": 5},
        {"page_ids": ["111111111"], "ad_status": "inactive"},
        {"page_ids": ["111111111"], "per_page": -1},
        {"page_ids": ["111111111"], "per_page": "5"},
        {"page_ids": ["111111111"], "per_page": 1.5},
        {"page_ids": ["111111111"], "per_page": True},
        {"page_ids": ["111111111"], "limit": 10},
        {"page_ids": ["111111111"], "cookies": [{"name": "xs", "value": SECRET_COOKIE_VALUE}]},
    ]

    def assert_rejected(self, resp):
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"]["code"], "invalid_request")
        self.assertTrue(resp.get_json()["error"]["message"])
        self.assertEqual(self.ns["jobs"], {})
        self.assertEqual(self.calls["run_job"], [])
        self.assertEqual(self.calls["meta_auth_search"], [])
        self.assertEqual(self.calls["api_post"], [])

    def test_invalid_bodies_rejected_without_starting_a_job(self):
        for body in self.BAD_BODIES:
            with self.subTest(body=body):
                self.setUpNamespace(locked_cookies=LOCKED_COOKIES)
                self.assert_rejected(self.api_start(body))

    def test_non_json_and_malformed_json_rejected(self):
        for data, ctype in (("page_ids=111111111", "application/x-www-form-urlencoded"),
                            ("{not json", "application/json"),
                            ("", "application/json")):
            with self.subTest(data=data, ctype=ctype):
                self.setUpNamespace()
                self.assert_rejected(self.client.post("/api/v1/scrapes", data=data, content_type=ctype))

    def test_error_messages_do_not_echo_submitted_values(self):
        self.setUpNamespace()
        bad_url = "https://example.com/private-path-xyz"
        resp = self.api_start({"page_urls": ["https://www.facebook.com/profile.php?id=222222222", bad_url]})
        msg = resp.get_json()["error"]["message"]
        self.assertIn("page_urls[1]", msg)
        self.assertNotIn(bad_url, msg)

        self.setUpNamespace()
        resp = self.api_start({"page_ids": ["111111111"], "cookies": SECRET_COOKIE_VALUE})
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn(SECRET_COOKIE_VALUE, resp.get_data(as_text=True))


# 3 + 6. Status: running / completed / failed / unknown ──────────────────────

class TestApiStatus(ApiTestCase):

    def test_running_status(self):
        self.setUpNamespace(locked_cookies=LOCKED_COOKIES, defer=True)
        job_id = self.api_start().get_json()["job_id"]
        resp = self.client.get(f"/api/v1/scrapes/{job_id}")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["job_id"], job_id)
        self.assertEqual(data["status"], "running")
        self.assertIsNone(data["auth_state"])
        self.assertIsInstance(data["log"], list)
        self.assertIsNone(data["completed_at"])
        self.assertTrue(data["created_at"])
        self.assertNotIn("count", data)
        self.assertNotIn("error", data)

    def test_completed_status(self):
        self.setUpNamespace(locked_cookies=LOCKED_COOKIES)
        job_id = self.api_start().get_json()["job_id"]
        data = self.client.get(f"/api/v1/scrapes/{job_id}").get_json()
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["auth_state"]["status"], "verified")
        self.assertIs(data["auth_state"]["session_valid"], True)
        self.assertTrue(data["completed_at"])
        self.assertIn("✅ Done!", data["log"])
        self.assertEqual(data["params"], {
            "country": "GB", "ad_status": "all", "per_page": 3, "searches": [], "domains": [],
            "page_urls": ["111111111", "https://www.facebook.com/profile.php?id=222222222"],
        })
        self.assertNotIn("ads", data)  # results live on the result endpoint only

    def test_failed_status(self):
        self.setUpNamespace(locked_cookies=LOCKED_COOKIES, viewer_error="viewer exploded")
        job_id = self.api_start().get_json()["job_id"]
        resp = self.client.get(f"/api/v1/scrapes/{job_id}")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["error"]["code"], "scrape_failed")
        self.assertIn("viewer exploded", data["error"]["message"])
        self.assertIsInstance(data["error"]["log_tail"], list)
        self.assertNotIn("count", data)

    def test_unknown_job_is_json_404(self):
        self.setUpNamespace()
        for path in ("/api/v1/scrapes/nope1234", "/api/v1/scrapes/nope1234/result"):
            with self.subTest(path=path):
                resp = self.client.get(path)
                self.assertEqual(resp.status_code, 404)
                self.assertTrue(resp.is_json)
                data = resp.get_json()
                self.assertEqual(data["job_id"], "nope1234")
                self.assertEqual(data["error"]["code"], "not_found")


# 4 + 5. Result: completed returns retained ads; running never partial ───────

class TestApiResult(ApiTestCase):

    def test_completed_result_returns_retained_normalized_ads(self):
        self.setUpNamespace(locked_cookies=LOCKED_COOKIES)
        job_id = self.api_start().get_json()["job_id"]
        resp = self.client.get(f"/api/v1/scrapes/{job_id}/result")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["job_id"], job_id)
        self.assertEqual(data["status"], "completed")
        self.assertIs(data["completed"], True)
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["ads"], self.ns["jobs"][job_id]["ads"])
        self.assertEqual(data["auth_state"]["status"], "verified")

        # Exactly the viewer's normalized view -- ad_record(raw) for each unique ad
        expected = [self.ns["ad_record"](ACTOR_ADS[0]), self.ns["ad_record"](ACTOR_ADS[1])]
        self.assertEqual(data["ads"], expected)

        img_ad, vid_ad = data["ads"]
        for ad in data["ads"]:
            self.assertEqual(set(ad), EXPECTED_AD_KEYS)
        self.assertEqual(img_ad["ad_id"], "1001")
        self.assertEqual(img_ad["name"], "Acme Co")
        self.assertEqual(img_ad["page_id"], "111111111")
        self.assertEqual(img_ad["status"], "ACTIVE")
        self.assertEqual(img_ad["body"], "Body copy 1001")
        self.assertEqual(img_ad["title"], "Title 1001")
        self.assertEqual(img_ad["cta"], "Shop Now")
        self.assertEqual(img_ad["landing"], "https://example.com/landing")
        self.assertEqual(img_ad["format"], "IMAGE")
        self.assertEqual(img_ad["images"], ["https://scontent.xx.fbcdn.net/1001.jpg"])
        self.assertEqual(img_ad["videos"], [])
        self.assertEqual(vid_ad["format"], "VIDEO")
        self.assertEqual(vid_ad["videos"], ["https://video.xx.fbcdn.net/1002.mp4"])
        self.assertEqual(vid_ad["images"], ["https://scontent.xx.fbcdn.net/1002_thumb.jpg"])

    def test_running_result_never_returns_partial_ads(self):
        self.setUpNamespace(locked_cookies=LOCKED_COOKIES, defer=True)
        job_id = self.api_start().get_json()["job_id"]
        # Even if something partial were sitting on the job record, a running
        # job must not surface it.
        self.ns["jobs"][job_id]["ads"] = [{"ad_id": "partial"}]
        resp = self.client.get(f"/api/v1/scrapes/{job_id}/result")
        self.assertEqual(resp.status_code, 409)
        data = resp.get_json()
        self.assertEqual(data["status"], "running")
        self.assertIs(data["completed"], False)
        self.assertEqual(data["error"]["code"], "not_completed")
        self.assertNotIn("ads", data)
        self.assertNotIn("count", data)
        self.assertNotIn("partial", resp.get_data(as_text=True))

        # Once the (deferred) job actually finishes, the real result appears.
        self.threads.run_deferred()
        resp = self.client.get(f"/api/v1/scrapes/{job_id}/result")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["count"], 2)

    def test_failed_result_is_structured_and_has_no_ads(self):
        self.setUpNamespace(locked_cookies=LOCKED_COOKIES, viewer_error="viewer exploded")
        job_id = self.api_start().get_json()["job_id"]
        self.assertNotIn("ads", self.ns["jobs"][job_id])  # not retained on failure
        resp = self.client.get(f"/api/v1/scrapes/{job_id}/result")
        self.assertEqual(resp.status_code, 409)
        data = resp.get_json()
        self.assertEqual(data["status"], "failed")
        self.assertIs(data["completed"], False)
        self.assertEqual(data["error"]["code"], "scrape_failed")
        self.assertIn("viewer exploded", data["error"]["message"])
        self.assertNotIn("ads", data)


# 7. Existing browser routes stay compatible ─────────────────────────────────

class TestBrowserRoutesCompatible(ApiTestCase):

    def test_form_start_status_and_result_flow(self):
        self.setUpNamespace(locked_cookies=LOCKED_COOKIES)
        resp = self.client.post("/start", data={
            "pages_bulk": "https://www.facebook.com/profile.php?id=222222222\n111111111",
            "country": "US", "ad_status": "active", "per_page": "0", "cookies": "",
        })
        self.assertEqual(resp.status_code, 200)
        _, job_id, brand = resp.get_data(as_text=True).split(":", 2)
        self.assertEqual(brand, "https://www.facebook.com/profile.php?id=222222222")
        args, kwargs = self.calls["run_job"][0]
        self.assertEqual(args, (job_id, brand, "US", [[]], [],
                                ["https://www.facebook.com/profile.php?id=222222222", "111111111"], "active"))
        self.assertEqual(kwargs, {"cookies": LOCKED_COOKIES, "per_page": 0})

        status = self.client.get(f"/status/{job_id}").get_json()
        self.assertEqual(set(status), {"status", "log", "auth_state"})
        self.assertEqual(status["status"], "done")

        result = self.client.get(f"/result/{job_id}")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.get_data(as_text=True), "<html>2 ads</html>")

    def test_form_cookies_textarea_still_overrides_locked_cookies(self):
        self.setUpNamespace(locked_cookies=LOCKED_COOKIES)
        form_cookies = [{"name": "xs", "value": "form-supplied"}]
        self.client.post("/start", data={"pages_bulk": "111111111", "cookies": json.dumps(form_cookies)})
        _args, kwargs = self.calls["run_job"][0]
        self.assertEqual(kwargs["cookies"], form_cookies)

    def test_form_defaults_and_empty_form_still_start_a_job(self):
        self.setUpNamespace()
        resp = self.client.post("/start", data={"per_page": "not-a-number"})
        self.assertEqual(resp.status_code, 200)
        args, kwargs = self.calls["run_job"][0]
        self.assertEqual(args[1:], ("Meta Ads", "US", [[]], [], [], "active"))
        self.assertEqual(kwargs, {"cookies": None, "per_page": 0})

    def test_legacy_result_and_status_for_running_and_unknown_jobs(self):
        self.setUpNamespace(locked_cookies=LOCKED_COOKIES, defer=True)
        job_id = self.client.post("/start", data={"pages_bulk": "111111111"}).get_data(as_text=True).split(":")[1]
        running = self.client.get(f"/result/{job_id}")
        self.assertEqual((running.status_code, running.get_data(as_text=True)), (202, "Still running or error"))
        self.assertEqual(self.client.get(f"/status/{job_id}").get_json()["status"], "running")

        missing = self.client.get("/result/nope1234")
        self.assertEqual((missing.status_code, missing.get_data(as_text=True)), (404, "Job not found"))
        self.assertEqual(self.client.get("/status/nope1234").get_json()["status"], "unknown")

    def test_form_jobs_are_also_visible_through_the_json_api(self):
        self.setUpNamespace(locked_cookies=LOCKED_COOKIES)
        job_id = self.client.post("/start", data={"pages_bulk": "111111111"}).get_data(as_text=True).split(":")[1]
        data = self.client.get(f"/api/v1/scrapes/{job_id}/result").get_json()
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["count"], 2)


# 8. No credential / cookie data retained or exposed ─────────────────────────

class TestNoSecretsRetainedOrExposed(ApiTestCase):

    def assert_clean(self, obj, label):
        text = json.dumps(obj, default=str)
        self.assertNotIn(SECRET_COOKIE_VALUE, text, label)
        self.assertNotIn("999999999", text, label)  # c_user cookie value
        self.assertEqual(forbidden_keys(obj), [], label)

    def test_api_job_and_responses_carry_no_cookie_data(self):
        self.setUpNamespace(locked_cookies=LOCKED_COOKIES)
        start = self.api_start()
        job_id = start.get_json()["job_id"]
        # Sanity: the secret really did reach the (fake) actor call, so this
        # test would catch a leak rather than pass vacuously.
        self.assertIn(SECRET_COOKIE_VALUE, json.dumps(self.calls["meta_auth_search"][0]["cookies"]))

        self.assert_clean(self.ns["jobs"][job_id], "job record")
        self.assert_clean(start.get_json(), "start response")
        self.assert_clean(self.client.get(f"/api/v1/scrapes/{job_id}").get_json(), "status response")
        self.assert_clean(self.client.get(f"/api/v1/scrapes/{job_id}/result").get_json(), "result response")
        self.assert_clean(self.client.get(f"/status/{job_id}").get_json(), "legacy status response")

    def test_form_supplied_cookies_are_not_retained_on_the_job(self):
        self.setUpNamespace()
        self.client.post("/start", data={"pages_bulk": "111111111", "cookies": json.dumps(LOCKED_COOKIES)})
        (job_id, job), = self.ns["jobs"].items()
        self.assertEqual(self.calls["run_job"][0][1]["cookies"], LOCKED_COOKIES)
        self.assert_clean(job, "job record")
        self.assert_clean(self.client.get(f"/api/v1/scrapes/{job_id}").get_json(), "status response")

    def test_cookie_junk_pasted_into_pages_box_is_not_retained_in_params(self):
        self.setUpNamespace()
        pasted = f'"value": "{SECRET_COOKIE_VALUE}",\n111111111'
        self.client.post("/start", data={"pages_bulk": pasted})
        (job_id, job), = self.ns["jobs"].items()
        self.assertEqual(job["params"]["page_urls"], ["111111111"])
        self.assertNotIn(SECRET_COOKIE_VALUE, json.dumps(job["params"]))
        self.assertNotIn(SECRET_COOKIE_VALUE, json.dumps(self.client.get(f"/api/v1/scrapes/{job_id}/result").get_json()["params"]))

    def test_retained_ads_are_allowlisted_not_raw_actor_records(self):
        self.setUpNamespace(locked_cookies=LOCKED_COOKIES)
        job_id = self.api_start().get_json()["job_id"]
        for ad in self.ns["jobs"][job_id]["ads"]:
            self.assertEqual(set(ad), EXPECTED_AD_KEYS)
            self.assertNotIn("snapshot", ad)
            self.assertNotIn("collation_id", ad)


if __name__ == "__main__":
    unittest.main()
