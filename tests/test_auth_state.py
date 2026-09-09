"""Sanitized regression tests for the authenticated-session state fix in app.py.

Why these tests load source text instead of `import app`
----------------------------------------------------------
app.py contains an unrelated, pre-existing f-string (nested quotes inside an
f-string expression, a PEP 701 construct) that only Python 3.12+ can parse.
This environment's available interpreter is 3.11, so `import app` fails at
module load with a SyntaxError on a line far from anything touched by this
fix -- that failure is reproducible on `main` before this branch's changes
too, so it is not something introduced or fixed here.

To still test the *real* shipped source (not a hand-copied duplicate), each
function under test is extracted directly from app.py's source text by name
and exec'd into an isolated namespace, with only its direct dependencies
(api_get/api_post/AUTH_ACTOR) replaced by sanitized fakes. No network call,
real Facebook cookie, or live scrape is ever made by these tests.
"""
from __future__ import annotations

import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch

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
        # A column-0 line that is just a dangling closer (e.g. the closing
        # "}" of a module-level dict literal) belongs to the block we're
        # extracting, not the next one.
        if line[0] not in (" ", "\t") and not line.lstrip().startswith((")", "]", "}")):
            end = j
            break
    return "\n".join(lines[start:end])


def load_auth_namespace(api_get=None, api_post=None, auth_actor="test-actor-id"):
    """Build a namespace containing the real classify_auth_state,
    combine_auth_states, wait_for_run and meta_auth_search functions from
    app.py, with their Apify HTTP dependencies replaced by fakes."""
    import time as time_module

    ns = {
        "time": time_module,
        "api_get": api_get,
        "api_post": api_post,
        "AUTH_ACTOR": auth_actor,
    }
    # exec is used deliberately here: it runs the exact function bodies as
    # shipped in app.py (see module docstring for why we can't `import app`
    # directly in this environment), not a hand-copied reimplementation.
    exec(extract_block(r"^AUTH_FAILURE_SIGNATURES = \{"), ns)  # noqa: S102
    exec(extract_block(r"^AUTH_STATUS_PRECEDENCE = \("), ns)  # noqa: S102
    exec(extract_block(r"^def _normalize_telemetry_entries\("), ns)  # noqa: S102
    exec(extract_block(r"^def _classify_telemetry_entry\("), ns)  # noqa: S102
    exec(extract_block(r"^def classify_auth_state\("), ns)  # noqa: S102
    exec(extract_block(r"^def combine_auth_states\("), ns)  # noqa: S102
    exec(extract_block(r"^def wait_for_run\("), ns)  # noqa: S102
    exec(extract_block(r"^def meta_auth_search\("), ns)  # noqa: S102
    return ns


def load_config_namespace(env):
    """Exec the real AUTH_ACTOR / AUTH_ACTOR_COUNT module-level config lines
    from app.py with a controlled os.environ (restored afterwards), returning
    a namespace with the resulting AUTH_ACTOR / AUTH_ACTOR_COUNT values plus
    the real parse_auth_actor_count function."""
    ns = {"os": os}
    exec(extract_block(r"^def parse_auth_actor_count\("), ns)  # noqa: S102
    with patch.dict(os.environ, env, clear=True):
        exec(extract_block(r"^AUTH_ACTOR\s*="), ns)  # noqa: S102
        exec(extract_block(r"^AUTH_ACTOR_COUNT = "), ns)  # noqa: S102
    return ns


class TestClassifyAuthState(unittest.TestCase):
    """Each of the five states Phase 2 requires, reported separately."""

    def setUp(self):
        self.ns = load_auth_namespace()
        self.classify = self.ns["classify_auth_state"]

    def test_no_cookies_is_not_a_session(self):
        state = self.classify(False, None, None, [])
        self.assertEqual(state, {
            "cookies_stored": False,
            "session_valid": False,
            "session_expired": False,
            "checkpoint_required": False,
            "csrf_tokens_missing": False,
            "status": "no_cookies_provided",
        })

    def test_cookies_stored_but_run_succeeded_with_no_ads_is_unknown(self):
        state = self.classify(True, "SUCCEEDED", None, [])
        self.assertTrue(state["cookies_stored"])
        self.assertEqual(state["session_valid"], "unknown")
        self.assertFalse(state["session_expired"])
        self.assertFalse(state["checkpoint_required"])
        self.assertFalse(state["csrf_tokens_missing"])

    def test_clean_run_with_ads_is_session_valid(self):
        ads = [{"ad_archive_id": "1"}, {"ad_archive_id": "2"}]
        state = self.classify(True, "SUCCEEDED", None, ads)
        self.assertEqual(state["session_valid"], True)
        self.assertFalse(state["session_expired"])
        self.assertFalse(state["checkpoint_required"])
        self.assertFalse(state["csrf_tokens_missing"])

    def test_login_redirect_message_is_session_expired(self):
        state = self.classify(True, "FAILED", "Redirected to login page", [])
        self.assertTrue(state["session_expired"])
        self.assertEqual(state["session_valid"], False)
        self.assertFalse(state["checkpoint_required"])
        self.assertFalse(state["csrf_tokens_missing"])

    def test_not_logged_in_message_is_session_expired(self):
        state = self.classify(True, "SUCCEEDED", "user is not logged in", [])
        self.assertTrue(state["session_expired"])
        self.assertEqual(state["session_valid"], False)

    def test_checkpoint_message_is_checkpoint_required(self):
        state = self.classify(True, "FAILED", "Facebook checkpoint detected", [])
        self.assertTrue(state["checkpoint_required"])
        self.assertEqual(state["session_valid"], False)
        self.assertFalse(state["session_expired"])

    def test_missing_fb_dtsg_message_is_csrf_tokens_missing(self):
        state = self.classify(True, "FAILED", "could not locate fb_dtsg on page", [])
        self.assertTrue(state["csrf_tokens_missing"])
        self.assertEqual(state["session_valid"], False)

    def test_missing_lsd_message_is_csrf_tokens_missing(self):
        state = self.classify(True, "FAILED", "lsd token not found in DOM", [])
        self.assertTrue(state["csrf_tokens_missing"])
        self.assertEqual(state["session_valid"], False)

    def test_generic_graphql_auth_failure_with_no_recognizable_signature(self):
        # A run that failed for a reason this layer can't further classify
        # (e.g. a raw GraphQL error the actor didn't map to a known cause)
        # must still be reported as an invalid session, not "unknown".
        state = self.classify(True, "FAILED", "GraphQL error: unauthorized (code 190)", [])
        self.assertEqual(state["session_valid"], False)
        self.assertFalse(state["session_expired"])
        self.assertFalse(state["checkpoint_required"])
        self.assertFalse(state["csrf_tokens_missing"])

    def test_cookies_stored_is_independent_of_validity(self):
        # cookies_stored must be True even when the session behind them is
        # completely dead -- these are reported separately, never conflated.
        state = self.classify(True, "FAILED", "checkpoint required", [])
        self.assertTrue(state["cookies_stored"])
        self.assertTrue(state["checkpoint_required"])


class TestCombineAuthStates(unittest.TestCase):
    def setUp(self):
        self.ns = load_auth_namespace()
        self.classify = self.ns["classify_auth_state"]
        self.combine = self.ns["combine_auth_states"]

    def test_empty_list_has_no_cookies(self):
        combined = self.combine([])
        self.assertFalse(combined["cookies_stored"])
        self.assertEqual(combined["session_valid"], False)

    def test_any_valid_session_wins(self):
        states = [
            self.classify(True, "FAILED", "checkpoint", []),
            self.classify(True, "SUCCEEDED", None, [{"ad_archive_id": "1"}]),
        ]
        combined = self.combine(states)
        self.assertEqual(combined["session_valid"], True)
        # the checkpoint signal from the other search is still surfaced
        self.assertTrue(combined["checkpoint_required"])

    def test_unknown_beats_false_when_nothing_confirmed_valid(self):
        states = [
            self.classify(True, "SUCCEEDED", None, []),   # unknown
            self.classify(False, None, None, []),          # no cookies -> False
        ]
        combined = self.combine(states)
        self.assertEqual(combined["session_valid"], "unknown")

    def test_all_false_stays_false(self):
        states = [
            self.classify(True, "FAILED", "not logged in", []),
            self.classify(True, "FAILED", "not logged in", []),
        ]
        combined = self.combine(states)
        self.assertEqual(combined["session_valid"], False)
        self.assertTrue(combined["session_expired"])


class TestWaitForRunStatusHandling(unittest.TestCase):
    """wait_for_run must surface the actor run's real status/message instead
    of silently treating any terminal state as success."""

    def test_succeeded_run_returns_dataset_items_and_status(self):
        def fake_api_get(path):
            if path.startswith("actor-runs/"):
                return {"data": {"status": "SUCCEEDED", "statusMessage": None,
                                  "defaultDatasetId": "ds1"}}
            if path.startswith("datasets/"):
                return [{"ad_archive_id": "1"}]
            raise AssertionError(f"unexpected path {path}")

        ns = load_auth_namespace(api_get=fake_api_get)
        ads, run_meta = ns["wait_for_run"]("run1", log=lambda m: None)
        self.assertEqual(ads, [{"ad_archive_id": "1"}])
        self.assertEqual(run_meta["status"], "SUCCEEDED")

    def test_failed_run_with_no_dataset_does_not_crash(self):
        # A run that fails before ever creating a dataset (e.g. crashed
        # immediately on a login redirect) has no defaultDatasetId at all.
        # The old code did r["data"]["defaultDatasetId"] unconditionally and
        # would KeyError here.
        def fake_api_get(path):
            assert path.startswith("actor-runs/")
            return {"data": {"status": "FAILED", "statusMessage": "Redirected to login",
                              "defaultDatasetId": None}}

        ns = load_auth_namespace(api_get=fake_api_get)
        ads, run_meta = ns["wait_for_run"]("run2", log=lambda m: None)
        self.assertEqual(ads, [])
        self.assertEqual(run_meta["status"], "FAILED")
        self.assertEqual(run_meta["status_message"], "Redirected to login")


class TestMetaAuthSearchNoLongerClaimsAuthFromCookiesAlone(unittest.TestCase):
    SECRET_COOKIE_VALUE = "c_user=999999999; xs=TOTALLY-SECRET-SESSION-VALUE-DO-NOT-LOG"

    def _run(self, run_status, status_message, ads_from_actor, actor_gated_types=None):
        calls = {"post_payloads": []}

        def fake_api_post(path, payload):
            calls["post_payloads"].append(payload)
            return {"data": {"id": "run-xyz"}}

        def fake_api_get(path):
            if path.startswith("actor-runs/"):
                return {"data": {"status": run_status, "statusMessage": status_message,
                                  "defaultDatasetId": "ds1" if ads_from_actor else None}}
            if path.startswith("datasets/"):
                return list(ads_from_actor)
            raise AssertionError(f"unexpected path {path}")

        ns = load_auth_namespace(api_get=fake_api_get, api_post=fake_api_post)
        logs = []
        ads, auth_state = ns["meta_auth_search"](
            search_urls=[{"url": "https://www.facebook.com/ads/library/?id=1"}],
            cookies_list=[{"name": "xs", "value": self.SECRET_COOKIE_VALUE}],
            count=20, country="US", ad_status="active", log=logs.append,
        )
        return ads, auth_state, logs, calls

    def test_expired_session_is_reported_honestly_not_as_authenticated(self):
        _ads, auth_state, logs, _calls = self._run("FAILED", "session expired, redirected to login", [])
        self.assertEqual(auth_state["session_valid"], False)
        self.assertTrue(auth_state["session_expired"])
        joined = "\n".join(logs)
        self.assertNotIn("Authenticated —", joined)
        self.assertNotIn("Authenticated mode", joined)  # the old, removed false claim

    def test_checkpoint_is_reported_honestly(self):
        _ads, auth_state, _logs, _calls = self._run("FAILED", "Facebook checkpoint required", [])
        self.assertTrue(auth_state["checkpoint_required"])
        self.assertEqual(auth_state["session_valid"], False)

    def test_successful_run_with_ads_is_reported_as_authenticated(self):
        _ads, auth_state, logs, _calls = self._run(
            "SUCCEEDED", None, [{"ad_archive_id": "1", "gated_type": "LOGGED_OUT"}]
        )
        self.assertEqual(auth_state["session_valid"], True)
        self.assertIn("Authenticated —", "\n".join(logs))

    def test_gated_type_from_actor_is_never_overwritten(self):
        # This is the exact regression this fix targets: the app used to
        # force every returned ad's gated_type to "ELIGIBLE" purely because
        # cookies were present, discarding the actor's real per-ad signal.
        ads, _, _, _ = self._run(
            "SUCCEEDED", None, [{"ad_archive_id": "1", "gated_type": "LOGGED_OUT"}]
        )
        self.assertEqual(ads[0]["gated_type"], "LOGGED_OUT")

    def test_cookies_are_never_logged(self):
        _, _, logs, calls = self._run("SUCCEEDED", None, [{"ad_archive_id": "1"}])
        joined = "\n".join(logs)
        self.assertNotIn(self.SECRET_COOKIE_VALUE, joined)
        self.assertNotIn("xs=", joined)
        # sanity: confirm the secret really was on the wire to the fake actor
        # call (i.e. this test would catch a real leak, not just an absent one)
        self.assertTrue(any(
            self.SECRET_COOKIE_VALUE in str(p.get("cookies"))
            for p in calls["post_payloads"]
        ))

    def test_no_auth_actor_configured_reports_cookies_stored_without_claiming_valid(self):
        ns = load_auth_namespace(api_get=None, api_post=None, auth_actor="")
        logs = []
        ads, auth_state = ns["meta_auth_search"](
            search_urls=[{"url": "https://www.facebook.com/ads/library/?id=1"}],
            cookies_list=[{"name": "xs", "value": "irrelevant"}],
            count=20, country="US", ad_status="active", log=logs.append,
        )
        self.assertEqual(ads, [])
        self.assertTrue(auth_state["cookies_stored"])
        self.assertNotEqual(auth_state["session_valid"], True)


PRODUCTION_AUTH_ACTOR_ID = "big_wave~meta-ads-auth-scraper"


class TestAuthActorIdConfigurable(unittest.TestCase):
    """Requirement: the authenticated actor is configurable through
    AUTH_ACTOR_ID, which must override the code-level fallback when set,
    falling back to the production default actor
    ("big_wave~meta-ads-auth-scraper") when the env var is absent."""

    def test_auth_actor_id_env_var_overrides_default_when_present(self):
        ns = load_config_namespace({"AUTH_ACTOR_ID": "IUTRYPRMFxeXlfjII"})
        self.assertEqual(ns["AUTH_ACTOR"], "IUTRYPRMFxeXlfjII")

    def test_auth_actor_id_falls_back_to_production_default_when_absent(self):
        ns = load_config_namespace({})
        self.assertEqual(ns["AUTH_ACTOR"], PRODUCTION_AUTH_ACTOR_ID)


class TestAuthActorCountConfig(unittest.TestCase):
    """Requirement: AUTH_ACTOR_COUNT is configurable, defaults to 100, is
    restricted to the validated 1-100 range, and malformed/out-of-range
    values safely fall back to 100."""

    def setUp(self):
        self.parse = load_config_namespace({})["parse_auth_actor_count"]

    def test_default_is_100_when_unset(self):
        self.assertEqual(self.parse(None), 100)

    def test_valid_value_is_used(self):
        self.assertEqual(self.parse("55"), 55)

    def test_boundary_values_are_accepted(self):
        self.assertEqual(self.parse("1"), 1)
        self.assertEqual(self.parse("100"), 100)

    def test_zero_falls_back_to_100(self):
        self.assertEqual(self.parse("0"), 100)

    def test_above_max_falls_back_to_100(self):
        self.assertEqual(self.parse("101"), 100)

    def test_negative_falls_back_to_100(self):
        self.assertEqual(self.parse("-5"), 100)

    def test_malformed_non_numeric_falls_back_to_100(self):
        self.assertEqual(self.parse("not-a-number"), 100)

    def test_malformed_float_string_falls_back_to_100(self):
        self.assertEqual(self.parse("12.5"), 100)

    def test_empty_string_falls_back_to_100(self):
        self.assertEqual(self.parse(""), 100)

    def test_env_var_integration_valid(self):
        ns = load_config_namespace({"AUTH_ACTOR_COUNT": "42"})
        self.assertEqual(ns["AUTH_ACTOR_COUNT"], 42)

    def test_env_var_integration_malformed_falls_back_to_100(self):
        ns = load_config_namespace({"AUTH_ACTOR_COUNT": "garbage"})
        self.assertEqual(ns["AUTH_ACTOR_COUNT"], 100)

    def test_env_var_integration_absent_defaults_to_100(self):
        ns = load_config_namespace({})
        self.assertEqual(ns["AUTH_ACTOR_COUNT"], 100)


class TestClassifyAuthStateWithTelemetry(unittest.TestCase):
    """Contract tests using fixtures that exactly match the real actor's
    AUTH_TELEMETRY output.

    Confirmed from meta-ads-auth-scraper-dev/src/main.js + src/lib.js:
    `Actor.setValue('AUTH_TELEMETRY', authTelemetry)` where `authTelemetry`
    is always a JS array built by `authTelemetry.push({ url, country,
    ...authState })` once per searched URL, and `authState` (from
    `classifyAuthentication()` in src/lib.js) is one of:

        {cookies_provided: false, auth_status: "no_cookies_provided",
         checkpoint_detected, login_wall_detected}
        {cookies_provided: true, auth_status: "checkpoint_required",
         checkpoint_detected: true, login_wall_detected}
        {cookies_provided: true, auth_status: "session_expired",
         checkpoint_detected: false, login_wall_detected: true}
        {cookies_provided: true, auth_status: "unverified",
         checkpoint_detected: false, login_wall_detected: false}
        {cookies_provided: true, auth_status: "verified",
         checkpoint_detected: false, login_wall_detected: false}

    There is no `verified: true` field anywhere in this contract -- only
    `auth_status: "verified"`. Ads being present is never sufficient on its
    own to claim "verified" once telemetry exists."""

    def setUp(self):
        self.ns = load_auth_namespace()
        self.classify = self.ns["classify_auth_state"]

    @staticmethod
    def entry(auth_status, cookies_provided=True, checkpoint_detected=False,
              login_wall_detected=False, url="https://www.facebook.com/ads/library/?id=1",
              country="US"):
        return {
            "url": url,
            "country": country,
            "cookies_provided": cookies_provided,
            "auth_status": auth_status,
            "checkpoint_detected": checkpoint_detected,
            "login_wall_detected": login_wall_detected,
        }

    # -- single verified telemetry object --------------------------------
    def test_single_verified_telemetry_object(self):
        # A bare object (not wrapped in a list) must also be accepted.
        state = self.classify(True, "SUCCEEDED", None, [{"ad_archive_id": "1"}],
                               telemetry=self.entry("verified"))
        self.assertEqual(state["status"], "verified")
        self.assertEqual(state["session_valid"], True)

    # -- verified telemetry array ------------------------------------------
    def test_verified_telemetry_array(self):
        state = self.classify(True, "SUCCEEDED", None, [{"ad_archive_id": "1"}],
                               telemetry=[self.entry("verified")])
        self.assertEqual(state["status"], "verified")
        self.assertEqual(state["session_valid"], True)

    def test_ads_present_is_not_verified_without_telemetry_confirmation(self):
        # Requirement 8-adjacent regression guard: telemetry present but not
        # confirming "verified" must not be overridden by ads being present.
        state = self.classify(True, "SUCCEEDED", None, [{"ad_archive_id": "1"}],
                               telemetry=[self.entry("unverified")])
        self.assertNotEqual(state["status"], "verified")
        self.assertNotEqual(state["session_valid"], True)

    # -- checkpoint_detected -------------------------------------------------
    def test_checkpoint_detected_wins_even_with_ads_returned(self):
        state = self.classify(True, "SUCCEEDED", None, [{"ad_archive_id": "1"}],
                               telemetry=[self.entry("checkpoint_required", checkpoint_detected=True)])
        self.assertEqual(state["status"], "checkpoint_required")
        self.assertTrue(state["checkpoint_required"])
        self.assertEqual(state["session_valid"], False)

    def test_checkpoint_detected_boolean_wins_even_if_auth_status_disagrees(self):
        # Requirement 6: never trust a "verified" claim when checkpoint_detected
        # is true -- the boolean signal outranks a contradicting auth_status.
        state = self.classify(True, "SUCCEEDED", None, [],
                               telemetry=[self.entry("verified", checkpoint_detected=True)])
        self.assertEqual(state["status"], "checkpoint_required")
        self.assertEqual(state["session_valid"], False)

    # -- login_wall_detected --------------------------------------------------
    def test_login_wall_detected_maps_to_session_expired(self):
        state = self.classify(True, "SUCCEEDED", None, [],
                               telemetry=[self.entry("session_expired", login_wall_detected=True)])
        self.assertEqual(state["status"], "session_expired")
        self.assertTrue(state["session_expired"])
        self.assertEqual(state["session_valid"], False)

    def test_checkpoint_beats_login_wall_precedence(self):
        state = self.classify(True, "SUCCEEDED", None, [],
                               telemetry=[self.entry("checkpoint_required",
                                                      checkpoint_detected=True, login_wall_detected=True)])
        self.assertEqual(state["status"], "checkpoint_required")

    # -- no cookies -------------------------------------------------------
    def test_no_cookies_provided_telemetry(self):
        state = self.classify(True, "SUCCEEDED", None, [],
                               telemetry=[self.entry("no_cookies_provided", cookies_provided=False)])
        self.assertEqual(state["status"], "no_cookies_provided")
        self.assertEqual(state["session_valid"], False)

    # -- unverified ---------------------------------------------------------
    def test_unverified_telemetry(self):
        state = self.classify(True, "SUCCEEDED", None, [],
                               telemetry=[self.entry("unverified")])
        self.assertEqual(state["status"], "unverified")
        self.assertNotEqual(state["session_valid"], True)

    # -- malformed telemetry --------------------------------------------------
    def test_malformed_telemetry_object_with_no_recognizable_fields(self):
        state = self.classify(True, "SUCCEEDED", None, [{"ad_archive_id": "1"}],
                               telemetry={"unexpected_field": 123})
        self.assertEqual(state["status"], "unverified")
        self.assertNotEqual(state["session_valid"], True)

    def test_malformed_telemetry_wrong_type_never_falls_back_to_ads_heuristic(self):
        # Requirement 8: use the legacy ads-returned heuristic only when
        # AUTH_TELEMETRY is genuinely unavailable (None), never when it is
        # present but malformed (here: neither an object nor an array).
        state = self.classify(True, "SUCCEEDED", None, [{"ad_archive_id": "1"}],
                               telemetry="not-a-valid-shape")
        self.assertEqual(state["status"], "unverified")
        self.assertNotEqual(state["session_valid"], True)

    def test_malformed_telemetry_array_of_non_dict_entries(self):
        state = self.classify(True, "SUCCEEDED", None, [{"ad_archive_id": "1"}],
                               telemetry=[42, "oops"])
        self.assertEqual(state["status"], "unverified")
        self.assertNotEqual(state["session_valid"], True)

    def test_empty_telemetry_array_is_unverified_not_verified(self):
        state = self.classify(True, "SUCCEEDED", None, [{"ad_archive_id": "1"}], telemetry=[])
        self.assertEqual(state["status"], "unverified")
        self.assertNotEqual(state["session_valid"], True)

    # -- contradictory multi-URL telemetry -----------------------------------
    def test_contradictory_multi_url_telemetry_most_concerning_wins(self):
        # One URL came back verified, another hit a checkpoint in the same
        # run -- the conservative combined status is checkpoint_required,
        # matching the precedence used for combine_auth_states across the
        # whole job (requirement 5).
        telemetry = [
            self.entry("verified", url="https://www.facebook.com/ads/library/?id=1"),
            self.entry("checkpoint_required", checkpoint_detected=True,
                       url="https://www.facebook.com/ads/library/?id=2"),
        ]
        state = self.classify(True, "SUCCEEDED", None, [{"ad_archive_id": "1"}], telemetry=telemetry)
        self.assertEqual(state["status"], "checkpoint_required")
        self.assertEqual(state["session_valid"], False)

    def test_contradictory_multi_url_telemetry_login_wall_beats_verified(self):
        telemetry = [
            self.entry("session_expired", login_wall_detected=True,
                       url="https://www.facebook.com/ads/library/?id=1"),
            self.entry("verified", url="https://www.facebook.com/ads/library/?id=2"),
        ]
        state = self.classify(True, "SUCCEEDED", None, [], telemetry=telemetry)
        self.assertEqual(state["status"], "session_expired")

    def test_all_verified_multi_url_telemetry_is_verified(self):
        telemetry = [
            self.entry("verified", url="https://www.facebook.com/ads/library/?id=1"),
            self.entry("verified", url="https://www.facebook.com/ads/library/?id=2"),
        ]
        state = self.classify(True, "SUCCEEDED", None, [{"ad_archive_id": "1"}], telemetry=telemetry)
        self.assertEqual(state["status"], "verified")
        self.assertEqual(state["session_valid"], True)

    # -- missing telemetry legacy fallback -----------------------------------
    def test_missing_telemetry_falls_back_to_legacy_ads_heuristic(self):
        state = self.classify(True, "SUCCEEDED", None, [{"ad_archive_id": "1"}], telemetry=None)
        self.assertEqual(state["status"], "verified")
        self.assertEqual(state["session_valid"], True)

    def test_missing_telemetry_with_no_ads_stays_unverified(self):
        state = self.classify(True, "SUCCEEDED", None, [], telemetry=None)
        self.assertEqual(state["status"], "unverified")

    def test_no_cookies_status_is_no_cookies_provided(self):
        state = self.classify(False, None, None, [])
        self.assertEqual(state["status"], "no_cookies_provided")


class TestWaitForRunTelemetry(unittest.TestCase):
    """Requirement: wait_for_run retrieves AUTH_TELEMETRY from the run's
    default key-value store, and a retrieval failure must not fail an
    otherwise successful scrape."""

    def test_telemetry_retrieved_from_default_kv_store(self):
        # The real actor always emits AUTH_TELEMETRY as an array of per-URL
        # entries (see meta-ads-auth-scraper-dev/src/main.js).
        real_shape_telemetry = [{
            "url": "https://www.facebook.com/ads/library/?id=1",
            "country": "US",
            "cookies_provided": True,
            "auth_status": "verified",
            "checkpoint_detected": False,
            "login_wall_detected": False,
        }]

        def fake_api_get(path):
            if path.startswith("actor-runs/"):
                return {"data": {"status": "SUCCEEDED", "statusMessage": None,
                                  "defaultDatasetId": "ds1",
                                  "defaultKeyValueStoreId": "kv1"}}
            if path.startswith("datasets/"):
                return [{"ad_archive_id": "1"}]
            if path == "key-value-stores/kv1/records/AUTH_TELEMETRY":
                return real_shape_telemetry
            raise AssertionError(f"unexpected path {path}")

        ns = load_auth_namespace(api_get=fake_api_get)
        ads, run_meta = ns["wait_for_run"]("run1", log=lambda m: None)
        self.assertEqual(ads, [{"ad_archive_id": "1"}])
        self.assertEqual(run_meta["telemetry"], real_shape_telemetry)

    def test_no_kv_store_id_skips_telemetry_fetch_without_error(self):
        def fake_api_get(path):
            if path.startswith("actor-runs/"):
                return {"data": {"status": "SUCCEEDED", "statusMessage": None,
                                  "defaultDatasetId": "ds1"}}
            if path.startswith("datasets/"):
                return [{"ad_archive_id": "1"}]
            raise AssertionError(f"unexpected path {path}")

        ns = load_auth_namespace(api_get=fake_api_get)
        ads, run_meta = ns["wait_for_run"]("run1", log=lambda m: None)
        self.assertIsNone(run_meta["telemetry"])

    def test_telemetry_fetch_failure_does_not_fail_the_scrape(self):
        def fake_api_get(path):
            if path.startswith("actor-runs/"):
                return {"data": {"status": "SUCCEEDED", "statusMessage": None,
                                  "defaultDatasetId": "ds1",
                                  "defaultKeyValueStoreId": "kv1"}}
            if path.startswith("datasets/"):
                return [{"ad_archive_id": "1"}, {"ad_archive_id": "2"}]
            if path == "key-value-stores/kv1/records/AUTH_TELEMETRY":
                raise Exception("Apify GET key-value-stores/kv1/records/AUTH_TELEMETRY → HTTP 404: not found")
            raise AssertionError(f"unexpected path {path}")

        logs = []
        ns = load_auth_namespace(api_get=fake_api_get)
        ads, run_meta = ns["wait_for_run"]("run1", log=logs.append)
        # The scrape itself (ads) is unaffected by the telemetry failure.
        self.assertEqual(len(ads), 2)
        self.assertIsNone(run_meta["telemetry"])
        # A warning was reported, but sanitized -- no raw exception text,
        # response bodies, tokens, or cookie material echoed into the log.
        joined = "\n".join(logs)
        self.assertIn("telemetry", joined.lower())
        self.assertNotIn("404", joined)
        self.assertNotIn("not found", joined)


class TestMetaAuthSearchTelemetryIntegration(unittest.TestCase):
    """Requirement: actor telemetry is passed into authentication
    classification end-to-end through meta_auth_search."""

    def _run(self, run_status, status_message, ads_from_actor, telemetry=None, count=100):
        def fake_api_post(path, payload):
            return {"data": {"id": "run-xyz"}}

        def fake_api_get(path):
            if path.startswith("actor-runs/"):
                data = {"status": run_status, "statusMessage": status_message,
                        "defaultDatasetId": "ds1" if ads_from_actor else None}
                if telemetry is not None:
                    data["defaultKeyValueStoreId"] = "kv1"
                return {"data": data}
            if path.startswith("datasets/"):
                return list(ads_from_actor)
            if path == "key-value-stores/kv1/records/AUTH_TELEMETRY":
                return telemetry
            raise AssertionError(f"unexpected path {path}")

        ns = load_auth_namespace(api_get=fake_api_get, api_post=fake_api_post)
        logs = []
        ads, auth_state = ns["meta_auth_search"](
            search_urls=[{"url": "https://www.facebook.com/ads/library/?id=1"}],
            cookies_list=[{"name": "xs", "value": "irrelevant"}],
            count=count, country="US", ad_status="active", log=logs.append,
        )
        return ads, auth_state, logs

    def test_verified_actor_telemetry_reported_as_authenticated(self):
        ads, auth_state, logs = self._run(
            "SUCCEEDED", None, [{"ad_archive_id": "1"}],
            telemetry=[{
                "url": "https://www.facebook.com/ads/library/?id=1", "country": "US",
                "cookies_provided": True, "auth_status": "verified",
                "checkpoint_detected": False, "login_wall_detected": False,
            }],
        )
        self.assertEqual(auth_state["status"], "verified")
        self.assertIn("Authenticated —", "\n".join(logs))

    def test_checkpoint_telemetry_reported_honestly_despite_ads(self):
        ads, auth_state, logs = self._run(
            "SUCCEEDED", None, [{"ad_archive_id": "1"}],
            telemetry=[{
                "url": "https://www.facebook.com/ads/library/?id=1", "country": "US",
                "cookies_provided": True, "auth_status": "checkpoint_required",
                "checkpoint_detected": True, "login_wall_detected": False,
            }],
        )
        self.assertEqual(auth_state["status"], "checkpoint_required")
        self.assertEqual(auth_state["session_valid"], False)
        self.assertNotIn("Authenticated —", "\n".join(logs))

    def test_expired_login_wall_telemetry_reported_honestly(self):
        _ads, auth_state, logs = self._run(
            "SUCCEEDED", None, [],
            telemetry=[{
                "url": "https://www.facebook.com/ads/library/?id=1", "country": "US",
                "cookies_provided": True, "auth_status": "session_expired",
                "checkpoint_detected": False, "login_wall_detected": True,
            }],
        )
        self.assertEqual(auth_state["status"], "session_expired")
        self.assertNotIn("Authenticated —", "\n".join(logs))

    def test_missing_telemetry_falls_back_to_existing_classifier(self):
        ads, auth_state, _logs = self._run(
            "SUCCEEDED", None, [{"ad_archive_id": "1"}], telemetry=None
        )
        self.assertEqual(auth_state["status"], "verified")
        self.assertEqual(len(ads), 1)


class TestSuccessfulHundredItemDataset(unittest.TestCase):
    """Requirement: the validated actor count (100) flows through end to end
    and a full 100-item dataset is handled without truncation or corruption."""

    def test_100_item_dataset_returned_intact_with_configured_count(self):
        hundred_ads = [{"ad_archive_id": str(i), "gated_type": "LOGGED_OUT"} for i in range(100)]
        calls = {"post_payloads": []}

        def fake_api_post(path, payload):
            calls["post_payloads"].append(payload)
            return {"data": {"id": "run-100"}}

        def fake_api_get(path):
            if path.startswith("actor-runs/"):
                return {"data": {"status": "SUCCEEDED", "statusMessage": None,
                                  "defaultDatasetId": "ds100"}}
            if path.startswith("datasets/"):
                return list(hundred_ads)
            raise AssertionError(f"unexpected path {path}")

        ns = load_auth_namespace(api_get=fake_api_get, api_post=fake_api_post)
        ads, auth_state = ns["meta_auth_search"](
            search_urls=[{"url": "https://www.facebook.com/ads/library/?id=1"}],
            cookies_list=[{"name": "xs", "value": "irrelevant"}],
            count=100, country="US", ad_status="active", log=lambda m: None,
        )
        self.assertEqual(len(ads), 100)
        self.assertEqual(ads, hundred_ads)
        self.assertEqual(auth_state["status"], "verified")
        # Confirm the validated actor count (default AUTH_ACTOR_COUNT) is
        # what actually gets sent to the actor, not the old hardcoded 20.
        self.assertEqual(calls["post_payloads"][0]["count"], 100)


if __name__ == "__main__":
    unittest.main()
