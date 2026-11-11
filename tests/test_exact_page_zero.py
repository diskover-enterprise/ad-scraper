"""Exact-page zero results are flagged, and every actor run is traceable.

- meta_auth_search logs the Apify run ID and returns a bounded, credential-free
  run summary (status, ad count, per-URL telemetry fields only).
- run_job keeps those runs on the job; the JSON API exposes them as `apify_runs`.
- An exact-page (view_all_page_id) job that ends with zero ads carries a distinct
  `warning`: `exact_page_zero_ads` only when the actor's own telemetry shows a
  verified session and GraphQL traffic, otherwise `exact_page_zero_unverified`.
  Keyword/domain jobs and jobs with ads never get one. Job status values are
  unchanged (still "completed"), so existing consumers behave as before.

No network: the same fakes as tests/test_auth_state.py and tests/test_scrape_api.py.
"""
from __future__ import annotations

import unittest

from test_auth_state import load_auth_namespace
from test_scrape_api import ApiTestCase, LOCKED_COOKIES, forbidden_keys, make_ad

PAGE_ID = "324822937374070"
RUN_ID = "2NZc2li3l81CFObbf"
VERIFIED_TELEMETRY = {
    "url": "https://www.facebook.com/ads/library/?view_all_page_id=" + PAGE_ID, "country": "US",
    "cookies_provided": True, "auth_status": "verified", "checkpoint_detected": False,
    "login_wall_detected": False, "graphql_responses": 3, "html_ads": 2, "graphql_ads": 0, "unique_ads": 2,
}


# Telemetry of a page that loaded (verified, GraphQL traffic) and truly had no ads.
ZERO_TELEMETRY = {"auth_status": "verified", "checkpoint_detected": False, "login_wall_detected": False,
                  "graphql_responses": 2, "html_ads": 0, "graphql_ads": 0, "unique_ads": 0}


def zero_run(entries=1, **overrides):
    entry = dict(ZERO_TELEMETRY, **overrides)
    return {"run_id": RUN_ID, "status": "SUCCEEDED", "ads": 0, "error": False,
            "telemetry": [dict(entry) for _ in range(entries)], "telemetry_truncated": False}


class TestMetaAuthSearchRecordsTheRun(unittest.TestCase):
    def run_search(self, ads, telemetry=None, fail=False):
        def fake_api_post(path, payload):
            if fail:
                raise Exception("Apify POST acts/x/runs → HTTP 500: boom")
            return {"data": {"id": RUN_ID}}

        def fake_api_get(path):
            if path.startswith("actor-runs/"):
                data = {"status": "SUCCEEDED", "statusMessage": "ok", "defaultDatasetId": "ds1"}
                if telemetry is not None:
                    data["defaultKeyValueStoreId"] = "kv1"
                return {"data": data}
            if path.startswith("datasets/"):
                return list(ads)
            if path == "key-value-stores/kv1/records/AUTH_TELEMETRY":
                return telemetry
            raise AssertionError(path)

        ns = load_auth_namespace(api_get=fake_api_get, api_post=fake_api_post)
        logs = []
        found, state = ns["meta_auth_search"](
            search_urls=[{"url": "https://www.facebook.com/ads/library/?view_all_page_id=" + PAGE_ID}],
            cookies_list=[{"name": "xs", "value": "irrelevant"}], count=3, country="US", ad_status="active",
            log=logs.append)
        return found, state, logs

    def test_run_id_is_logged_and_summarized_without_credentials(self):
        _ads, state, logs = self.run_search([{"ad_archive_id": "1"}], telemetry=[VERIFIED_TELEMETRY])
        self.assertIn(f"Apify run {RUN_ID}", "\n".join(logs))
        run = state["apify_run"]
        self.assertEqual((run["run_id"], run["status"], run["ads"], run["error"]), (RUN_ID, "SUCCEEDED", 1, False))
        self.assertEqual(run["telemetry"], [{"auth_status": "verified", "checkpoint_detected": False,
                                             "login_wall_detected": False, "graphql_responses": 3, "html_ads": 2,
                                             "graphql_ads": 0, "unique_ads": 2}])
        self.assertEqual(forbidden_keys(run), [])
        self.assertNotIn("url", run["telemetry"][0])

    def test_missing_telemetry_is_none(self):
        _ads, state, _logs = self.run_search([])
        self.assertIsNone(state["apify_run"]["telemetry"])

    def test_actor_failure_is_recorded_as_an_error(self):
        ads, state, _logs = self.run_search([], fail=True)
        self.assertEqual(ads, [])
        self.assertEqual((state["apify_run"]["status"], state["apify_run"]["error"]), ("FAILED", True))


class TestExactPageZeroWarning(ApiTestCase):
    def start(self, body, actor_ads, apify_run):
        self.setUpNamespace(locked_cookies=LOCKED_COOKIES, actor_ads=actor_ads)

        def fake_meta_auth_search(search_urls, cookies_list, count, country, ad_status, log):
            log(f"  🆔 Apify run {RUN_ID}")
            state = {"cookies_stored": True, "session_valid": "unknown", "session_expired": False,
                     "checkpoint_required": False, "csrf_tokens_missing": False, "status": "unverified",
                     "apify_run": apify_run}
            return list(actor_ads), state

        self.ns["meta_auth_search"] = fake_meta_auth_search
        job_id = self.api_start(body).get_json()["job_id"]
        return job_id, self.client.get(f"/api/v1/scrapes/{job_id}").get_json(), \
            self.client.get(f"/api/v1/scrapes/{job_id}/result").get_json()

    def page_body(self):
        return {"page_ids": [PAGE_ID], "country": "US", "ad_status": "active", "per_page": 3}

    def test_zero_without_telemetry_is_flagged_unverified(self):
        run = {"run_id": RUN_ID, "status": "SUCCEEDED", "ads": 0, "error": False, "telemetry": None}
        _job, status, result = self.start(self.page_body(), [], run)
        self.assertEqual((status["status"], status["count"]), ("completed", 0))
        self.assertEqual(status["warning"]["code"], "exact_page_zero_unverified")
        self.assertEqual(result["warning"]["code"], "exact_page_zero_unverified")
        self.assertEqual(status["apify_runs"], [run])
        self.assertIn(f"Apify run {RUN_ID}", "\n".join(status["log"]))
        self.assertIn("exact_page_zero_unverified", "\n".join(status["log"]))

    def test_zero_with_verified_telemetry_is_a_confirmed_zero(self):
        run = zero_run()
        _job, status, _result = self.start(self.page_body(), [], run)
        self.assertEqual(status["warning"]["code"], "exact_page_zero_ads")

    def test_zero_after_an_actor_error_is_unverified(self):
        telemetry = [{"auth_status": "verified", "graphql_responses": 2}]
        run = {"run_id": RUN_ID, "status": "FAILED", "ads": 0, "error": True, "telemetry": telemetry}
        _job, status, _result = self.start(self.page_body(), [], run)
        self.assertEqual(status["warning"]["code"], "exact_page_zero_unverified")

    def test_ads_or_keyword_searches_are_never_flagged(self):
        run = {"run_id": RUN_ID, "status": "SUCCEEDED", "ads": 1, "error": False, "telemetry": None}
        _job, status, result = self.start(self.page_body(), [make_ad("1001", page_id=PAGE_ID)], run)
        self.assertIsNone(status["warning"])
        self.assertEqual(result["count"], 1)
        _job, status, _result = self.start({"keywords": ["push button"], "country": "US"}, [], run)
        self.assertIsNone(status["warning"])
        self.assertEqual(status["status"], "completed")


class TestTelemetryIsTypeBounded(TestMetaAuthSearchRecordsTheRun):
    """Review fix 1/4/5/6: garbage telemetry never raises and never reaches the API."""

    def test_garbage_values_become_none(self):
        garbage = {"auth_status": "x" * 40, "checkpoint_detected": "yes", "login_wall_detected": 1,
                   "graphql_responses": "3", "html_ads": [1], "graphql_ads": {"a": 1}, "unique_ads": True}
        _ads, state, _logs = self.run_search([], telemetry=[garbage])
        entry = state["apify_run"]["telemetry"][0]
        self.assertEqual(entry, {k: None for k in garbage})

    def test_valid_values_and_short_status_survive(self):
        _ads, state, _logs = self.run_search([], telemetry=[ZERO_TELEMETRY])
        self.assertEqual(state["apify_run"]["telemetry"], [ZERO_TELEMETRY])

    def test_more_than_ten_entries_are_cut_and_marked_truncated(self):
        _ads, state, _logs = self.run_search([], telemetry=[dict(ZERO_TELEMETRY) for _ in range(12)])
        run = state["apify_run"]
        self.assertEqual((len(run["telemetry"]), run["telemetry_truncated"]), (10, True))

    def test_summary_never_raises_even_on_a_broken_run_meta(self):
        ns = load_auth_namespace(api_get=lambda p: None, api_post=lambda p, b: None)
        for bad in (None, "str", {"telemetry": object()}, {"status": ["x"], "telemetry": [object(), 3]}):
            run = ns["_apify_run_summary"](RUN_ID, bad, 4)
            self.assertEqual(run["ads"], 4)
            self.assertIsNone(run["telemetry"]) if not isinstance(bad, dict) else None
        self.assertIsNone(ns["_apify_run_summary"]("é1", {}, 0)["run_id"])  # non-ASCII id rejected
        self.assertIsNone(ns["_apify_run_summary"]("a b", {}, 0)["run_id"])
        self.assertEqual(ns["_apify_run_summary"]("abc123", {}, 0)["run_id"], "abc123")


class TestConfirmedZeroNeedsFullEvidence(TestExactPageZeroWarning):
    """Review fixes 1-3: a confirmed zero needs consistent, complete, untruncated telemetry."""

    def code(self, run, body=None):
        _job, status, _result = self.start(body or self.page_body(), [], run)
        self.assertEqual(status["status"], "completed")
        return status["warning"]["code"]

    def test_complete_consistent_telemetry_is_confirmed(self):
        self.assertEqual(self.code(zero_run()), "exact_page_zero_ads")

    def test_non_numeric_telemetry_cannot_fail_the_job(self):
        # Unsanitised values handed straight to the warning (as if the summary had a bug).
        for bad in ("3", [3], {"n": 3}, True):
            run = zero_run(graphql_responses=bad)
            self.assertEqual(self.code(run), "exact_page_zero_unverified", bad)

    def test_a_crashing_warning_check_never_fails_the_job(self):
        self.setUpNamespace(locked_cookies=LOCKED_COOKIES, actor_ads=[make_ad("1001", page_id=PAGE_ID)])
        def boom(*a, **k):
            raise TypeError("boom")
        self.ns["exact_page_zero_warning"] = boom
        job_id = self.api_start(self.page_body()).get_json()["job_id"]
        status = self.client.get(f"/api/v1/scrapes/{job_id}").get_json()
        self.assertEqual((status["status"], status["count"]), ("completed", 1))
        self.assertIsNone(status["warning"])
        self.assertIn("exact-page warning check failed", "\n".join(status["log"]))

    def test_contradictory_telemetry_is_unverified(self):
        for field in ("html_ads", "graphql_ads", "unique_ads"):
            self.assertEqual(self.code(zero_run(**{field: 2})), "exact_page_zero_unverified", field)

    def test_missing_ad_counters_are_unverified(self):
        for field in ("html_ads", "graphql_ads", "unique_ads", "graphql_responses"):
            run = zero_run()
            for e in run["telemetry"]:
                del e[field]
            self.assertEqual(self.code(run), "exact_page_zero_unverified", field)

    def test_wrong_session_or_no_graphql_traffic_is_unverified(self):
        self.assertEqual(self.code(zero_run(auth_status="unverified")), "exact_page_zero_unverified")
        self.assertEqual(self.code(zero_run(graphql_responses=0)), "exact_page_zero_unverified")

    def test_telemetry_must_cover_every_page_url_sent(self):
        two_pages = {"page_ids": [PAGE_ID, "111111111"], "country": "US", "ad_status": "active"}
        self.assertEqual(self.code(zero_run(entries=1), two_pages), "exact_page_zero_unverified")
        self.assertEqual(self.code(zero_run(entries=2), two_pages), "exact_page_zero_ads")
        self.assertEqual(self.code(zero_run(entries=3), two_pages), "exact_page_zero_unverified")

    def test_truncated_telemetry_is_unverified(self):
        run = zero_run(entries=1)
        run["telemetry_truncated"] = True
        self.assertEqual(self.code(run), "exact_page_zero_unverified")


class TestApifyRunsOnTheJob(ApiTestCase):
    def test_runs_are_capped_at_ten(self):
        self.setUpNamespace(locked_cookies=LOCKED_COOKIES)
        n = []

        def fake_meta_auth_search(search_urls, cookies_list, count, country, ad_status, log):
            n.append(1)
            return [], {"cookies_stored": True, "session_valid": "unknown", "session_expired": False,
                        "checkpoint_required": False, "csrf_tokens_missing": False, "status": "unverified",
                        "apify_run": {"run_id": f"r{len(n)}", "status": "SUCCEEDED", "ads": 0,
                                      "error": False, "telemetry": None}}
        self.ns["meta_auth_search"] = fake_meta_auth_search
        self.ns["jobs"]["j"] = {"status": "running", "log": []}
        self.ns["run_job"]("j", "Brand", "US", [[f"k{i}"] for i in range(12)], [], [], "active",
                           cookies=LOCKED_COOKIES, per_page=0)
        job = self.ns["jobs"]["j"]
        self.assertEqual(len(n), 12)
        self.assertEqual((job["status"], len(job["apify_runs"])), ("done", 10))

    def test_cookieless_path_records_a_run_without_credentials(self):
        self.setUpNamespace(locked_cookies=None, actor_ads=[make_ad("1001", page_id=PAGE_ID)])
        job_id = self.api_start({"page_ids": [PAGE_ID]}).get_json()["job_id"]
        status = self.client.get(f"/api/v1/scrapes/{job_id}").get_json()
        (run,) = status["apify_runs"]
        self.assertEqual((run["run_id"], run["status"], run["ads"], run["telemetry"]), (None, "SUCCEEDED", 1, None))  # fake id "run-1" has a hyphen: not recorded
        self.assertEqual(forbidden_keys(status["apify_runs"]), [])
        self.assertIsNone(status["warning"])

    def test_http_responses_carry_no_credential_material(self):
        from test_scrape_api import SECRET_COOKIE_VALUE
        self.setUpNamespace(locked_cookies=LOCKED_COOKIES, actor_ads=[])
        def fake_meta_auth_search(search_urls, cookies_list, count, country, ad_status, log):
            log(f"  🆔 Apify run {RUN_ID}")
            state = {"cookies_stored": True, "session_valid": "unknown", "session_expired": False,
                     "checkpoint_required": False, "csrf_tokens_missing": False, "status": "unverified",
                     "apify_run": self.ns["_apify_run_summary"](
                         RUN_ID, {"status": "SUCCEEDED", "telemetry": [dict(ZERO_TELEMETRY, cookies=[{"value": SECRET_COOKIE_VALUE}],
                                                                           url="https://x/?token=abc")]}, 0)}
            return [], state
        self.ns["meta_auth_search"] = fake_meta_auth_search
        job_id = self.api_start({"page_ids": [PAGE_ID]}).get_json()["job_id"]
        for path in (f"/api/v1/scrapes/{job_id}", f"/api/v1/scrapes/{job_id}/result"):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(forbidden_keys(resp.get_json()), [], path)
            text = resp.get_data(as_text=True)
            self.assertNotIn(SECRET_COOKIE_VALUE, text)
            self.assertNotIn("token=abc", text)
        self.assertEqual(self.client.get(f"/api/v1/scrapes/{job_id}").get_json()["warning"]["code"],
                         "exact_page_zero_ads")


if __name__ == "__main__":
    unittest.main()
