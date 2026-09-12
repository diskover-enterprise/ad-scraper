"""Regression tests for the exact-page Active Only URL defect.

Root cause: fb_page_to_adlib_url() accepted a `status` parameter but ignored
it, hardcoding `active_status=all` on every generated competitor/exact-page
Ad Library URL -- regardless of whether the form was set to "Active Only" or
"All Ads". Keyword and landing-domain URLs (built inline in run_job) already
honored the selected status correctly; only the page-ID/exact-page path was
wrong.

Why these tests load source text instead of `import app`
----------------------------------------------------------
app.py contains a pre-existing PEP 701 f-string construct that only Python
3.12+ can parse, so `import app` fails at module load in older interpreters
-- unrelated to and unaffected by this fix (see tests/test_auth_state.py for
the same note). To still exercise the real shipped source, each function
under test is extracted directly from app.py's source text by name and
exec'd into an isolated namespace, with only its direct network/heavy
dependencies replaced by sanitized fakes. No network call is ever made.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path
from urllib.parse import quote as urlquote

APP_PY = Path(__file__).resolve().parent.parent / "app.py"
APP_SRC = APP_PY.read_text(encoding="utf-8")


def extract_block(start_pattern: str) -> str:
    """Extract one top-level source block (a `def ...:` or a module-level
    assignment) starting at the first line matching start_pattern, running
    until (but not including) the next line that starts at column 0."""
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


def load_url_namespace():
    """Real fb_page_to_adlib_url, exec'd from app.py's source text."""
    ns = {"re": re}
    exec(extract_block(r"^def fb_page_to_adlib_url\("), ns)  # noqa: S102
    return ns


class TestFbPageToAdlibUrlHonorsStatus(unittest.TestCase):
    """The exact-page (competitor Page ID) URL builder must reflect the
    caller-selected status instead of hardcoding active_status=all."""

    def setUp(self):
        self.ns = load_url_namespace()
        self.fb_page_to_adlib_url = self.ns["fb_page_to_adlib_url"]

    def test_active_only_produces_active_status_active(self):
        url = self.fb_page_to_adlib_url("https://facebook.com/profile.php?id=111111111", "active", "US")
        self.assertIn("active_status=active", url)
        self.assertNotIn("active_status=all", url)

    def test_all_ads_produces_active_status_all(self):
        url = self.fb_page_to_adlib_url("https://facebook.com/profile.php?id=111111111", "all", "US")
        self.assertIn("active_status=all", url)

    def test_multiple_competitor_page_ids_inherit_active_only_status(self):
        page_urls = [
            "https://facebook.com/profile.php?id=111111111",
            "https://facebook.com/profile.php?id=222222222",
            "https://www.facebook.com/pages/SomeBrand/333333333",
        ]
        urls = [self.fb_page_to_adlib_url(p, "active", "US") for p in page_urls]
        self.assertTrue(all(u is not None for u in urls))
        for url in urls:
            self.assertIn("active_status=active", url)
            self.assertNotIn("active_status=all", url)
        # each distinct page ID still lands in its own URL
        self.assertIn("view_all_page_id=111111111", urls[0])
        self.assertIn("view_all_page_id=222222222", urls[1])
        self.assertIn("view_all_page_id=333333333", urls[2])

    def test_multiple_competitor_page_ids_inherit_all_ads_status(self):
        page_urls = [
            "https://facebook.com/profile.php?id=444444444",
            "https://facebook.com/profile.php?id=555555555",
        ]
        urls = [self.fb_page_to_adlib_url(p, "all", "US") for p in page_urls]
        for url in urls:
            self.assertIn("active_status=all", url)

    def test_no_duplicate_or_conflicting_active_status_param(self):
        url = self.fb_page_to_adlib_url("https://facebook.com/profile.php?id=111111111", "active", "US")
        self.assertEqual(url.count("active_status="), 1)

    def test_unrecognized_status_falls_back_to_active_not_all(self):
        # Defensive default: an unexpected/garbage status value must not
        # silently fall back to the old "all" behavior.
        url = self.fb_page_to_adlib_url("https://facebook.com/profile.php?id=111111111", "bogus", "US")
        self.assertIn("active_status=active", url)

    def test_country_media_type_search_type_and_page_id_preserved(self):
        url = self.fb_page_to_adlib_url("https://facebook.com/profile.php?id=111111111", "active", "GB")
        self.assertIn("country=GB", url)
        self.assertIn("media_type=all", url)
        self.assertIn("search_type=page", url)
        self.assertIn("view_all_page_id=111111111", url)

    def test_already_full_adlib_url_passed_through_unchanged(self):
        # Pre-built Ad Library URLs are returned as-is; status honoring only
        # applies to URLs this function itself constructs from a page ref.
        existing = "https://www.facebook.com/ads/library/?active_status=all&id=1"
        self.assertEqual(self.fb_page_to_adlib_url(existing, "active", "US"), existing)


def load_run_job_namespace(capture, ad_status_seen):
    """Real run_job (plus its real fb_page_to_adlib_url dependency), exec'd
    from app.py's source text, with network/heavy dependencies stubbed."""
    import threading

    def fake_meta_auth_search(search_urls, cookies_list, count, country, ad_status, log):
        capture.extend(u["url"] for u in search_urls)
        ad_status_seen.append(ad_status)
        return [], {
            "cookies_stored": True, "session_valid": True, "session_expired": False,
            "checkpoint_required": False, "csrf_tokens_missing": False, "status": "verified",
        }

    def fake_combine_auth_states(states):
        return states[0] if states else {
            "cookies_stored": False, "session_valid": False, "session_expired": False,
            "checkpoint_required": False, "csrf_tokens_missing": False, "status": "unverified",
        }

    import json as json_module

    ns = {
        "re": re,
        "json": json_module,
        "threading": threading,
        "urlquote": urlquote,
        "jobs": {},
        "meta_auth_search": fake_meta_auth_search,
        "combine_auth_states": fake_combine_auth_states,
        "translate_ads_bulk": lambda ads, log: None,
        "build_viewer": lambda brand, country, ads: "",
        "api_post": lambda *a, **k: (_ for _ in ()).throw(AssertionError("api_post should not be called")),
        "wait_for_run": lambda *a, **k: (_ for _ in ()).throw(AssertionError("wait_for_run should not be called")),
        "AUTH_ACTOR_COUNT": 100,
    }
    exec(extract_block(r"^def fb_page_to_adlib_url\("), ns)  # noqa: S102
    exec(extract_block(r"^def run_job\("), ns)  # noqa: S102
    return ns


class TestRunJobUrlsHonorSelectedStatus(unittest.TestCase):
    """End-to-end: the actual list of Ad Library URLs run_job hands to the
    actor (keyword, landing-domain, and competitor Page ID searches) must
    all carry the same active_status the user selected on the form."""

    def _run(self, ad_status):
        captured_urls = []
        ad_status_seen = []
        ns = load_run_job_namespace(captured_urls, ad_status_seen)
        job_id = "test-job"
        ns["jobs"][job_id] = {"status": "running", "log": [], "html": None}
        ns["run_job"](
            job_id, "TestBrand", "US",
            searches=[["shoes"]],
            domains=["example.com"],
            page_urls=[
                "https://facebook.com/profile.php?id=111111111",
                "https://facebook.com/profile.php?id=222222222",
            ],
            ad_status=ad_status,
            cookies=[{"name": "xs", "value": "irrelevant"}],
            per_page=0,
        )
        return captured_urls, ad_status_seen, ns["jobs"][job_id]

    def _by_search_type(self, urls, search_type):
        pat = re.compile(rf"search_type={re.escape(search_type)}(&|$)")
        return [u for u in urls if pat.search(u)]

    def test_active_only_keyword_url_uses_active(self):
        urls, _, job = self._run("active")
        self.assertNotEqual(job["status"], "error", job["log"])
        keyword_urls = self._by_search_type(urls, "keyword_unordered")
        self.assertEqual(len(keyword_urls), 1)
        self.assertIn("active_status=active", keyword_urls[0])

    def test_all_ads_keyword_url_uses_all(self):
        urls, _, job = self._run("all")
        self.assertNotEqual(job["status"], "error", job["log"])
        keyword_urls = self._by_search_type(urls, "keyword_unordered")
        self.assertEqual(len(keyword_urls), 1)
        self.assertIn("active_status=all", keyword_urls[0])

    def test_active_only_landing_domain_url_uses_active(self):
        urls, _, _job = self._run("active")
        domain_urls = self._by_search_type(urls, "page_like_and_ads_using_domain")
        self.assertEqual(len(domain_urls), 1)
        self.assertIn("active_status=active", domain_urls[0])
        self.assertIn(f"q={urlquote('example.com')}", domain_urls[0])

    def test_all_ads_landing_domain_url_uses_all(self):
        urls, _, _job = self._run("all")
        domain_urls = self._by_search_type(urls, "page_like_and_ads_using_domain")
        self.assertEqual(len(domain_urls), 1)
        self.assertIn("active_status=all", domain_urls[0])

    def test_active_only_exact_page_urls_use_active_for_every_competitor(self):
        urls, _, _job = self._run("active")
        page_urls = self._by_search_type(urls, "page")
        self.assertEqual(len(page_urls), 2)
        for u in page_urls:
            self.assertIn("active_status=active", u)
            self.assertNotIn("active_status=all", u)
        self.assertTrue(any("view_all_page_id=111111111" in u for u in page_urls))
        self.assertTrue(any("view_all_page_id=222222222" in u for u in page_urls))

    def test_all_ads_exact_page_urls_use_all_for_every_competitor(self):
        urls, _, _job = self._run("all")
        page_urls = self._by_search_type(urls, "page")
        self.assertEqual(len(page_urls), 2)
        for u in page_urls:
            self.assertIn("active_status=all", u)

    def test_no_url_has_duplicate_or_conflicting_active_status_param(self):
        for ad_status in ("active", "all"):
            urls, _, _job = self._run(ad_status)
            self.assertTrue(urls, "expected at least one generated URL")
            for u in urls:
                self.assertEqual(u.count("active_status="), 1, u)

    def test_meta_auth_search_itself_receives_the_selected_status(self):
        # ad_status is also forwarded as its own argument to the actor call
        # (independent of what's embedded in each URL string).
        _urls, seen, _job = self._run("all")
        self.assertEqual(seen, ["all"])


if __name__ == "__main__":
    unittest.main()
