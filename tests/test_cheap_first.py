"""Cheap-first exact-page scraping (CHEAP_FIRST) and Apify cost logging.

- adapt_cheap_ad maps cheap-actor items (snake_case or camelCase, missing
  keys, garbage) onto the curious_coder-style record without ever raising.
- cheap_result_acceptable accepts only a non-empty result where EVERY ad is
  gated_type ELIGIBLE and has an image or video.
- run_job with CHEAP_FIRST on: exact-page URLs try the cheap actor first (no
  cookies in its payload); unacceptable/failed URLs fall back to ONE
  meta_auth_search call with only those URLs; all-cheap jobs never call it.
  With the flag off, or for keyword URLs, the old path runs unchanged.
- Every recorded run carries usage_usd and an actor label; the job cost sums.

Loads app.py source blocks the same way as the other test modules (see
tests/test_auth_state.py for why `import app` is not used). No network, no
sleep: api_post / wait_for_run / meta_auth_search are in-memory fakes and
search threads run synchronously.
"""
from __future__ import annotations

import copy
import json
import os
import re
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from urllib.parse import quote as urlquote

from test_auth_state import load_auth_namespace
from test_scrape_api import extract_block, extract_region, flask, load_api_namespace, make_ad

SECRET_COOKIE_VALUE = "xs-CHEAP-FIRST-SECRET-DO-NOT-SEND"
COOKIES = [{"name": "xs", "value": SECRET_COOKIE_VALUE, "domain": ".facebook.com"}]
CHEAP_ACTOR_ID = "fake-cheap-actor"
FLAG_ON = {"CHEAP_FIRST": "1", "CHEAP_ACTOR_ID": CHEAP_ACTOR_ID}
PAGE_A, PAGE_B = "111111111", "222222222"

RUN_JOB_BLOCKS = (
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
    r"^APIFY_TELEMETRY_FIELDS = \(",
    r"^MAX_APIFY_RUNS_PER_JOB = ",
    r"^def _apify_run_summary\(",
    r"^def adapt_cheap_ad\(",
    r"^def cheap_result_acceptable\(",
    r"^def cheap_actor_search\(",
    r"^def cheap_first_search\(",
    r"^def exact_page_zero_warning\(",
    r"^def run_job\(",
    r"^def job_apify_cost_usd\(",
)


class SyncThreading:
    class Thread:
        def __init__(self, target, args=(), kwargs=None, daemon=None):
            self.target, self.args, self.kwargs = target, args, kwargs or {}

        def start(self):
            self.target(*self.args, **self.kwargs)

        def join(self):
            pass


def cheap_item(aid, page_id, gated="ELIGIBLE", media=True, video=False):
    ad = make_ad(aid, page_id=page_id, video=video)
    ad["gated_type"] = gated
    if not media:
        ad["snapshot"].pop("images", None)
        ad["snapshot"].pop("videos", None)
    return ad


def cheap_items_for(page_id, mode):
    if mode in ("eligible", "failed"):
        return [cheap_item(page_id + "01", page_id), cheap_item(page_id + "02", page_id, video=True)]
    if mode == "logged_out":
        return [cheap_item(page_id + "01", page_id, "LOGGED_OUT"), cheap_item(page_id + "02", page_id, "LOGGED_OUT")]
    if mode == "mixed":
        return [cheap_item(page_id + "01", page_id), cheap_item(page_id + "02", page_id, "LOGGED_OUT")]
    if mode == "nomedia":
        return [cheap_item(page_id + "01", page_id), cheap_item(page_id + "02", page_id, media=False)]
    if mode == "empty":
        return []
    raise AssertionError(mode)


class FakeApify:
    """api_post / wait_for_run / meta_auth_search fakes. cheap_pages maps a
    page ID to how the cheap actor behaves for it (default 'eligible')."""

    def __init__(self, cheap_pages=None, usage=0.012):
        self.cheap_pages = cheap_pages or {}
        self.usage = usage
        self.posts = []
        self.auth_calls = []
        self.runs = {}

    def cheap_posts(self):
        return [(p, body) for p, body in self.posts if p.startswith(f"acts/{CHEAP_ACTOR_ID}/runs")]

    def api_post(self, path, payload):
        self.posts.append((path, copy.deepcopy(payload)))
        run_id = f"run{len(self.posts)}"
        if path.startswith(f"acts/{CHEAP_ACTOR_ID}/runs"):
            page_id = re.search(r"view_all_page_id=(\d+)", payload["startUrls"][0]["url"]).group(1)
            mode = self.cheap_pages.get(page_id, "eligible")
            if mode == "raise":
                raise Exception("Apify POST acts/x/runs → HTTP 500: boom-body")
            self.runs[run_id] = (page_id, mode)
        else:
            self.runs[run_id] = (None, "curious")
        return {"data": {"id": run_id}}

    def wait_for_run(self, run_id, log):
        page_id, mode = self.runs[run_id]
        items = [make_ad("5001")] if mode == "curious" else cheap_items_for(page_id, mode)
        status = "FAILED" if mode == "failed" else "SUCCEEDED"
        return copy.deepcopy(items), {"status": status, "status_message": None, "telemetry": None,
                                      "usage_usd": self.usage}

    def meta_auth_search(self, search_urls, cookies_list, count, country, ad_status, log):
        self.auth_calls.append({"urls": [u["url"] for u in search_urls], "cookies": cookies_list, "count": count})
        state = {"cookies_stored": True, "session_valid": True, "session_expired": False,
                 "checkpoint_required": False, "csrf_tokens_missing": False, "status": "verified",
                 "apify_run": {"run_id": "auth1", "status": "SUCCEEDED", "ads": 1, "error": False,
                               "telemetry": None, "usage_usd": 0.2, "actor": "auth"}}
        return [make_ad("9001", page_id="999999999")], state


def load_namespace(env=None, fake=None):
    """Real config lines, cheap-first helpers, run_job and its helpers from
    app.py, with the Apify seams replaced by `fake`."""
    fake = fake or FakeApify()
    ns = {
        "os": os, "re": re, "json": json, "urlquote": urlquote,
        "datetime": datetime, "timezone": timezone, "threading": SyncThreading,
        "jobs": {},
        "api_post": fake.api_post, "wait_for_run": fake.wait_for_run, "meta_auth_search": fake.meta_auth_search,
        "translate_ads_bulk": lambda ads, log: None,
        "build_viewer": lambda brand, country, ads: f"<html>{len(ads)}</html>",
        "AUTH_ACTOR_COUNT": 100, "META_ACTOR": "fake-meta-actor",
    }
    exec(extract_block(r"^def parse_auth_actor_count\("), ns)  # noqa: S102
    with patch.dict(os.environ, env or {}, clear=True):
        exec(extract_region("def parse_env_flag(", "APIFY_BASE"), ns)  # noqa: S102
    for pattern in RUN_JOB_BLOCKS:
        exec(extract_block(pattern), ns)  # noqa: S102
    return ns


def page_url(page_id):
    return f"https://www.facebook.com/profile.php?id={page_id}"


def run_job(env, page_ids=(), keywords=(), cookies=COOKIES, cheap_pages=None, usage=0.012):
    fake = FakeApify(cheap_pages, usage)
    ns = load_namespace(env, fake)
    ns["jobs"]["j"] = {"status": "running", "log": []}
    ns["run_job"]("j", "Brand", "US", [list(keywords)], [], [page_url(p) for p in page_ids], "active",
                  cookies=cookies, per_page=0)
    job = ns["jobs"]["j"]
    assert job["status"] == "done", job["log"]
    return fake, job, ns


# ── Config ───────────────────────────────────────────────────────────────────

class TestCheapConfig(unittest.TestCase):
    def test_flag_only_on_for_explicit_truthy_values(self):
        for raw in ("1", "true", "TRUE", "yes", "on", " On "):
            self.assertIs(load_namespace({"CHEAP_FIRST": raw})["CHEAP_FIRST"], True, raw)
        for raw in ("", "0", "false", "no", "off", "maybe"):
            self.assertIs(load_namespace({"CHEAP_FIRST": raw})["CHEAP_FIRST"], False, raw)
        self.assertIs(load_namespace({})["CHEAP_FIRST"], False)

    def test_defaults_and_count_validation(self):
        ns = load_namespace({})
        self.assertEqual(ns["CHEAP_ACTOR"], "apidojo~facebook-ads-scraper")
        self.assertEqual(ns["CHEAP_ACTOR_BUILD"], "")
        self.assertEqual(ns["CHEAP_ACTOR_COUNT"], 60)
        self.assertEqual(load_namespace({"CHEAP_ACTOR_COUNT": "30"})["CHEAP_ACTOR_COUNT"], 30)
        for raw in ("abc", "0", "500", "-3", "2.5"):
            self.assertEqual(load_namespace({"CHEAP_ACTOR_COUNT": raw})["CHEAP_ACTOR_COUNT"], 60, raw)


# ── adapt_cheap_ad ───────────────────────────────────────────────────────────

class TestAdaptCheapAd(unittest.TestCase):
    def setUp(self):
        self.ns = load_namespace({})
        self.adapt = self.ns["adapt_cheap_ad"]

    def test_snake_case_item(self):
        item = make_ad("1001", page_id=PAGE_A)
        ad = self.adapt(copy.deepcopy(item))
        self.assertEqual(ad["_source"], "cheap")
        self.assertEqual(ad["ad_archive_id"], "1001")
        self.assertEqual(ad["page_id"], PAGE_A)
        self.assertEqual(ad["page_name"], "Acme Co")
        self.assertIs(ad["is_active"], True)
        self.assertEqual(ad["start_date"], "2026-09-01")
        self.assertEqual(ad["gated_type"], "ELIGIBLE")
        self.assertEqual(ad["publisher_platform"], ["facebook", "instagram"])
        for k, v in item["snapshot"].items():  # snapshot kept as is
            self.assertEqual(ad["snapshot"][k], v)
        self.assertEqual(self.ns["extract_urls"](ad), (["https://scontent.xx.fbcdn.net/1001.jpg"], []))

    def test_camel_case_item(self):
        item = {
            "adArchiveID": 123456, "pageID": 456789, "pageName": "Camel Co", "isActive": True,
            "startDate": 1767225600, "gatedType": "ELIGIBLE", "collationCount": "3",
            "publisherPlatform": ["FACEBOOK"], "impressionsWithIndex": {"impressionsIndex": 2},
            "snapshot": {"body": {"text": "b"}, "linkUrl": "https://example.com/l",
                         "images": [{"originalImageUrl": "https://scontent.xx.fbcdn.net/c.jpg"}]},
        }
        ad = self.adapt(item)
        self.assertEqual((ad["ad_archive_id"], ad["page_id"], ad["page_name"]), ("123456", "456789", "Camel Co"))
        self.assertEqual(ad["gated_type"], "ELIGIBLE")
        self.assertEqual(ad["collation_count"], 3)
        self.assertEqual(ad["snapshot"]["linkUrl"], "https://example.com/l")  # original key kept
        self.assertEqual(ad["snapshot"]["link_url"], "https://example.com/l")
        self.assertEqual(self.ns["extract_urls"](ad)[0], ["https://scontent.xx.fbcdn.net/c.jpg"])
        rec = self.ns["normalize_ad"](ad)
        self.assertEqual((rec["landing"], rec["imp_idx"], rec["variants"]), ("https://example.com/l", 2, 3))

    def test_missing_keys(self):
        ad = self.adapt({})
        self.assertEqual(ad["_source"], "cheap")
        self.assertEqual((ad["ad_archive_id"], ad["gated_type"], ad["snapshot"]), ("", "", {}))
        self.ns["ad_record"](ad)  # downstream consumers cope with it

    def test_garbage_never_raises(self):
        for item in (None, "string", 5, [], {"snapshot": "x", "collationCount": "many", "publisherPlatform": "facebook",
                                             "snapshot_extra": object()}):
            ad = self.adapt(item)
            self.assertIsInstance(ad, dict)
            self.assertEqual(ad["_source"], "cheap")
            self.assertFalse(self.ns["cheap_result_acceptable"]([ad])[0])
        ad = self.adapt({"collationCount": "many", "publisherPlatform": "facebook"})
        self.assertEqual((ad["collation_count"], ad["publisher_platform"]), (0, ["facebook"]))


# ── cheap_result_acceptable ──────────────────────────────────────────────────

class TestCheapResultAcceptable(unittest.TestCase):
    def setUp(self):
        ns = load_namespace({})
        self.ok = ns["cheap_result_acceptable"]
        self.adapt = ns["adapt_cheap_ad"]

    def ads(self, mode):
        return [self.adapt(i) for i in cheap_items_for(PAGE_A, mode)]

    def test_all_eligible_with_media(self):
        self.assertEqual(self.ok(self.ads("eligible"))[0], True)

    def test_one_logged_out(self):
        ok, reason = self.ok(self.ads("mixed"))
        self.assertFalse(ok)
        self.assertIn("LOGGED_OUT", reason)

    def test_one_without_media(self):
        ok, reason = self.ok(self.ads("nomedia"))
        self.assertFalse(ok)
        self.assertIn("media", reason)

    def test_empty(self):
        self.assertFalse(self.ok([])[0])
        self.assertFalse(self.ok(None)[0])

    def test_profile_picture_is_not_creative(self):
        # Measured 2026-11-10: snapshot.page_profile_picture_url is the Page avatar, not ad media.
        item = {"ad_archive_id": "1", "gated_type": "ELIGIBLE",
                "snapshot": {"page_profile_picture_url": "https://example.com/avatar.jpg"}}
        self.assertFalse(self.ok([self.adapt(item)])[0])

    def test_creative_fields_from_measured_run(self):
        item = {"ad_archive_id": "2", "gated_type": "ELIGIBLE",
                "snapshot": {"images": [{"original_image_url": "https://example.com/a.jpg"}]}}
        item2 = {"ad_archive_id": "3", "gated_type": "ELIGIBLE",
                 "snapshot": {"videos": [{"video_hd_url": "https://example.com/v.mp4"}]}}
        item3 = {"ad_archive_id": "4", "gated_type": "ELIGIBLE",
                 "snapshot": {"cards": [{"original_image_url": "https://example.com/c.jpg"}]}}
        self.assertTrue(self.ok([self.adapt(i) for i in (item, item2, item3)])[0])

    def test_malformed_never_raises(self):
        self.assertFalse(self.ok([{"gated_type": "ELIGIBLE", "snapshot": {"cards": ["not-a-dict"]}}])[0])
        self.assertFalse(self.ok(["not-a-dict"])[0])


# ── run_job end to end ───────────────────────────────────────────────────────

class TestRunJobCheapFirst(unittest.TestCase):
    def test_all_eligible_uses_cheap_and_skips_auth_actor(self):
        fake, job, _ns = run_job(FLAG_ON, page_ids=[PAGE_A, PAGE_B])
        self.assertEqual(fake.auth_calls, [])
        posts = fake.cheap_posts()
        self.assertEqual(len(posts), 2)
        for path, payload in posts:
            self.assertEqual(path, f"acts/{CHEAP_ACTOR_ID}/runs")
            self.assertEqual(set(payload), {"startUrls", "resultsLimit", "maxItems"})
            self.assertEqual((payload["resultsLimit"], payload["maxItems"]), (60, 60))
            self.assertIn("view_all_page_id=", payload["startUrls"][0]["url"])
            self.assertNotIn(SECRET_COOKIE_VALUE, json.dumps(payload))
            self.assertNotIn("cookie", json.dumps(payload).lower())
        self.assertEqual(len(job["ads"]), 4)
        st = job["auth_state"]
        self.assertEqual((st["cookies_stored"], st["session_valid"], st["status"]), (True, "unknown", "unverified"))
        self.assertEqual([r["actor"] for r in job["apify_runs"]], ["cheap", "cheap"])
        self.assertIn("Cheap actor OK", "\n".join(job["log"]))
        self.assertNotIn(SECRET_COOKIE_VALUE, "\n".join(job["log"]))

    def test_logged_out_page_falls_back_alone(self):
        fake, job, _ns = run_job(FLAG_ON, page_ids=[PAGE_A, PAGE_B], cheap_pages={PAGE_B: "logged_out"})
        self.assertEqual(len(fake.auth_calls), 1)
        (url,) = fake.auth_calls[0]["urls"]
        self.assertIn(f"view_all_page_id={PAGE_B}", url)
        self.assertEqual(fake.auth_calls[0]["cookies"], COOKIES)
        self.assertEqual(fake.auth_calls[0]["count"], 100)
        self.assertEqual(len(job["ads"]), 3)  # 2 cheap from page A + 1 from the auth actor
        self.assertEqual([r["actor"] for r in job["apify_runs"]], ["cheap", "cheap", "auth"])
        self.assertEqual(job["auth_state"]["status"], "verified")
        log = "\n".join(job["log"])
        self.assertIn("fallback", log)
        self.assertIn("gated_type=LOGGED_OUT", log)

    def assert_single_fallback(self, mode, reason_fragment):
        fake, job, _ns = run_job(FLAG_ON, page_ids=[PAGE_A], cheap_pages={PAGE_A: mode})
        self.assertEqual(len(fake.auth_calls), 1, mode)
        self.assertIn(f"view_all_page_id={PAGE_A}", fake.auth_calls[0]["urls"][0])
        self.assertIn(reason_fragment, "\n".join(job["log"]), mode)
        self.assertEqual([a["ad_id"] for a in job["ads"]], ["9001"], mode)  # no cheap ads kept
        return job

    def test_mixed_page_falls_back(self):
        self.assert_single_fallback("mixed", "gated_type=LOGGED_OUT")

    def test_page_without_media_falls_back(self):
        self.assert_single_fallback("nomedia", "ad without media")

    def test_zero_ads_falls_back(self):
        self.assert_single_fallback("empty", "no ads")

    def test_non_succeeded_run_falls_back(self):
        self.assert_single_fallback("failed", "run status FAILED")

    def test_cheap_exception_falls_back_without_leaking_the_error_body(self):
        job = self.assert_single_fallback("raise", "cheap actor error (Exception)")
        self.assertNotIn("boom-body", "\n".join(job["log"]))
        cheap_run = job["apify_runs"][0]
        self.assertEqual((cheap_run["actor"], cheap_run["error"], cheap_run["usage_usd"]), ("cheap", True, None))

    def test_flag_off_is_the_old_path(self):
        fake, job, _ns = run_job({"CHEAP_ACTOR_ID": CHEAP_ACTOR_ID}, page_ids=[PAGE_A, PAGE_B])
        self.assertEqual(fake.posts, [])
        self.assertEqual(len(fake.auth_calls), 1)
        self.assertEqual(len(fake.auth_calls[0]["urls"]), 2)
        self.assertEqual([r["actor"] for r in job["apify_runs"]], ["auth"])

    def test_missing_flag_global_is_the_old_path(self):
        # Older test harnesses exec run_job without the cheap-first config.
        fake = FakeApify()
        ns = load_namespace(FLAG_ON, fake)
        del ns["CHEAP_FIRST"]
        ns["jobs"]["j"] = {"status": "running", "log": []}
        ns["run_job"]("j", "Brand", "US", [[]], [], [page_url(PAGE_A)], "active", cookies=COOKIES, per_page=0)
        self.assertEqual(fake.posts, [])
        self.assertEqual(len(fake.auth_calls), 1)

    def test_keyword_urls_with_flag_on_use_the_old_path(self):
        fake, job, _ns = run_job(FLAG_ON, keywords=["shoes"])
        self.assertEqual(fake.posts, [])
        self.assertEqual(len(fake.auth_calls), 1)
        self.assertIn("search_type=keyword_unordered", fake.auth_calls[0]["urls"][0])

    def test_keyword_plus_page_sends_only_the_keyword_to_auth(self):
        fake, job, _ns = run_job(FLAG_ON, page_ids=[PAGE_A], keywords=["shoes"])
        self.assertEqual(len(fake.cheap_posts()), 1)
        (url,) = fake.auth_calls[0]["urls"]
        self.assertIn("search_type=keyword_unordered", url)
        self.assertEqual(len(job["ads"]), 3)

    def test_cookieless_path_never_uses_the_cheap_actor(self):
        fake, job, _ns = run_job(FLAG_ON, page_ids=[PAGE_A], cookies=None)
        self.assertEqual(fake.cheap_posts(), [])
        self.assertEqual(len(fake.posts), 1)
        self.assertEqual(fake.auth_calls, [])
        self.assertEqual((job["apify_runs"][0]["actor"], job["apify_runs"][0]["usage_usd"]), ("curious", 0.012))

    def test_build_tag_goes_on_the_run_post(self):
        fake, _job, _ns = run_job(dict(FLAG_ON, CHEAP_ACTOR_BUILD="beta"), page_ids=[PAGE_A])
        self.assertEqual(fake.cheap_posts()[0][0], f"acts/{CHEAP_ACTOR_ID}/runs?build=beta")


# ── Cost logging ─────────────────────────────────────────────────────────────

class TestCostLogging(unittest.TestCase):
    def auth_run(self, usage):
        def fake_api_get(path):
            if path.startswith("actor-runs/"):
                data = {"status": "SUCCEEDED", "statusMessage": None, "defaultDatasetId": "ds1"}
                if usage is not None:
                    data["usageTotalUsd"] = usage
                return {"data": data}
            if path.startswith("datasets/"):
                return [{"ad_archive_id": "1"}]
            raise AssertionError(path)

        ns = load_auth_namespace(api_get=fake_api_get, api_post=lambda path, payload: {"data": {"id": "run1"}})
        _ads, run_meta = ns["wait_for_run"]("run1", log=lambda m: None)
        _ads, state = ns["meta_auth_search"]([{"url": "https://www.facebook.com/ads/library/?view_all_page_id=1"}],
                                             COOKIES, 3, "US", "active", lambda m: None)
        return run_meta, state["apify_run"]

    def test_usage_is_recorded_on_auth_runs(self):
        run_meta, run = self.auth_run(0.012)
        self.assertEqual(run_meta["usage_usd"], 0.012)
        self.assertEqual((run["usage_usd"], run["actor"]), (0.012, "auth"))

    def test_missing_usage_is_none(self):
        run_meta, run = self.auth_run(None)
        self.assertIsNone(run_meta["usage_usd"])
        self.assertIsNone(run["usage_usd"])
        self.assertIsNone(self.auth_run("garbage")[1]["usage_usd"])

    def test_cheap_runs_in_apify_runs_carry_usage(self):
        _fake, job, _ns = run_job(FLAG_ON, page_ids=[PAGE_A], usage=0.012)
        self.assertEqual([(r["actor"], r["usage_usd"]) for r in job["apify_runs"]], [("cheap", 0.012)])
        _fake, job, _ns = run_job(FLAG_ON, page_ids=[PAGE_A], usage=None)
        self.assertIsNone(job["apify_runs"][0]["usage_usd"])

    def test_run_summary_is_tolerant(self):
        summary = load_namespace({})["_apify_run_summary"]
        meta = {"status": "SUCCEEDED", "telemetry": None}
        self.assertIsNone(summary("r1", meta, 0)["usage_usd"])
        self.assertIsNone(summary("r1", meta, 0)["actor"])
        self.assertEqual(summary("r1", meta, 0, usage_usd="0.5", actor="curious")["usage_usd"], 0.5)
        for bad in ("abc", float("nan"), float("inf"), -1, True, [1]):
            self.assertIsNone(summary("r1", meta, 0, usage_usd=bad)["usage_usd"], bad)
        self.assertIsNone(summary("r1", meta, 0, actor="other")["actor"])

    def test_job_cost_sums_known_usage(self):
        cost = load_namespace({})["job_apify_cost_usd"]
        self.assertAlmostEqual(cost([{"usage_usd": 0.012}, {"usage_usd": None}, {"usage_usd": 0.2}]), 0.212)
        self.assertIsNone(cost([{"usage_usd": None}, {}]))
        self.assertIsNone(cost([]))
        self.assertIsNone(cost(None))
        self.assertEqual(cost([{"usage_usd": True}, {"usage_usd": 0.5}, "junk"]), 0.5)
        _fake, job, ns = run_job(FLAG_ON, page_ids=[PAGE_A, PAGE_B], cheap_pages={PAGE_B: "logged_out"})
        self.assertAlmostEqual(ns["job_apify_cost_usd"](job["apify_runs"]), 0.012 + 0.012 + 0.2)

    @unittest.skipIf(flask is None, "flask not installed")
    def test_api_job_summary_exposes_cost(self):
        ns, _calls, _threads = load_api_namespace()
        job = {"status": "done", "apify_runs": [{"usage_usd": 0.25, "actor": "cheap"}, {"usage_usd": 0.5}]}
        self.assertEqual(ns["_api_job_summary"]("j", job)["apify_cost_usd"], 0.75)
        self.assertIsNone(ns["_api_job_summary"]("j", {"status": "done"})["apify_cost_usd"])


if __name__ == "__main__":
    unittest.main()
