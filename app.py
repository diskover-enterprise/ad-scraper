"""
Meta Ad Intelligence Scraper
Uses curious_coder/facebook-ads-library-scraper (actor: XtaWFhbtfxyzqrFmd)
Deploy on Railway — set APIFY_TOKEN env var.
"""

import json, time, threading, uuid, urllib.request, os, re, io, wave, base64, ipaddress
from urllib.parse import urlparse, quote as urlquote, parse_qs, urlsplit, urljoin
from datetime import datetime, timezone
from flask import Flask, request, jsonify, render_template_string

app  = Flask(__name__)
jobs = {}        # { job_id: {status, log, html, ads, params, auth_state, ...} } -- never cookies
last_job_id = None  # track most recent job for /logs endpoint

# Locked cookies — stored in a file so every gunicorn worker can read them.
# Set via /cookies/lock, reused across scrapes until unlocked. Cleared on redeploy.
COOKIE_STORE = "/tmp/locked_cookies.json"

def get_locked_cookies():
    try:
        with open(COOKIE_STORE) as f:
            data = json.load(f)
            return data if isinstance(data, list) and data else None
    except Exception:
        return None

def set_locked_cookies(cookies):
    with open(COOKIE_STORE, "w") as f:
        json.dump(cookies, f)

def clear_locked_cookies():
    try:
        os.remove(COOKIE_STORE)
    except Exception:
        pass

# ── Config ───────────────────────────────────────────────────────────────────

APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")
META_ACTOR  = "XtaWFhbtfxyzqrFmd"   # curious_coder/facebook-ads-library-scraper (unauthenticated)
AUTH_ACTOR  = os.environ.get("AUTH_ACTOR_ID", "big_wave~meta-ads-auth-scraper")  # your custom meta-ads-auth-scraper actor ID; falls back to the production default when absent


def parse_auth_actor_count(raw, default=100, lo=1, hi=100):
    """Parse AUTH_ACTOR_COUNT from the environment.

    Anything that isn't a plain integer within [lo, hi] falls back to
    `default` rather than being forwarded to the actor -- the validated
    test actor (IUTRYPRMFxeXlfjII) has only been exercised up to 100
    items per run, so out-of-range or malformed values are not trusted.
    """
    if raw is None:
        return default
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if val < lo or val > hi:
        return default
    return val


AUTH_ACTOR_COUNT = parse_auth_actor_count(os.environ.get("AUTH_ACTOR_COUNT"))  # validated range 1-100, default 100
APIFY_BASE  = "https://api.apify.com/v2"

COUNTRIES = [
    ("",   "🌍 All Regions"),
    ("US", "United States"),
    ("GB", "United Kingdom"),
    ("AU", "Australia"),
    ("CA", "Canada"),
    ("DE", "Germany"),
    ("FR", "France"),
    ("SE", "Sweden"),
    ("NO", "Norway"),
    ("DK", "Denmark"),
    ("FI", "Finland"),
    ("IT", "Italy"),
    ("ES", "Spain"),
    ("NL", "Netherlands"),
    ("BE", "Belgium"),
    ("AT", "Austria"),
    ("CH", "Switzerland"),
]

COUNTRY_OPTIONS = "\n".join(
    f'<option value="{code}" {"selected" if code == "US" else ""}>{label}</option>'
    for code, label in COUNTRIES
)


# ── Apify helpers ─────────────────────────────────────────────────────────────

def apify_req(method, path, payload=None):
    sep  = "&" if "?" in path else "?"
    url  = f"{APIFY_BASE}/{path}{sep}token={APIFY_TOKEN}"
    data = json.dumps(payload).encode() if payload else None
    hdrs = {"Content-Type": "application/json"} if data else {}
    req  = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:    body_err = e.read().decode()[:300]
        except Exception: body_err = ""
        raise Exception(f"Apify {method} {path} → HTTP {e.code}: {body_err}")

def api_post(path, payload):
    return apify_req("POST", path, payload)

def api_get(path):
    return apify_req("GET", path)

def wait_for_run(run_id, log, poll=5, timeout=300):
    """Poll an Apify run to completion and fetch its dataset items.

    Returns (ads, run_meta) where run_meta = {"status": ..., "status_message": ...,
    "telemetry": ...} taken straight from the Apify run record (plus, when
    available, the actor's own AUTH_TELEMETRY record from its default
    key-value store). Callers must check run_meta["status"] themselves -- a
    FAILED/ABORTED/TIMED-OUT run can still have a (usually empty or partial)
    dataset, and treating that dataset as a normal successful result is
    exactly how a login redirect or checkpoint inside the actor gets silently
    reported as "it worked, 0 ads found" instead of a real failure.

    Telemetry retrieval is best-effort: a failure to fetch or parse it is
    logged as a sanitized warning and never raised, so it cannot turn an
    otherwise successful scrape into a failed one -- callers fall back to
    the existing status/message classifier when run_meta["telemetry"] is None.
    """
    deadline = time.time() + timeout
    r = {}
    while time.time() < deadline:
        r      = api_get(f"actor-runs/{run_id}")
        status = r["data"]["status"]
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            break
        time.sleep(poll)
    run_data = r.get("data", {}) or {}
    run_meta = {
        "status":         run_data.get("status"),
        "status_message": run_data.get("statusMessage"),
        "telemetry":      None,
    }
    ds_id = run_data.get("defaultDatasetId")
    ads = []
    if ds_id:
        raw = api_get(f"datasets/{ds_id}/items?limit=200")
        # Apify may return items as a raw list OR wrapped in {"data": {"items": [...]}}
        if isinstance(raw, list):
            ads = raw
        elif isinstance(raw, dict):
            if "data" in raw:
                ads = raw["data"].get("items", [])
            else:
                ads = raw.get("items", [])

    kv_id = run_data.get("defaultKeyValueStoreId")
    if kv_id:
        try:
            raw_telemetry = api_get(f"key-value-stores/{kv_id}/records/AUTH_TELEMETRY")
            # The actor emits AUTH_TELEMETRY as a JSON array (one entry per
            # searched URL) but a bare object is also accepted defensively
            # -- classify_auth_state normalizes either shape.
            run_meta["telemetry"] = raw_telemetry if isinstance(raw_telemetry, (dict, list)) else None
        except Exception as e:
            # Sanitized on purpose: never echo the raw exception (it can
            # embed response bodies) -- just the exception type, which is
            # enough to debug from logs without risking a token/cookie leak.
            log(f"  ⚠️ Could not retrieve auth telemetry ({type(e).__name__}) — falling back to status-based classification")

    return ads, run_meta


# ── Authentication / session state classification ───────────────────────────
#
# This layer never talks to Facebook directly -- authentication, the GraphQL
# requests, and pagination all happen inside the separate Apify actor
# (AUTH_ACTOR_ID). From here we can only reason about two things: whether
# cookies were supplied at all, and the Apify run's own reported status /
# status message plus whatever ads it actually returned. Where the actor
# gives no usable signal we report "unknown" rather than guessing -- this
# function must never claim a state (especially session_valid=True) that it
# cannot actually support, which is the bug being fixed here (previously the
# app displayed "Authenticated mode" the instant cookies were present, with
# no regard for whether the session behind them was still good).
AUTH_FAILURE_SIGNATURES = {
    "checkpoint_required": ("checkpoint", "suspicious activity", "verify your identity", "confirm it's you"),
    "session_expired":     ("login", "log in", "logged out", "not logged in", "session expired", "redirect"),
    "csrf_tokens_missing": ("fb_dtsg", "lsd token", "csrf"),
}


# Conservative status precedence used by both classify_auth_state and
# combine_auth_states: whichever of these appears first in this tuple for a
# given classification is reported as the overall `status`, most-concerning
# first. "verified" is only ever reached once every more-concerning signal
# has been ruled out.
AUTH_STATUS_PRECEDENCE = (
    "checkpoint_required",
    "session_expired",      # covers both an outright expiry and a login-wall redirect
    "no_cookies_provided",
    "unverified",
    "verified",
)


def _normalize_telemetry_entries(telemetry):
    """Normalize AUTH_TELEMETRY into a list of per-URL entry dicts.

    The real actor (meta-ads-auth-scraper, src/main.js) always calls
    `Actor.setValue('AUTH_TELEMETRY', authTelemetry)` with `authTelemetry`
    an ARRAY built by `authTelemetry.push({ url, country, ...authState })`
    once per searched URL, where `authState` comes from
    `classifyAuthentication()` (src/lib.js) and has exactly these fields:
    cookies_provided (bool), auth_status (one of "no_cookies_provided",
    "checkpoint_required", "session_expired", "unverified", "verified"),
    checkpoint_detected (bool), login_wall_detected (bool).

    A bare object (a single entry, not wrapped in a list) is also accepted
    defensively in case a future single-URL run ever emits one directly.

    Returns None only when telemetry itself is unavailable (the wait_for_run
    caller never fetched/found a record). Any other shape -- including an
    empty list or a value that is neither a dict nor a list -- means
    telemetry IS present but unusable, which must never fall back to the
    legacy ads-returned heuristic (see classify_auth_state below).
    """
    if telemetry is None:
        return None
    if isinstance(telemetry, dict):
        return [telemetry]
    if isinstance(telemetry, list):
        return telemetry
    return []


def _classify_telemetry_entry(entry):
    """Classify one AUTH_TELEMETRY entry into one of the five named
    statuses, using the required precedence: checkpoint > login-wall /
    session-expired > no-cookies > unverified > verified. A field's boolean
    signal (checkpoint_detected / login_wall_detected / cookies_provided)
    takes priority over auth_status so a self-contradictory entry (e.g.
    auth_status "verified" alongside checkpoint_detected true) is never
    reported as verified -- requirement 6 forbids trusting a nonexistent
    `verified: true` field, and this is the conservative reading of
    `auth_status: "verified"` itself: only trustworthy when nothing else in
    the same entry disagrees with it.
    """
    if not isinstance(entry, dict):
        return "unverified"

    auth_status         = entry.get("auth_status")
    checkpoint_detected = entry.get("checkpoint_detected")
    login_wall_detected = entry.get("login_wall_detected")
    cookies_provided     = entry.get("cookies_provided")

    if checkpoint_detected is True or auth_status == "checkpoint_required":
        return "checkpoint_required"
    if login_wall_detected is True or auth_status in ("session_expired", "logged_out"):
        return "session_expired"
    if cookies_provided is False or auth_status == "no_cookies_provided":
        return "no_cookies_provided"
    if auth_status == "unverified":
        return "unverified"
    if auth_status == "verified":
        return "verified"
    # Unrecognized/missing auth_status and no boolean signal fired --
    # malformed data, conservatively unverified rather than guessed.
    return "unverified"


def classify_auth_state(cookies_present, run_status, run_status_message, ads, telemetry=None):
    """Best-effort session-state classification for one authenticated-actor run.

    Returns a dict with the five states Phase 2 requires, reported
    separately: cookies_stored, session_valid, session_expired,
    checkpoint_required, csrf_tokens_missing. session_valid may be True,
    False, or the string "unknown" when this layer genuinely cannot tell.

    Also reports `status`, a single conservative summary value following
    AUTH_STATUS_PRECEDENCE. `telemetry`, when provided, is the actor's own
    AUTH_TELEMETRY record (see wait_for_run / _normalize_telemetry_entries)
    -- either a single object or an array of per-URL entries -- and is
    authoritative over the run status-message heuristics below: once
    telemetry is present at all, ads merely being present is never
    sufficient on its own to report "verified", and a malformed/unusable
    telemetry payload is reported "unverified" rather than silently falling
    back to the legacy ads-returned heuristic.
    """
    state = {
        "cookies_stored":      bool(cookies_present),
        "session_valid":       "unknown",
        "session_expired":     False,
        "checkpoint_required": False,
        "csrf_tokens_missing": False,
        "status":              "unverified",
    }

    if not cookies_present:
        # No cookies were ever presented -- there is no session to evaluate.
        state["session_valid"] = False
        state["status"] = "no_cookies_provided"
        return state

    entries = _normalize_telemetry_entries(telemetry)

    if entries is not None:
        # Telemetry is present (object or array) -- authoritative over the
        # status-message heuristics and the legacy ads-returned fallback.
        # Multiple per-URL entries combine via the same conservative
        # precedence used across the whole job: the most-concerning status
        # among them wins.
        entry_statuses = [_classify_telemetry_entry(e) for e in entries] or ["unverified"]
        telemetry_status = next(
            (st for st in AUTH_STATUS_PRECEDENCE if st in entry_statuses),
            "unverified",
        )
        if telemetry_status == "checkpoint_required":
            state["checkpoint_required"] = True
            state["session_valid"] = False
        elif telemetry_status == "session_expired":
            state["session_expired"] = True
            state["session_valid"] = False
        elif telemetry_status == "no_cookies_provided":
            state["session_valid"] = False
        elif telemetry_status == "verified":
            state["session_valid"] = True
        state["status"] = telemetry_status
        return state

    # No telemetry was available at all -- fall back to the status-message
    # heuristics below, then (if those found nothing) the legacy
    # ads-returned signal.
    haystack = (run_status_message or "").lower()
    for key, signatures in AUTH_FAILURE_SIGNATURES.items():
        if any(sig in haystack for sig in signatures):
            state[key] = True

    if state["checkpoint_required"]:
        state["session_valid"] = False
        state["status"] = "checkpoint_required"
        return state

    if state["session_expired"]:
        state["session_valid"] = False
        state["status"] = "session_expired"
        return state

    if state["csrf_tokens_missing"]:
        # Not one of the five named statuses -- surfaced via the
        # csrf_tokens_missing flag, summarized conservatively as unverified.
        state["session_valid"] = False
        state["status"] = "unverified"
        return state

    if run_status and run_status != "SUCCEEDED":
        # The actor run itself did not finish cleanly and its status message
        # (if any) didn't match a recognizable Facebook-side signature above.
        # This is still a real failure -- just one we can't further classify
        # from here.
        state["session_valid"] = False
        state["status"] = "unverified"
        return state

    if not ads:
        # The run "succeeded" but returned nothing. That is not proof the
        # session actually unlocked authenticated content, so this stays
        # unknown rather than being reported as valid.
        state["status"] = "unverified"
        return state

    # No telemetry was available, so this falls back to the pre-existing
    # heuristic: a clean run with cookies supplied that actually returned
    # ads is the closest positive signal available at this layer. It is
    # still not a GraphQL-level confirmation (that lives inside the actor).
    state["session_valid"] = True
    state["status"] = "verified"
    return state


def combine_auth_states(states):
    """Merge the per-search auth states from one job into a single summary."""
    if not states:
        return classify_auth_state(False, None, None, [])
    combined = {
        "cookies_stored":      any(s.get("cookies_stored") for s in states),
        "session_expired":     any(s.get("session_expired") for s in states),
        "checkpoint_required": any(s.get("checkpoint_required") for s in states),
        "csrf_tokens_missing": any(s.get("csrf_tokens_missing") for s in states),
    }
    values = [s.get("session_valid") for s in states]
    if any(v is True for v in values):
        combined["session_valid"] = True
    elif any(v == "unknown" for v in values):
        combined["session_valid"] = "unknown"
    else:
        combined["session_valid"] = False

    # Conservative overall status: whichever named status appears among the
    # per-search results that sorts earliest (most concerning) in
    # AUTH_STATUS_PRECEDENCE wins, e.g. one search hitting a checkpoint marks
    # the whole job checkpoint_required even if another search looked clean.
    statuses = {s.get("status") for s in states if s.get("status")}
    combined["status"] = next((st for st in AUTH_STATUS_PRECEDENCE if st in statuses), "unverified")
    return combined


# ── Ad data helpers ───────────────────────────────────────────────────────────

def fb_page_to_adlib_url(page_url, status, country):
    """Convert a Facebook page/profile URL into a Meta Ad Library page-search URL.
    Handles profile.php?id=NUMERIC and /pages/.../NUMERIC. Returns None if no ID found.
    """
    if not page_url:
        return None
    page_url = page_url.strip()
    # Ignore obvious non-page junk (e.g. lines from a pasted cookie JSON)
    if any(ch in page_url for ch in ('{', '}', '"', ':')) and "facebook.com" not in page_url:
        return None
    # If it's already a full Ad Library URL, use it as-is
    if "facebook.com/ads/library" in page_url:
        return page_url
    page_id = None
    # view_all_page_id=123...  (Ad Library page param)
    m = re.search(r"view_all_page_id=(\d+)", page_url)
    if m:
        page_id = m.group(1)
    # profile.php?id=123...  or ?id=123 / &id=123
    if not page_id:
        m = re.search(r"(?:profile\.php\?id=|[?&]id=)(\d+)", page_url)
        if m:
            page_id = m.group(1)
    # A bare numeric ID pasted on its own (Facebook page IDs are ~8-20 digits)
    if not page_id and page_url.isdigit() and 6 <= len(page_url) <= 20:
        page_id = page_url
    # /pages/Name/123456 — only for actual facebook URLs
    if not page_id and "facebook.com" in page_url:
        m = re.search(r"/(\d{6,})/?(?:[?#]|$)", page_url)
        if m:
            page_id = m.group(1)
    if not page_id:
        return None
    # Use a CONCRETE country (not ALL) — country=ALL makes the Ad Library show a
    # country picker in a headless browser and never fires the ad-results query.
    # Honor the caller's selected status (e.g. "active" for Active Only, "all"
    # for All Ads) instead of hardcoding — a competitor exact-page search must
    # respect the same status filter as keyword/domain searches.
    _status = status if status in ("active", "all") else "active"
    return (
        f"https://www.facebook.com/ads/library/"
        f"?active_status={_status}&ad_type=all&country={country or 'US'}"
        f"&view_all_page_id={page_id}&search_type=page&media_type=all"
    )


def extract_urls(ad):
    """Return (images[], videos[]) from a curious_coder ad record.
    Field names confirmed snake_case from debug output.
    """
    imgs, vids = [], []
    snap = ad.get("snapshot") or {}

    def add_img(v):
        if v and isinstance(v, str) and v not in imgs:
            imgs.append(v)
    def add_vid(v):
        if v and isinstance(v, str) and v not in vids:
            vids.append(v)

    def add_one_img(obj):
        """Add just ONE url per image object (variants of the same picture)."""
        if isinstance(obj, dict):
            add_img(obj.get("resized_image_url") or obj.get("original_image_url") or obj.get("url"))
        elif isinstance(obj, str):
            add_img(obj)

    # snapshot.images[] — objects with resized_image_url / original_image_url / url
    for img_obj in snap.get("images") or []:
        add_one_img(img_obj)

    # snapshot.videos[]
    for vid_obj in snap.get("videos") or []:
        if isinstance(vid_obj, dict):
            add_vid(vid_obj.get("video_hd_url"))
            add_vid(vid_obj.get("video_sd_url"))
            add_vid(vid_obj.get("url"))
            add_img(vid_obj.get("video_preview_image_url"))
            add_img(vid_obj.get("thumbnail_url"))
        elif isinstance(vid_obj, str):
            add_vid(vid_obj)

    # Carousel cards — one image per card
    for card in snap.get("cards") or []:
        add_img(card.get("resized_image_url") or card.get("original_image_url") or card.get("url"))
        add_vid(card.get("video_hd_url"))
        add_vid(card.get("video_sd_url"))

    # extra_images / extra_videos (confirmed in snapshot keys)
    for img_obj in snap.get("extra_images") or []:
        add_one_img(img_obj)
    for vid_obj in snap.get("extra_videos") or []:
        if isinstance(vid_obj, dict):
            add_vid(vid_obj.get("video_hd_url"))
            add_vid(vid_obj.get("video_sd_url"))
            add_vid(vid_obj.get("url"))
        elif isinstance(vid_obj, str):
            add_vid(vid_obj)

    # Top-level snapshot fallbacks — one image only
    if not imgs:
        add_img(snap.get("resized_image_url") or snap.get("original_image_url"))
    add_vid(snap.get("video_hd_url"))
    add_vid(snap.get("video_sd_url"))

    # Video-only: use preview as image stand-in
    if vids and not imgs:
        add_img(snap.get("video_preview_image_url"))

    return imgs, vids


def normalize_ad(ad):
    """Flatten a curious_coder record into a display dict.
    All top-level fields use snake_case (confirmed from debug output).
    """
    snap = ad.get("snapshot") or {}

    name = ad.get("page_name") or snap.get("page_name") or "Unknown"

    status = "ACTIVE" if ad.get("is_active") else "INACTIVE"

    raw_date = ad.get("start_date", "")
    if isinstance(raw_date, (int, float)) and raw_date > 0:
        try:
            raw_date = datetime.fromtimestamp(raw_date).strftime("%Y-%m-%d")
        except Exception:
            raw_date = ""
    elif isinstance(raw_date, str) and raw_date:
        pass  # already a string date

    body  = (snap.get("body")  or {})
    body  = body.get("text", "") if isinstance(body, dict) else str(body or "")
    title = (snap.get("title") or {})
    title = title.get("text", "") if isinstance(title, dict) else str(title or "")

    cta = snap.get("cta_text") or snap.get("cta_type") or ""

    landing = snap.get("link_url") or snap.get("landing_page_url") or ""
    if not landing:
        for c in snap.get("cards", []):
            landing = c.get("link_url") or ""
            if landing:
                break

    ad_id   = str(ad.get("ad_archive_id") or ad.get("ad_id") or "")
    lib_url = ad.get("ad_library_url") or (f"https://www.facebook.com/ads/library/?id={ad_id}" if ad_id else "#")

    # Page ID — unique per advertiser account (disambiguates same-name pages)
    page_id = str(ad.get("page_id") or snap.get("page_id") or "")
    page_ads_url = (
        f"https://www.facebook.com/ads/library/?active_status=all&ad_type=all"
        f"&country=ALL&view_all_page_id={page_id}&search_type=page&media_type=all"
    ) if page_id else ""

    # Impressions index → human range
    impressions = ""
    imp_idx = -1
    imp = ad.get("impressions_with_index") or {}
    if isinstance(imp, dict):
        # Facebook returns snake_case; handle both just in case
        idx = imp.get("impressions_index", imp.get("impressionsIndex", -1))
        # Fallback: derive index from lower_bound string if index missing
        if idx == -1 and imp.get("lower_bound"):
            try:
                lb = int(str(imp["lower_bound"]).replace(",", ""))
                thresholds = [1000, 5000, 20000, 50000, 100000, 500000, 1000000]
                idx = next((i for i, t in enumerate(thresholds) if lb < t), 7)
            except Exception:
                pass
        ranges = ["<1K", "1K–5K", "5K–20K", "20K–50K", "50K–100K", "100K–500K", "500K–1M", ">1M"]
        if 0 <= idx < len(ranges):
            impressions = ranges[idx]
            imp_idx = idx

    variants = ad.get("collation_count", 0) or 0

    pubs  = ad.get("publisher_platform") or []
    plats = ", ".join(p.capitalize() for p in pubs) if pubs else "Facebook"

    return {
        "name":        name,
        "status":      status,
        "date":        str(raw_date),
        "body":        body,
        "title":       title,
        "cta":         cta,
        "landing":     landing,
        "lib_url":     lib_url,
        "impressions": impressions,
        "imp_idx":     imp_idx,
        "variants":    int(variants),
        "plats":       plats,
        "ad_id":       ad_id,
        "page_id":     page_id,
        "page_ads_url": page_ads_url,
    }


def ad_record(ad):
    """The retained / JSON form of one ad: normalize_ad() plus the creative
    URLs, format and translation the viewer renders. Built only from ad
    content fields -- never the raw actor record.
    """
    imgs, vids = extract_urls(ad)
    rec = normalize_ad(ad)
    rec.update({
        "format":               "VIDEO" if vids else ("IMAGE" if imgs else "UNKNOWN"),
        "images":               imgs,
        "videos":               vids,
        "gated_type":           ad.get("gated_type") or "",
        "translation":          ad.get("_translation") or "",
        "translation_language": ad.get("_trans_lang") or "",
    })
    return rec


# ── Translation ────────────────────────────────────────────────────────────

# Common English function words — cheap local language guess (no API call)
_EN_WORDS = {
    "the", "and", "you", "your", "for", "with", "this", "that", "our", "get",
    "now", "free", "best", "how", "why", "what", "are", "can", "will", "new",
    "more", "all", "from", "have", "has", "was", "not", "but", "out", "here",
    "today", "off", "save", "shop", "buy", "learn", "try", "see", "help",
}

def looks_english(text):
    """Cheap heuristic: is this text probably English? Avoids an API call.
    Returns True if English (skip translation), False if likely foreign.
    """
    if not text or not text.strip():
        return True  # nothing to translate

    # Non-Latin scripts (Arabic, Chinese, Cyrillic, Hebrew, etc.) → definitely foreign
    for ch in text:
        o = ord(ch)
        if (0x0400 <= o <= 0x04FF or   # Cyrillic
            0x0590 <= o <= 0x05FF or   # Hebrew
            0x0600 <= o <= 0x06FF or   # Arabic
            0x4E00 <= o <= 0x9FFF or   # CJK (Chinese/Japanese kanji)
            0x3040 <= o <= 0x30FF or   # Japanese kana
            0xAC00 <= o <= 0xD7AF or   # Korean
            0x0E00 <= o <= 0x0E7F):    # Thai
            return False

    # Latin script — count how many English function words appear
    words = re.findall(r"[a-zA-ZÀ-ÿ]+", text.lower())
    if len(words) < 4:
        return True  # too short to judge, don't waste an API call
    hits = sum(1 for w in words if w in _EN_WORDS)
    ratio = hits / len(words)
    # If almost no English function words, it's probably a Latin-script foreign
    # language (Spanish, German, French, etc.) → translate
    return ratio >= 0.08


_translate_error = None  # captures first API error for surfacing in job log

def translate_text(title, body):
    """Detect language + translate to English via Claude Haiku.
    Calls the Anthropic API directly with urllib (no SDK dependency).
    Returns (language, translation) or (None, None) if English/unavailable.
    """
    global _translate_error
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None, None
    text = f"{title}\n\n{body}".strip()
    if not text:
        return None, None
    try:
        payload = json.dumps({
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 1024,
            "messages": [{
                "role": "user",
                "content": (
                    "Detect the language of this ad copy.\n"
                    "If it is English, reply with exactly: ENGLISH\n"
                    "If it is another language, reply in this exact format:\n"
                    "LANGUAGE: [detected language name]\n"
                    "TRANSLATION:\n"
                    "[full English translation]\n\n"
                    f"Ad copy:\n{text}"
                ),
            }],
        }).encode()

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "x-api-key":         api_key,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            method="POST",
        )

        # Retry on transient overload/rate-limit (529 / 429) with backoff
        resp = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    resp = json.loads(r.read())
                break
            except urllib.error.HTTPError as he:
                if he.code in (429, 529) and attempt < 3:
                    time.sleep(2 * (attempt + 1))  # 2s, 4s, 6s
                    continue
                raise
        if resp is None:
            raise Exception("no response after retries")

        reply = resp["content"][0]["text"].strip()
        if reply.upper().startswith("ENGLISH"):
            return None, None
        language, translation, in_trans = "", "", False
        for line in reply.splitlines():
            if line.startswith("LANGUAGE:"):
                language = line.replace("LANGUAGE:", "").strip()
            elif line.startswith("TRANSLATION:"):
                in_trans = True
            elif in_trans:
                translation += line + "\n"
        return language, translation.strip()
    except urllib.error.HTTPError as e:
        try:
            body_err = e.read().decode()
        except Exception:
            body_err = ""
        msg = f"HTTP {e.code}: {body_err[:200]}"
        print(f"[TRANSLATE] {msg}")
        _translate_error = msg
        return None, None
    except Exception as e:
        print(f"[TRANSLATE] error: {e}")
        _translate_error = str(e)
        return None, None


def translate_ads_bulk(ads, log):
    """Translate all non-English ads in parallel threads, storing result on each ad."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log("  🌐 Translation skipped — ANTHROPIC_API_KEY not set")
        return

    def worker(ad):
        n = normalize_ad(ad)
        # Local pre-filter — skip English ads before spending an API call
        if looks_english(f"{n['title']}\n{n['body']}"):
            return
        lang, trans = translate_text(n["title"], n["body"])
        if trans:
            ad["_translation"] = trans
            ad["_trans_lang"]  = lang

    # Only spawn threads for ads that fail the English check
    foreign = [ad for ad in ads
               if not looks_english(f"{normalize_ad(ad)['title']}\n{normalize_ad(ad)['body']}")]
    if not foreign:
        log("  🌐 All ads appear English — translation skipped (no API cost)")
        return

    log(f"  🌐 {len(foreign)} non-English ad(s) detected — translating…")
    threads = [threading.Thread(target=worker, args=(ad,)) for ad in foreign]
    for t in threads: t.start()
    for t in threads: t.join()

    n_trans = sum(1 for ad in ads if ad.get("_translation"))
    log(f"  🌐 Translated {n_trans} ad(s)")
    if n_trans == 0 and _translate_error:
        log(f"  ⚠️ Translation API error: {_translate_error}")


# ── Authenticated Meta scraper (custom Apify actor) ───────────────────────────

def meta_auth_search(search_urls, cookies_list, count, country, ad_status, log):
    """
    Authenticated scrape via a custom Apify Playwright actor.
    The actor runs on Apify's infrastructure (handles proxies + fingerprinting)
    and injects the user's Facebook session cookies to unlock LOGGED_OUT creatives.
    """
    if not AUTH_ACTOR:
        log("  ❌ AUTH_ACTOR_ID env var not set — deploy the meta-auth-actor first")
        return [], classify_auth_state(bool(cookies_list), None, None, [])

    # cookies_stored is a fact (we have them); it is NOT proof the Facebook
    # session behind them is still valid, so we don't call this "Authenticated
    # mode" yet -- that claim is only made after the run comes back and is
    # classified below.
    log(f"  🔐 cookies_stored — attempting authenticated run via actor {AUTH_ACTOR}")
    try:
        run = api_post(f"acts/{AUTH_ACTOR}/runs", {
            "urls":    search_urls,
            "cookies": cookies_list,
            "count":   count,
        })
        run_id = run["data"]["id"]
        ads, run_meta = wait_for_run(run_id, log)
        auth_state = classify_auth_state(True, run_meta.get("status"), run_meta.get("status_message"), ads,
                                          telemetry=run_meta.get("telemetry"))
        # Deliberately NOT forcing ad["gated_type"] = "ELIGIBLE" here anymore.
        # Whatever gate status the actor itself assigned per ad is the only
        # real signal of whether that specific ad's authenticated content was
        # actually unlocked -- overwriting it just because cookies were
        # supplied is exactly the false "authenticated" claim this fix removes.
        if auth_state["session_valid"] is True:
            log(f"  🔐 Authenticated — {len(ads)} ads from auth actor")
        elif auth_state["session_valid"] == "unknown":
            log(f"  🔐 Run completed but could not confirm session validity — {len(ads)} ads from auth actor")
        else:
            reasons = [k for k in ("session_expired", "checkpoint_required", "csrf_tokens_missing") if auth_state[k]]
            reason_txt = f" ({', '.join(reasons)})" if reasons else ""
            log(f"  ⚠️ Authenticated run did not confirm a valid session{reason_txt} — {len(ads)} ads from auth actor")
        return ads, auth_state
    except Exception as e:
        log(f"  ❌ Auth actor failed: {e}")
        return [], classify_auth_state(True, "FAILED", str(e), [])


# ── Scrape worker ─────────────────────────────────────────────────────────────

def run_job(job_id, brand, country, searches, domains, page_urls, ad_status, cookies=None, per_page=0):
    job = jobs[job_id]
    def log(msg): job["log"].append(msg)

    # Backward-compat: allow single string for domains/page_urls
    if isinstance(domains, str):   domains   = [domains]   if domains   else []
    if isinstance(page_urls, str): page_urls = [page_urls] if page_urls else []

    try:
        _country = country or "US"
        _status  = ad_status if ad_status in ("active", "all") else "active"
        results  = [[] for _ in searches]
        auth_states = [None for _ in searches]

        def run_search(i, queries):
            log(f"🔍 Search {i+1}/{len(searches)}: {queries}")
            try:
                urls = [
                    {"url": (
                        f"https://www.facebook.com/ads/library/"
                        f"?active_status={_status}&ad_type=all&country={_country}"
                        f"&q={urlquote(q)}&search_type=keyword_unordered&media_type=all"
                    )}
                    for q in queries if q.strip()
                ]
                if i == 0:
                    # Domain searches: find all advertisers driving to each domain
                    for domain in domains:
                        urls.append({"url": (
                            f"https://www.facebook.com/ads/library/"
                            f"?active_status={_status}&ad_type=all&country={_country}"
                            f"&q={urlquote(domain)}&search_type=page_like_and_ads_using_domain&media_type=all"
                        )})
                        log(f"   🌐 Domain search: {domain}")
                    # Page searches: convert each to an Ad Library page-search URL
                    for page_url in page_urls:
                        adlib = fb_page_to_adlib_url(page_url, _status, _country)
                        if adlib:
                            urls.append({"url": adlib})
                            log(f"   📄 Page → Ad Library: {adlib}")
                        else:
                            log(f"   ⚠️ Couldn't extract a page ID from: {page_url}")

                if not urls:
                    results[i] = []
                    return

                if cookies:
                    ads, auth_state = meta_auth_search(urls, cookies, count=AUTH_ACTOR_COUNT,
                                           country=_country, ad_status=_status, log=log)
                    auth_states[i] = auth_state
                else:
                    run    = api_post(f"acts/{META_ACTOR}/runs", {"urls": urls, "count": 15, "scrapeAdDetails": True})
                    run_id = run["data"]["id"]
                    ads, _run_meta = wait_for_run(run_id, log)
                log(f"   ✓ {len(ads)} ads returned")
                results[i] = ads
            except Exception as e:
                log(f"   ✗ Error: {e}")
                results[i] = []

        threads = [threading.Thread(target=run_search, args=(i, q)) for i, q in enumerate(searches)]
        for t in threads: t.start()
        for t in threads: t.join()

        all_ads = []
        for r in results:
            all_ads.extend(r)

        # Deduplicate by archive ID
        seen, unique = set(), []
        for ad in all_ads:
            aid = ad.get("adArchiveID") or ad.get("ad_archive_id") or id(ad)
            if aid not in seen:
                seen.add(aid)
                unique.append(ad)

        # Discovery mode: cap ads per advertiser (keep the highest-impression / newest)
        if per_page and per_page > 0:
            def _rank(ad):
                n = normalize_ad(ad)
                return (n.get("imp_idx", -1), n.get("date", ""))
            by_adv = {}
            for ad in unique:
                name = normalize_ad(ad)["name"]
                by_adv.setdefault(name, []).append(ad)
            capped = []
            for name, ads_list in by_adv.items():
                ads_list.sort(key=_rank, reverse=True)
                capped.extend(ads_list[:per_page])
            log(f"🔎 Discovery mode: {len(by_adv)} pages, capped to {per_page} ads each → {len(capped)} ads (from {len(unique)})")
            unique = capped

        log(f"📊 {len(unique)} unique ads")

        job["auth_state"] = combine_auth_states([s for s in auth_states if s is not None])
        st = job["auth_state"]
        log(
            f"🔐 Auth state: cookies_stored={st['cookies_stored']} "
            f"session_valid={st['session_valid']} "
            f"session_expired={st['session_expired']} "
            f"checkpoint_required={st['checkpoint_required']} "
            f"csrf_tokens_missing={st['csrf_tokens_missing']}"
        )

        # DEBUG: dump snapshot structure of the first ad with no extractable creative
        for ad in unique:
            imgs_dbg, vids_dbg = extract_urls(ad)
            if not imgs_dbg and not vids_dbg:
                snap_dbg = ad.get("snapshot") or {}
                print(f"[NOCREATIVE] snapshot keys = {list(snap_dbg.keys())}")
                print(f"[NOCREATIVE] sample = {json.dumps(snap_dbg)[:1200]}")
                break
        translate_ads_bulk(unique, log)
        # Retain the same normalized ads the viewer renders so the JSON API
        # can serve them without HTML parsing. Set before status flips to
        # "done" so a completed job always has its ads.
        ads_out = [ad_record(ad) for ad in unique]
        job["html"]   = build_viewer(brand, country, unique)
        job["ads"]    = ads_out
        job["completed_at"] = datetime.now(timezone.utc).isoformat()
        job["status"] = "done"
        log("✅ Done!")

    except Exception as e:
        import traceback
        job["error"]  = str(e)
        job["status"] = "error"
        log(f"❌ {e}")
        log(traceback.format_exc())


# ── Viewer builder ─────────────────────────────────────────────────────────────

def build_viewer(brand, country, ads):
    C = "#1877f2"  # Meta blue

    total      = len(ads) or 1
    active_cnt = sum(1 for a in ads if a.get("is_active"))
    has_media  = sum(1 for a in ads if any(extract_urls(a)))
    q_media    = round(has_media / total * 100)

    # ── Card builder ──────────────────────────────────────────────────────────
    def card(ad):
        n         = normalize_ad(ad)
        imgs, vids = extract_urls(ad)
        fmt       = "VIDEO" if vids else ("IMAGE" if imgs else "UNKNOWN")
        lib_url   = n["lib_url"]
        lp        = n["landing"] or "#"
        try:    lp_host = urlparse(lp).netloc or lp
        except: lp_host = lp

        # Media block
        def pimg(u): return f"/img?u={urlquote(u)}"
        if vids:
            dl_links = " ".join(
                f'<a href="{v}" target="_blank" class="vid-dl">▶ Watch Video {i+1}</a>'
                for i, v in enumerate(vids[:3]))
            if imgs:
                # Show proxied thumbnail + watch link
                thumb_html = f'<img src="{pimg(imgs[0])}" style="width:100%;max-height:320px;object-fit:contain;display:block;cursor:pointer" onclick="window.open(\'{vids[0]}\',\'_blank\')">'
                media = (f'<div class="media-wrap">'
                         f'{thumb_html}'
                         f'<div class="vid-dl-row">{dl_links}</div></div>')
            else:
                # No thumbnail — just show watch links
                media = (f'<div class="media-wrap" style="background:#111;min-height:120px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;padding:16px">'
                         f'<span style="font-size:36px">🎬</span>'
                         f'<div class="vid-dl-row" style="justify-content:center">{dl_links}</div></div>')
        elif imgs:
            img_html = "".join(
                f'<img src="{pimg(img)}" onclick="openFull(\'{pimg(img)}\')">'
                for img in imgs[:4])
            media = (f'<div class="media-wrap img-grid img-count-{min(len(imgs),4)}">'
                     f'{img_html}</div>')
        else:
            gated    = ad.get("gated_type") or ""
            is_sens  = ad.get("contains_sensitive_content")
            prof_pic = (ad.get("snapshot") or {}).get("page_profile_picture_url") or ""
            if gated == "LOGGED_OUT":
                reason = "🔒 Login required — Meta only serves this creative to authenticated users"
            elif gated and gated != "ELIGIBLE":
                reason = "🔞 Gated — creative withheld by Meta policy"
            elif not ad.get("is_active"):
                reason = "⏸ Inactive — creative not served by Meta API"
            elif is_sens:
                reason = "⚠️ Sensitive content — creative withheld by Meta"
            else:
                reason = "🖼️ No creative returned"
            prof_html = (f'<img src="{pimg(prof_pic)}" style="width:64px;height:64px;border-radius:50%;object-fit:cover;margin-bottom:6px">'
                         if prof_pic else '<span style="font-size:32px">📄</span>')
            media = (f'<div class="media-placeholder" style="min-height:120px">'
                     f'{prof_html}'
                     f'<span style="font-size:11px;color:#999;text-align:center;padding:0 12px">{reason}</span>'
                     f'<a href="{lib_url}" target="_blank" style="margin-top:4px;font-size:12px">View in Ad Library →</a></div>')

        body_html  = (n["body"]  or "").replace('"', '&quot;').replace('\n', '<br>')
        title_html = (n["title"] or "").replace('"', '&quot;')
        cta        = n["cta"] or ""
        imp_badge  = f'<span class="badge imp">👁 {n["impressions"]}</span>' if n["impressions"] else ""
        var_badge  = f'<span class="badge hot">🔥 {n["variants"]} variants</span>' if n["variants"] > 2 else ""
        st_cls     = "active" if n["status"] == "ACTIVE" else "inactive"

        # Data attributes for JS (filter / sort / search / export)
        adv_slug  = (n["name"]  or "Unknown").replace('"', "'")
        body_slug = (n["body"]  or "").replace('"', "'").replace('\n', ' ')[:300]
        ttl_slug  = (n["title"] or "").replace('"', "'")[:120]
        cp_text   = f"{n['title'] or ''}\n\n{n['body'] or ''}".strip().replace('"', "'")[:600]
        lp_slug   = lp.replace('"', "'")
        lib_slug  = lib_url.replace('"', "'")
        # Auto-translation (set during scrape for non-English ads)
        trans      = ad.get("_translation") or ""
        trans_lang = ad.get("_trans_lang")  or "original language"
        if trans:
            t_html   = trans.replace('<', '&lt;').replace('>', '&gt;').replace('\n', '<br>')
            trans_html = (
                f'<div class="card-translation">'
                f'<div class="trans-lang">🌐 Translated from {trans_lang}:</div>'
                f'<div class="trans-text">{t_html}</div>'
                f'</div>'
            )
        else:
            trans_html = ""

        imgs_attr      = ",".join(f"/img?u={urlquote(img)}" for img in imgs[:4])
        vids_attr      = ",".join(f"/vid?u={urlquote(v)}" for v in vids[:3])
        orig_imgs_attr = ",".join(imgs[:4])
        orig_vids_attr = ",".join(vids[:3])

        return (
            f'<div class="card" data-status="{n["status"]}" data-fmt="{fmt}"'
            f' data-advertiser="{adv_slug}" data-body="{body_slug}" data-title="{ttl_slug}"'
            f' data-date="{n["date"]}" data-imp="{n["imp_idx"]}" data-cta="{cta}" data-lp="{lp_slug}" data-lib="{lib_slug}"'
            f' data-imgs="{imgs_attr}" data-vids="{vids_attr}"'
            f' data-orig-imgs="{orig_imgs_attr}" data-orig-vids="{orig_vids_attr}"'
            f' data-pageid="{n["page_id"]}">'
            f'<label class="card-cb-wrap" onclick="event.stopPropagation()"><input type="checkbox" class="card-cb" onchange="toggleSelect(this)"></label>'
            f'<div class="card-header">'
            f'<div class="card-name">{n["name"]}'
            f'{f" <span style=\"font-size:10px;color:#aaa;font-weight:normal\">#{n['page_id'][-6:]}</span>" if n["page_id"] else ""}</div>'
            f'<div class="card-meta">{n["date"]} · {n["plats"]}'
            f'{f" · <a href=\"{n['page_ads_url']}\" target=\"_blank\" style=\"color:#1877f2;text-decoration:none\">all ads from this page ↗</a>" if n["page_ads_url"] else ""}</div>'
            f'<div class="badge-row">'
            f'<span class="badge {st_cls}">{n["status"]}</span>'
            f'<span class="badge fmt">{fmt}</span>'
            f'{imp_badge}{var_badge}'
            f'</div></div>'
            f'{media}'
            f'<div class="card-body">'
            f'{f"<div class=ad-title>{title_html}</div>" if title_html else ""}'
            f'<div class="ad-copy">{body_html or "<em style=color:#aaa>No copy text</em>"}</div>'
            f'</div>'
            f'<div class="card-footer">'
            f'<div class="footer-meta">'
            f'{f"<span class=cta-pill>{cta}</span>" if cta else ""}'
            f'<a href="{lp}" target="_blank" class="lp-link" title="{lp_slug}">{lp_host}</a>'
            f'<a href="{lib_url}" target="_blank" class="lib-link">Ad Library ↗</a>'
            f'</div>'
            f'<div class="footer-actions">'
            f'<button class="btn-sm" onclick="copyText(this)" data-text="{cp_text}">📋 Copy</button>'
            f'{f"<button class=btn-sm onclick=dlVideo(this) data-src={vids[0]}>⬇ Video</button>" if vids else ""}'
            f'<button class="btn-sm" onclick="shotCard(this)">📷 Shot</button>'
            f'</div></div>'
            f'{trans_html}'
            f'</div>'
        )

    # ── Table row builder ─────────────────────────────────────────────────────
    def trow(ad):
        n          = normalize_ad(ad)
        imgs, vids = extract_urls(ad)
        fmt        = "VIDEO" if vids else ("IMAGE" if imgs else "UNKNOWN")
        thumb      = imgs[0] if imgs else ""
        lp         = n["landing"] or "#"
        try:    lp_host = urlparse(lp).netloc or lp
        except: lp_host = lp
        st_cls     = "active" if n["status"] == "ACTIVE" else "inactive"
        cp_text    = f"{n['title'] or ''}\n\n{n['body'] or ''}".strip().replace('"', "'")[:600]
        body_short = (n["body"] or "")[:160]
        adv_slug   = (n["name"] or "").replace('"', "'")
        body_slug  = (n["body"] or "").replace('"', "'").replace('\n', ' ')[:200]

        def pimg(u): return f"/img?u={urlquote(u)}"
        if vids:
            media_cell = f'<video src="{vids[0]}" class="table-thumb" controls></video>'
        elif thumb:
            media_cell = f'<img src="{pimg(thumb)}" class="table-thumb" onclick="openFull(\'{pimg(thumb)}\')">'
        else:
            media_cell = "—"

        return (
            f'<tr data-status="{n["status"]}" data-fmt="{fmt}"'
            f' data-advertiser="{adv_slug}" data-date="{n["date"]}" data-body="{body_slug}">'
            f'<td>{media_cell}</td>'
            f'<td><strong style="font-size:13px">{n["name"]}</strong>'
            f'<div style="font-size:11px;color:#888;margin-top:2px">{n["date"]} · {n["plats"]}</div></td>'
            f'<td><span class="badge {st_cls}">{n["status"]}</span>'
            f'<br><span class="badge fmt" style="margin-top:3px;display:inline-block">{fmt}</span></td>'
            f'<td class="table-copy">{body_short}{"…" if len(n["body"] or "") > 160 else ""}</td>'
            f'<td style="font-size:12px">{n["cta"] or "—"}</td>'
            f'<td><a href="{lp}" target="_blank" style="font-size:11px;color:{C}">{lp_host}</a></td>'
            f'<td><a href="{n["lib_url"]}" target="_blank" style="font-size:11px;color:#888">↗</a></td>'
            f'<td><button class="btn-sm" onclick="copyText(this)" data-text="{cp_text}">📋</button></td>'
            f'</tr>'
        )

    cards_html = "\n".join(card(a) for a in ads)
    rows_html  = "\n".join(trow(a) for a in ads)

    # Clean label for filenames — pull the meaningful part out of a URL/domain
    label = brand
    if "facebook.com/" in label:
        # Facebook page URL → use the page name (last path segment)
        seg = label.rstrip("/").split("facebook.com/")[-1].split("/")[0].split("?")[0]
        label = seg or label
    elif "://" in label or "." in label and "/" in label:
        # Full URL → hostname without www.
        label = (urlparse(label if "://" in label else "https://" + label).netloc or label).replace("www.", "")
    elif label.count(".") >= 1 and " " not in label:
        # Bare domain like get-novaburn.com → strip www.
        label = label.replace("www.", "")
    brand_slug = label.replace(" ", "_")

    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>{brand} — Meta Ad Intelligence</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:Arial,sans-serif;background:#f0f2f5;color:#1a1a1a}}
/* ── Header */
header{{background:{C};color:white;padding:16px 24px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px}}
.header-left h1{{font-size:18px;margin-bottom:4px}}
.header-left a{{color:rgba(255,255,255,.75);font-size:12px;text-decoration:none}}
.header-left a:hover{{color:white}}
.stats{{display:flex;gap:10px;flex-wrap:wrap}}
.stat{{background:rgba(255,255,255,.15);padding:6px 14px;border-radius:20px;text-align:center;min-width:60px}}
.stat strong{{display:block;font-size:20px;font-weight:bold}}
.stat span{{font-size:11px;opacity:.85}}
.export-btn{{background:rgba(255,255,255,.2);color:white;border:1px solid rgba(255,255,255,.4);border-radius:8px;padding:7px 16px;cursor:pointer;font-size:12px;font-weight:bold;white-space:nowrap}}
.export-btn:hover{{background:rgba(255,255,255,.3)}}
/* ── Filters */
.filters{{background:white;border-bottom:1px solid #e0e0e0;padding:10px 24px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;position:sticky;top:0;z-index:100;box-shadow:0 2px 6px rgba(0,0,0,.06)}}
.fbtn{{background:#f0f2f5;color:#333;border:1px solid #ddd;padding:5px 13px;border-radius:14px;cursor:pointer;font-size:12px;white-space:nowrap}}
.fbtn.on,.fbtn:hover{{background:{C};color:white;border-color:{C}}}
.search-box{{padding:5px 12px;border:1px solid #ddd;border-radius:14px;font-size:12px;width:200px;outline:none}}
.search-box:focus{{border-color:{C}}}
.sort-sel{{padding:5px 8px;border:1px solid #ddd;border-radius:14px;font-size:12px;background:white;cursor:pointer;outline:none}}
.view-toggle{{display:flex;border:1px solid #ddd;border-radius:8px;overflow:hidden;flex-shrink:0}}
.vbtn{{background:#f0f2f5;border:none;padding:5px 12px;cursor:pointer;font-size:12px;color:#555}}
.vbtn.on{{background:{C};color:white}}
.fcount{{margin-left:auto;font-size:12px;color:#666;white-space:nowrap}}
/* ── Cards */
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px;padding:20px 24px}}
.grid.hidden{{display:none}}
.card{{background:white;border-radius:10px;overflow:hidden;box-shadow:0 2px 6px rgba(0,0,0,.1);display:flex;flex-direction:column}}
.card-header{{padding:10px 12px 8px;border-bottom:1px solid #f0f0f0}}
.card-name{{font-weight:bold;font-size:13px}}
.card-meta{{font-size:11px;color:#888;margin-top:2px}}
.badge-row{{display:flex;gap:4px;flex-wrap:wrap;margin-top:6px}}
.badge{{font-size:10px;font-weight:bold;padding:2px 7px;border-radius:9px}}
.badge.active{{background:#d4edda;color:#155724}}
.badge.inactive{{background:#f8d7da;color:#721c24}}
.badge.fmt{{background:#e2e3e5;color:#383d41}}
.badge.hot{{background:#fff3cd;color:#856404}}
.badge.imp{{background:#cce5ff;color:#004085}}
/* ── Media */
.media-wrap{{background:#000}}
.media-wrap video{{width:100%;max-height:320px;object-fit:contain;display:block}}
.vid-dl-row{{background:#111;padding:6px 8px;display:flex;gap:8px;flex-wrap:wrap}}
.vid-dl{{color:#7eb8f7;font-size:12px;text-decoration:none;padding:3px 8px;border:1px solid #444;border-radius:4px}}
.vid-dl:hover{{background:#222}}
.img-grid{{display:grid;background:#f7f8fa}}
.img-count-1{{grid-template-columns:1fr}}
.img-count-2,.img-count-3,.img-count-4{{grid-template-columns:1fr 1fr}}
.img-count-1 img{{width:100%;height:auto;max-height:360px;object-fit:contain;cursor:zoom-in;display:block}}
.img-count-2 img,.img-count-3 img,.img-count-4 img{{width:100%;height:170px;object-fit:cover;cursor:zoom-in;border:1px solid #eee}}
.media-placeholder{{background:#f7f8fa;min-height:140px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;color:#999}}
.media-placeholder span{{font-size:36px}}
.media-placeholder a{{color:{C};font-size:13px;font-weight:bold;text-decoration:none}}
/* ── Card body */
.card-body{{padding:10px 12px;flex:1}}
.ad-title{{font-weight:bold;font-size:13px;margin-bottom:4px}}
.ad-copy{{font-size:13px;color:#444;line-height:1.55;max-height:76px;overflow:hidden;transition:max-height .3s}}
.ad-copy.open{{max-height:600px}}
.toggle-copy{{color:{C};font-size:11px;font-weight:bold;cursor:pointer;margin-top:4px;display:inline-block}}
/* ── Card footer */
.card-footer{{padding:8px 12px;border-top:1px solid #f0f0f0;display:flex;flex-direction:column;gap:6px}}
.footer-meta{{display:flex;gap:6px;align-items:center;overflow:hidden}}
.footer-actions{{display:flex;gap:6px}}
.cta-pill{{background:{C};color:white;font-size:10px;font-weight:bold;padding:2px 8px;border-radius:10px;white-space:nowrap;flex-shrink:0}}
.lp-link{{font-size:11px;color:{C};text-decoration:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}}
.lib-link{{font-size:11px;color:#888;text-decoration:none;white-space:nowrap}}
.btn-sm{{font-size:11px;color:#555;background:#f0f2f5;border:1px solid #ddd;border-radius:6px;padding:3px 8px;cursor:pointer;white-space:nowrap}}
.btn-sm:hover{{background:#e4e6e9}}
/* ── Table */
.table-wrap{{padding:20px 24px;overflow-x:auto;display:none}}
.table-wrap.active{{display:block}}
.data-table{{width:100%;border-collapse:collapse;background:white;border-radius:10px;overflow:hidden;box-shadow:0 2px 6px rgba(0,0,0,.1);font-size:13px}}
.data-table th{{background:#f0f2f5;padding:10px 12px;text-align:left;font-size:12px;color:#555;border-bottom:2px solid #e0e0e0;cursor:pointer;white-space:nowrap;user-select:none}}
.data-table th:hover{{background:#e4e6e9}}
.data-table th.asc::after{{content:" ↑"}}
.data-table th.desc::after{{content:" ↓"}}
.data-table td{{padding:8px 12px;border-bottom:1px solid #f0f0f0;vertical-align:top}}
.data-table tr:hover td{{background:#fafafa}}
.table-thumb{{width:64px;height:64px;object-fit:cover;border-radius:6px;cursor:zoom-in;display:block}}
.table-copy{{max-width:280px;font-size:12px;color:#444;line-height:1.4}}
/* ── Advertiser grouping */
.adv-group{{padding:0 24px 8px}}
.adv-group-header{{display:flex;align-items:center;gap:10px;padding:10px 0 8px;cursor:pointer;border-bottom:2px solid {C};margin-bottom:12px}}
.adv-group-header h3{{font-size:14px;font-weight:bold;color:#333;flex:1}}
.adv-count{{background:{C};color:white;font-size:11px;font-weight:bold;padding:2px 8px;border-radius:10px}}
.adv-toggle{{font-size:12px;color:#888}}
.adv-group-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px;margin-bottom:16px}}
/* ── Lightbox */
#lb{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.88);z-index:999;align-items:center;justify-content:center;cursor:zoom-out}}
#lb.open{{display:flex}}
#lb img{{max-width:92vw;max-height:92vh;border-radius:8px}}
/* ── Selection */
.card{{position:relative}}
.card-cb-wrap{{position:absolute;top:8px;left:8px;z-index:10;line-height:0}}
.card-cb{{width:18px;height:18px;cursor:pointer;accent-color:{C}}}
.card.selected{{outline:2px solid {C};outline-offset:-2px;background:#f0f6ff}}
/* ── Bulk action bar */
.sel-bar{{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#1a1a1a;color:white;padding:10px 20px;border-radius:28px;display:flex;align-items:center;gap:10px;box-shadow:0 4px 24px rgba(0,0,0,.4);z-index:200;font-size:13px;white-space:nowrap;transition:opacity .2s}}
.sel-bar.hidden{{display:none}}
.sel-bar-btn{{background:rgba(255,255,255,.15);color:white;border:1px solid rgba(255,255,255,.25);border-radius:16px;padding:5px 14px;cursor:pointer;font-size:12px;font-weight:bold}}
.sel-bar-btn:hover{{background:rgba(255,255,255,.25)}}
.sel-bar-btn.primary{{background:{C};border-color:{C}}}
.sel-bar-btn.primary:hover{{background:#1565c0}}
/* ── Generate Modal */
#gen-modal{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:400;overflow-y:auto;padding:24px}}
#gen-modal.open{{display:flex;align-items:flex-start;justify-content:center}}
.gen-panel{{background:white;border-radius:14px;width:100%;max-width:960px;box-shadow:0 8px 40px rgba(0,0,0,.3);margin:auto}}
.gen-header{{padding:18px 24px;border-bottom:1px solid #eee;display:flex;justify-content:space-between;align-items:center}}
.gen-header h2{{font-size:17px;font-weight:bold}}
.gen-close{{background:none;border:none;font-size:22px;cursor:pointer;color:#888;line-height:1}}
.gen-body{{padding:20px 24px;max-height:75vh;overflow-y:auto}}
.gen-ad-row{{border:1px solid #e0e0e0;border-radius:10px;margin-bottom:16px;overflow:hidden}}
.gen-ad-top{{display:grid;grid-template-columns:100px 1fr;gap:12px;background:#f7f8fa;padding:12px;align-items:start}}
.gen-thumb{{width:100px;height:75px;object-fit:cover;border-radius:6px;background:#111;display:block}}
.gen-ad-info h3{{font-size:13px;font-weight:bold;margin-bottom:3px}}
.gen-ad-info p{{font-size:11px;color:#666;line-height:1.4;max-height:48px;overflow:hidden;margin:0}}
.gen-ad-body{{padding:12px}}
.gen-analysis{{font-size:12px;color:#444;line-height:1.6;background:#f0f6ff;padding:10px 12px;border-radius:6px;margin-bottom:12px;border-left:3px solid {C}}}
.gen-prompts{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}}
.gen-prompt-wrap label{{font-size:11px;font-weight:bold;color:#555;display:block;margin-bottom:4px}}
.gen-prompt-wrap textarea{{width:100%;height:72px;padding:8px;border:1px solid #ddd;border-radius:6px;font-size:11px;resize:vertical;outline:none;font-family:inherit;box-sizing:border-box}}
.gen-prompt-wrap textarea:focus{{border-color:{C}}}
.gen-row-actions{{display:flex;gap:8px;flex-wrap:wrap}}
.gbtn{{padding:6px 14px;border:none;border-radius:7px;cursor:pointer;font-size:12px;font-weight:bold;white-space:nowrap}}
.gbtn:disabled{{opacity:.4;cursor:not-allowed}}
.gbtn.analyze{{background:#f0f2f5;color:#333}}
.gbtn.analyze:hover:not(:disabled){{background:#e4e6e9}}
.gbtn.flux{{background:#6366f1;color:white}}
.gbtn.flux:hover:not(:disabled){{background:#4f46e5}}
.gbtn.hf{{background:#f59e0b;color:white}}
.gbtn.hf:hover:not(:disabled){{background:#d97706}}
.gen-outputs{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px}}
.gen-out-box{{border:1px solid #e0e0e0;border-radius:8px;overflow:hidden}}
.gen-out-label{{font-size:10px;font-weight:bold;color:#555;padding:5px 8px;background:#f0f2f5;border-bottom:1px solid #e0e0e0}}
.gen-out-content{{display:flex;align-items:center;justify-content:center;min-height:120px;background:#fafafa;font-size:12px;color:#aaa;text-align:center;padding:12px}}
.gen-out-content img,.gen-out-content video{{width:100%;display:block}}
/* ── Translation */
.card-translation{{padding:8px 12px;border-top:1px solid #f0f0f0;background:#fffef0;font-size:12px}}
.card-translation .trans-lang{{font-size:10px;color:#888;font-weight:bold;margin-bottom:3px}}
.card-translation .trans-text{{color:#444;line-height:1.5}}
.gen-footer{{padding:14px 24px;border-top:1px solid #eee;display:flex;gap:10px;justify-content:flex-end}}
.gfbtn{{padding:9px 20px;border:none;border-radius:8px;cursor:pointer;font-size:13px;font-weight:bold}}
.gfbtn.blue{{background:{C};color:white}}
.gfbtn.blue:hover{{background:#1565c0}}
.gfbtn.purple{{background:#6366f1;color:white}}
.gfbtn.purple:hover{{background:#4f46e5}}
.gfbtn.grey{{background:#f0f2f5;color:#333}}
.gfbtn.grey:hover{{background:#e4e6e9}}
</style>
</head><body>

<header>
  <div class="header-left">
    <h1>📘 {brand} — Meta Ad Intelligence</h1>
    <a href="/">← New search</a>
  </div>
  <div class="stats">
    <div class="stat"><strong>{len(ads)}</strong><span>total</span></div>
    <div class="stat"><strong>{active_cnt}</strong><span>active</span></div>
    <div class="stat"><strong>{q_media}%</strong><span>has media</span></div>
  </div>
  <button class="export-btn" onclick="exportCSV()">⬇ Export CSV</button>
</header>

<div class="filters">
  <button class="fbtn on" onclick="setFilter('all',this)">All</button>
  <button class="fbtn" onclick="setFilter('ACTIVE',this)">Active</button>
  <button class="fbtn" onclick="setFilter('INACTIVE',this)">Inactive</button>
  <button class="fbtn" onclick="setFilter('VIDEO',this)">📹 Video</button>
  <button class="fbtn" onclick="setFilter('IMAGE',this)">🖼 Image</button>
  <button class="fbtn" id="gbtn" onclick="toggleGroup(this)">⊞ Group</button>
  <button class="fbtn" id="selbtn" onclick="toggleSelectMode(this)">☐ Select</button>
  <input id="srch" class="search-box" placeholder="Search advertiser or copy…" oninput="applyFilters()">
  <select class="sort-sel" id="adv-sel" onchange="setAdvertiser(this.value)">
    <option value="">📄 All pages</option>
  </select>
  <select class="sort-sel" id="period-sel" onchange="setPeriod(this.value)">
    <option value="all">📅 All dates</option>
    <option value="w0">This week</option>
    <option value="w1">Last week</option>
    <option value="d7">Last 7 days</option>
    <option value="d30">Last 30 days</option>
    <option value="m0">This month</option>
    <option value="m1">Last month</option>
  </select>
  <select class="sort-sel" onchange="sortCards(this.value)">
    <option value="">Sort: default</option>
    <option value="date_desc" selected>Active since (newest)</option>
    <option value="date_asc">Active since (oldest)</option>
    <option value="imp_desc">Impressions ↓</option>
    <option value="advertiser">Advertiser A–Z</option>
  </select>
  <div class="view-toggle">
    <button class="vbtn on" id="vcard" onclick="setView('card')">⊞ Cards</button>
    <button class="vbtn" id="vtable" onclick="setView('table')">☰ Table</button>
  </div>
  <span class="fcount" id="fc">{len(ads)} ads</span>
</div>

<div class="grid" id="grid">{cards_html}</div>

<div class="table-wrap" id="twrap">
  <table class="data-table">
    <thead><tr>
      <th>Media</th>
      <th onclick="sortTbl(1)">Advertiser</th>
      <th onclick="sortTbl(2)">Status</th>
      <th onclick="sortTbl(3)">Ad Copy</th>
      <th>CTA</th>
      <th onclick="sortTbl(5)">Landing Page</th>
      <th>Library</th>
      <th></th>
    </tr></thead>
    <tbody id="tbody">{rows_html}</tbody>
  </table>
</div>

<div id="sel-bar" class="sel-bar hidden">
  <span id="sel-count">0 selected</span>
  <button class="sel-bar-btn" onclick="selectAllVisible()">Select All</button>
  <button class="sel-bar-btn" onclick="clearSel()">Clear</button>
  <button class="sel-bar-btn" onclick="bulkCSV()">⬇ CSV</button>
  <button class="sel-bar-btn primary" onclick="bulkZip()">⬇ Download ZIP</button>
  <button class="sel-bar-btn" style="background:#6366f1;border-color:#6366f1" onclick="openGenModal()">🎨 Generate</button>
</div>

<div id="gen-modal">
  <div class="gen-panel">
    <div class="gen-header">
      <h2>🎨 Generate Ad Iterations</h2>
      <button class="gen-close" onclick="closeGenModal()">✕</button>
    </div>
    <div class="gen-body" id="gen-body"></div>
    <div class="gen-footer">
      <button class="gfbtn grey" onclick="closeGenModal()">Close</button>
      <button class="gfbtn blue" onclick="analyzeAll()">🔍 Analyze All</button>
      <button class="gfbtn purple" onclick="generateAll()">⚡ Generate All</button>
    </div>
  </div>
</div>

<div id="lb" onclick="this.classList.remove('open')"><img id="lbi" src=""></div>

<script>
// ── State
let curFilter = 'all', curView = 'card';
let curPeriod = 'all';
let curAdvertiser = '';

// ── Advertiser / page filter
function setAdvertiser(v) {{ curAdvertiser = v; applyFilters(); }}

// Populate the advertiser dropdown — keyed by PAGE ID so same-name pages stay separate
(function populateAdvertisers() {{
  const pages = {{}};  // pageKey -> {{ name, id, count }}
  document.querySelectorAll('.card').forEach(c => {{
    const name = (c.dataset.advertiser || 'Unknown').trim();
    const id   = (c.dataset.pageid || '').trim();
    const key  = id || name;
    if (!pages[key]) pages[key] = {{ name, id, count: 0 }};
    pages[key].count++;
  }});
  // Detect names shared by more than one page ID
  const nameCounts = {{}};
  Object.values(pages).forEach(p => {{ nameCounts[p.name] = (nameCounts[p.name] || 0) + 1; }});
  const sel = document.getElementById('adv-sel');
  Object.entries(pages).sort((a, b) => a[1].name.localeCompare(b[1].name)).forEach(([key, p]) => {{
    const o = document.createElement('option');
    o.value = key;
    // Show ID suffix when the name is shared by multiple accounts
    const suffix = (nameCounts[p.name] > 1 && p.id) ? ` (#${{p.id.slice(-6)}})` : '';
    o.textContent = `${{p.name}}${{suffix}} — ${{p.count}}`;
    sel.appendChild(o);
  }});
}})();
let selectMode = false;
const selected = new Set();
const KEYWORD = "{brand_slug}";  // search term / brand used for this scrape

// ── Period (date) filter
function setPeriod(p) {{ curPeriod = p; applyFilters(); }}

function matchPeriod(el) {{
  if (curPeriod === 'all') return true;
  const ds = el.dataset.date || '';
  const d  = new Date(ds);
  if (isNaN(d)) return false;  // no valid date → hide when filtering by period
  const now = new Date();
  const day = 86400000;

  // Start of current week (Monday)
  const startOfWeek = (ref) => {{
    const x = new Date(ref);
    const wd = (x.getDay() + 6) % 7;  // Mon=0
    x.setHours(0,0,0,0); x.setDate(x.getDate() - wd);
    return x;
  }};

  if (curPeriod === 'd7')  return (now - d) <= 7  * day;
  if (curPeriod === 'd30') return (now - d) <= 30 * day;
  if (curPeriod === 'w0') {{ const s = startOfWeek(now); return d >= s; }}
  if (curPeriod === 'w1') {{ const s = startOfWeek(now); const p = new Date(s - 7*day); return d >= p && d < s; }}
  if (curPeriod === 'm0') return d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth();
  if (curPeriod === 'm1') {{
    const m = new Date(now.getFullYear(), now.getMonth() - 1, 1);
    return d.getFullYear() === m.getFullYear() && d.getMonth() === m.getMonth();
  }}
  return true;
}}

// Default sort: impressions descending
sortCards('date_desc');

// ── View toggle
function setView(v) {{
  curView = v;
  document.getElementById('vcard').classList.toggle('on', v === 'card');
  document.getElementById('vtable').classList.toggle('on', v === 'table');
  document.getElementById('grid').classList.toggle('hidden', v !== 'card');
  document.getElementById('twrap').classList.toggle('active', v === 'table');
  applyFilters();
}}

// ── Filter + search
function applyFilters() {{
  const q = (document.getElementById('srch').value || '').toLowerCase();
  let n = 0;
  const matchAdv = (el) => !curAdvertiser || ((el.dataset.pageid || el.dataset.advertiser || '') === curAdvertiser);
  if (curView === 'card') {{
    document.querySelectorAll('.card').forEach(c => {{
      const show = matchF(c) && matchPeriod(c) && matchAdv(c) && (!q || [c.dataset.advertiser, c.dataset.body, c.dataset.title].some(s => (s||'').toLowerCase().includes(q)));
      c.style.display = show ? '' : 'none';
      if (show) n++;
    }});
  }} else {{
    document.querySelectorAll('#tbody tr').forEach(r => {{
      const show = matchF(r) && matchPeriod(r) && matchAdv(r) && (!q || r.textContent.toLowerCase().includes(q));
      r.style.display = show ? '' : 'none';
      if (show) n++;
    }});
  }}
  document.getElementById('fc').textContent = n + ' ads';
}}

function matchF(el) {{
  const f = curFilter;
  return f === 'all'
    || (f === 'ACTIVE'   && el.dataset.status === 'ACTIVE')
    || (f === 'INACTIVE' && el.dataset.status === 'INACTIVE')
    || (f === 'VIDEO'    && el.dataset.fmt === 'VIDEO')
    || (f === 'IMAGE'    && el.dataset.fmt === 'IMAGE');
}}

function setFilter(f, btn) {{
  document.querySelectorAll('.fbtn').forEach(b => b.classList.remove('on'));
  btn.classList.add('on');
  curFilter = f;
  applyFilters();
}}

// ── Sort cards
function sortCards(by) {{
  if (!by) return;
  const grid  = document.getElementById('grid');
  const cards = [...grid.querySelectorAll('.card')];
  cards.sort((a, b) => {{
    if (by === 'advertiser') return (a.dataset.advertiser||'').localeCompare(b.dataset.advertiser||'');
    if (by === 'date_desc')  return (b.dataset.date||'').localeCompare(a.dataset.date||'');
    if (by === 'date_asc')   return (a.dataset.date||'').localeCompare(b.dataset.date||'');
    if (by === 'imp_desc')   return parseInt(b.dataset.imp||-1) - parseInt(a.dataset.imp||-1);
    return 0;
  }});
  cards.forEach(c => grid.appendChild(c));
}}

// ── Sort table
let tCol = -1, tAsc = true;
function sortTbl(col) {{
  const tbody = document.getElementById('tbody');
  const rows  = [...tbody.querySelectorAll('tr')];
  tAsc = (tCol === col) ? !tAsc : true;
  tCol = col;
  rows.sort((a, b) => {{
    const av = a.cells[col]?.textContent.trim() || '';
    const bv = b.cells[col]?.textContent.trim() || '';
    return tAsc ? av.localeCompare(bv) : bv.localeCompare(av);
  }});
  rows.forEach(r => tbody.appendChild(r));
  document.querySelectorAll('.data-table th').forEach((th, i) => {{
    th.classList.remove('asc', 'desc');
    if (i === col) th.classList.add(tAsc ? 'asc' : 'desc');
  }});
}}

// ── Group by advertiser
let grouped = false;
function toggleGroup(btn) {{
  grouped = !grouped;
  btn.classList.toggle('on', grouped);
  btn.textContent = grouped ? '⊟ Ungroup' : '⊞ Group';
  const grid  = document.getElementById('grid');
  const cards = [...grid.querySelectorAll('.card')];
  if (grouped) {{
    const map = {{}};
    cards.forEach(c => {{ const a = c.dataset.advertiser || 'Unknown'; (map[a] = map[a] || []).push(c); }});
    const sorted = Object.entries(map).sort((a, b) => b[1].length - a[1].length);
    grid.innerHTML = ''; grid.style.display = 'block';
    sorted.forEach(([name, cs]) => {{
      const g = document.createElement('div'); g.className = 'adv-group';
      g.innerHTML = `<div class="adv-group-header" onclick="const d=this.nextSibling;d.style.display=d.style.display==='none'?'grid':'none';this.querySelector('.adv-toggle').textContent=d.style.display==='none'?'▶ show':'▼ hide'"><h3>${{name}}</h3><span class="adv-count">${{cs.length}} ad${{cs.length>1?'s':''}}</span><span class="adv-toggle">▼ hide</span></div><div class="adv-group-grid"></div>`;
      cs.forEach(c => g.querySelector('.adv-group-grid').appendChild(c));
      grid.appendChild(g);
    }});
    document.getElementById('fc').textContent = sorted.length + ' advertisers';
  }} else {{
    const cs = [...grid.querySelectorAll('.card')];
    grid.innerHTML = ''; grid.style.display = '';
    cs.forEach(c => grid.appendChild(c));
    document.getElementById('fc').textContent = cs.length + ' ads';
  }}
}}

// ── Read more toggle
document.querySelectorAll('.ad-copy').forEach(el => {{
  if (el.scrollHeight > el.clientHeight + 5) {{
    const t = document.createElement('span');
    t.className = 'toggle-copy'; t.textContent = 'Read more ▼';
    t.onclick = () => {{ el.classList.toggle('open'); t.textContent = el.classList.contains('open') ? 'Show less ▲' : 'Read more ▼'; }};
    el.after(t);
  }}
}});

// ── Export CSV
function exportCSV() {{
  const rows = [['Advertiser','Status','Format','Date','Title','Body','CTA','Landing Page','Ad Library URL']];
  document.querySelectorAll('.card').forEach(c => {{
    if (c.style.display === 'none') return;
    rows.push([c.dataset.advertiser||'', c.dataset.status||'', c.dataset.fmt||'', c.dataset.date||'', c.dataset.title||'', c.dataset.body||'', c.dataset.cta||'', c.dataset.lp||'', c.dataset.lib||'']);
  }});
  const csv = rows.map(r => r.map(v => '"' + String(v).replace(/"/g, '""') + '"').join(',')).join('\\n');
  const a = document.createElement('a');
  a.href = 'data:text/csv;charset=utf-8,' + encodeURIComponent(csv);
  a.download = '{brand_slug}_ads.csv'; a.click();
}}

// ── Copy to clipboard
function copyText(btn) {{
  const text = btn.dataset.text || '';
  navigator.clipboard.writeText(text).then(() => {{
    const orig = btn.textContent; btn.textContent = '✓ Copied!';
    setTimeout(() => btn.textContent = orig, 1500);
  }}).catch(() => {{
    const ta = document.createElement('textarea'); ta.value = text;
    document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove();
    btn.textContent = '✓ Copied!'; setTimeout(() => btn.textContent = '📋 Copy', 1500);
  }});
}}

// ── Selection
function toggleSelectMode(btn) {{
  selectMode = !selectMode;
  btn.classList.toggle('on', selectMode);
  btn.textContent = selectMode ? '✓ Select' : '☐ Select';
  document.querySelectorAll('.card-cb-wrap').forEach(w => w.style.display = selectMode ? '' : 'none');
  if (!selectMode) clearSel();
}}

function toggleSelect(cb) {{
  const card = cb.closest('.card');
  if (cb.checked) {{ selected.add(card); card.classList.add('selected'); }}
  else            {{ selected.delete(card); card.classList.remove('selected'); }}
  updateSelBar();
}}

function updateSelBar() {{
  const bar = document.getElementById('sel-bar');
  document.getElementById('sel-count').textContent = selected.size + ' selected';
  bar.classList.toggle('hidden', selected.size === 0);
}}

function selectAllVisible() {{
  document.querySelectorAll('.card').forEach(c => {{
    if (c.style.display === 'none') return;
    c.classList.add('selected');
    const cb = c.querySelector('.card-cb');
    if (cb) cb.checked = true;
    selected.add(c);
  }});
  updateSelBar();
}}

function clearSel() {{
  selected.forEach(c => {{ c.classList.remove('selected'); const cb = c.querySelector('.card-cb'); if (cb) cb.checked = false; }});
  selected.clear();
  updateSelBar();
}}

// ── Bulk CSV export
function bulkCSV() {{
  const rows = [['Advertiser','Status','Format','Date','Title','Body','CTA','Landing Page','Ad Library URL']];
  selected.forEach(c => {{
    rows.push([c.dataset.advertiser||'', c.dataset.status||'', c.dataset.fmt||'', c.dataset.date||'', c.dataset.title||'', c.dataset.body||'', c.dataset.cta||'', c.dataset.lp||'', c.dataset.lib||'']);
  }});
  const csv = rows.map(r => r.map(v => '"' + String(v).replace(/"/g, '""') + '"').join(',')).join('\\n');
  const a = document.createElement('a');
  a.href = 'data:text/csv;charset=utf-8,' + encodeURIComponent(csv);
  a.download = 'selected_ads.csv'; a.click();
}}

// ── Bulk ZIP download — one subfolder per ad with all files
async function bulkZip() {{
  if (!selected.size) return;
  const btn = document.querySelector('.sel-bar-btn.primary');
  btn.textContent = '⏳ Zipping…';
  btn.disabled = true;

  const zip = new JSZip();
  let idx = 0;

  for (const card of selected) {{
    idx++;
    const adv    = (card.dataset.advertiser || 'ad').replace(/[^a-z0-9]/gi, '_').slice(0, 30);
    const date   = (card.dataset.date || 'nodate').replace(/[^0-9-]/g, '') || 'nodate';
    const type   = (card.dataset.fmt === 'VIDEO') ? 'VIDEO' : 'STATIC';
    const kw     = (KEYWORD || 'ad').replace(/[^a-z0-9]/gi, '_').slice(0, 30);
    // Unique per-ad suffix (ad ID, else running index) so folders never collide/overwrite
    const adId   = (card.dataset.lib || '').match(/id=([0-9]+)/)?.[1] || ('n' + idx);
    const folder = zip.folder(`${{date}} - ${{kw}} - ${{type}} - ${{adv}} - ${{adId}}`);

    const imgs = (card.dataset.imgs || '').split(',').filter(Boolean);
    const vids = (card.dataset.vids || '').split(',').filter(Boolean);

    // ── Images
    for (let i = 0; i < imgs.length; i++) {{
      try {{
        const r    = await fetch(imgs[i]);
        const blob = await r.blob();
        const ext  = blob.type.includes('png') ? 'png' : 'jpg';
        folder.file(`image${{i+1}}.${{ext}}`, blob);
      }} catch(e) {{}}
    }}

    // ── Videos: fetch via server proxy (no CORS)
    if (vids.length) {{
      for (let i = 0; i < vids.length; i++) {{
        try {{
          const r    = await fetch(vids[i]);
          const blob = await r.blob();
          folder.file(`video${{i+1}}.mp4`, blob);
        }} catch(e) {{
          folder.file(`video${{i+1}}_url.txt`, vids[i]);
        }}
      }}
    }}

    // ── Card screenshot (whole card as PNG)
    try {{
      const canvas = await html2canvas(card, {{ useCORS: true, allowTaint: true, scale: 2 }});
      const blob   = await new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
      folder.file('card_screenshot.png', blob);
    }} catch(e) {{}}

    // ── Ad copy companion file
    const title   = card.dataset.title   || '';
    const body    = card.dataset.body    || '';
    const cta     = card.dataset.cta     || '';
    const lp      = card.dataset.lp      || '';
    const libUrl  = card.dataset.lib     || '';
    const status  = card.dataset.status  || '';
    const fmt     = card.dataset.fmt     || '';

    const copyText = [
      `ADVERTISER: ${{card.dataset.advertiser || ''}}`,
      `STATUS: ${{status}}  |  FORMAT: ${{fmt}}  |  DATE: ${{date}}`,
      ``,
      title ? `HEADLINE:\\n${{title}}` : '',
      ``,
      `AD COPY:\\n${{body}}`,
      ``,
      cta     ? `CTA: ${{cta}}`             : '',
      lp      ? `LANDING PAGE: ${{lp}}`     : '',
      libUrl  ? `AD LIBRARY: ${{libUrl}}`   : '',
    ].filter(l => l !== null).join('\\n').trim();

    folder.file('ad_copy.txt', copyText);
  }}

  const content = await zip.generateAsync({{ type: 'blob' }});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(content);
  a.download = 'ads_download.zip'; a.click();

  btn.textContent = '⬇ Download ZIP';
  btn.disabled = false;
}}

// ── Lightbox
function openFull(s) {{ document.getElementById('lbi').src = s; document.getElementById('lb').classList.add('open'); }}

// ── Screenshot card
function shotCard(btn) {{
  const card = btn.closest('.card');
  html2canvas(card, {{useCORS: true, allowTaint: true, scale: 2}}).then(canvas => {{
    const a = document.createElement('a'); a.href = canvas.toDataURL('image/png');
    a.download = 'ad-screenshot.png'; a.click();
  }}).catch(() => alert('Screenshot failed — right-click the image and save directly.'));
}}

// ── Generate Modal
function openGenModal() {{
  if (!selected.size) return;
  const body = document.getElementById('gen-body');
  body.innerHTML = '';
  let idx = 0;
  for (const card of selected) {{
    idx++;
    const id       = (card.dataset.lib || '').match(/id=([0-9]+)/)?.[1] || idx;
    const adv      = card.dataset.advertiser || 'Unknown';
    const fmt      = card.dataset.fmt || 'IMAGE';
    const bodyTxt  = (card.dataset.body  || '').slice(0, 150);
    const title    = (card.dataset.title || '');
    const imgs     = (card.dataset.imgs  || '').split(',').filter(Boolean);
    const origImgs = (card.dataset.origImgs || '').split(',').filter(Boolean);
    const origVids = (card.dataset.origVids || '').split(',').filter(Boolean);
    const thumb    = imgs[0] || '';
    const safeAdv  = adv.replace(/"/g,"'");
    const safeTitle = title.replace(/"/g,"'");
    const safeBody  = bodyTxt.replace(/"/g,"'");

    const row = document.createElement('div');
    row.className = 'gen-ad-row';
    row.id = `gen-row-${{id}}`;
    row.innerHTML = `
      <div class="gen-ad-top">
        ${{thumb ? `<img class="gen-thumb" src="${{thumb}}">` : '<div class="gen-thumb"></div>'}}
        <div class="gen-ad-info">
          <h3>${{adv}} <span style="font-weight:normal;color:#888">— ${{fmt}}</span></h3>
          ${{title ? `<p><strong>${{title}}</strong></p>` : ''}}
          <p>${{bodyTxt}}${{bodyTxt.length >= 150 ? '…' : ''}}</p>
        </div>
      </div>
      <div class="gen-ad-body">
        <div class="gen-analysis" id="analysis-${{id}}" style="display:none"></div>
        <div class="gen-prompts" id="prompts-${{id}}" style="display:none">
          <div class="gen-prompt-wrap">
            <label>🖼 Flux Prompt (static image)</label>
            <textarea id="flux-prompt-${{id}}" placeholder="Click Analyze to generate…"></textarea>
          </div>
          <div class="gen-prompt-wrap">
            <label>🎬 Higgsfield Prompt (animation)</label>
            <textarea id="hf-prompt-${{id}}" placeholder="Click Analyze to generate…"></textarea>
          </div>
        </div>
        <div class="gen-row-actions" style="margin-top:8px">
          <button class="gbtn analyze"
            id="analyze-btn-${{id}}"
            data-id="${{id}}"
            data-adv="${{safeAdv}}"
            data-fmt="${{fmt}}"
            data-title="${{safeTitle}}"
            data-body="${{safeBody}}"
            data-orig-imgs="${{origImgs.join(',')}}"
            data-orig-vids="${{origVids.join(',')}}"
            onclick="analyzeAd('${{id}}', this)">🔍 Analyze</button>
          <button class="gbtn flux" id="flux-btn-${{id}}" onclick="generateImage('${{id}}', this)" disabled>🖼 Generate Image</button>
          <button class="gbtn hf"   id="hf-btn-${{id}}"   onclick="generateVideo('${{id}}', this)"  disabled>🎬 Animate</button>
          <button class="gbtn" style="background:#0ea5e9;color:white"
            data-adv="${{safeAdv}}" data-title="${{safeTitle}}" data-body="${{safeBody}}"
            data-orig-imgs="${{origImgs.join(',')}}" data-orig-vids="${{origVids.join(',')}}"
            onclick="generateFullAd('${{id}}', this)">🎞 Generate Full Ad (5 beats)</button>
        </div>
        <div class="gen-outputs" id="outputs-${{id}}" style="display:none">
          <div class="gen-out-box">
            <div class="gen-out-label">🖼 Flux — Generated Image</div>
            <div class="gen-out-content" id="flux-out-${{id}}">Not yet generated</div>
          </div>
          <div class="gen-out-box">
            <div class="gen-out-label">🎬 Higgsfield — Animated Video</div>
            <div class="gen-out-content" id="hf-out-${{id}}">Generate image first</div>
          </div>
        </div>
        <div id="beats-${{id}}" style="display:none;margin-top:14px"></div>
      </div>`;
    body.appendChild(row);
  }}
  document.getElementById('gen-modal').classList.add('open');
}}

function closeGenModal() {{
  document.getElementById('gen-modal').classList.remove('open');
}}

async function analyzeAd(id, btn) {{
  btn.textContent = '⏳ Analyzing…';
  btn.disabled    = true;
  const payload = {{
    ad_id:     id,
    advertiser: btn.dataset.adv,
    format:    btn.dataset.fmt,
    title:     btn.dataset.title,
    body:      btn.dataset.body,
    orig_imgs: (btn.dataset.origImgs || '').split(',').filter(Boolean),
    orig_vids: (btn.dataset.origVids || '').split(',').filter(Boolean),
  }};
  try {{
    const r    = await fetch('/analyze', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(payload) }});
    const data = await r.json();
    const el   = document.getElementById(`analysis-${{id}}`);
    el.innerHTML = `${{data.transcript ? `<strong>📝 Transcript (competitor's actual script):</strong><br><span style="white-space:pre-wrap">${{data.transcript}}</span><br><br>` : ''}}
                    ${{data.scene_breakdown ? `<strong>🎬 What Gemini saw:</strong> ${{data.scene_breakdown}}<br><br>` : ''}}
                    <strong>Visual Style:</strong> ${{data.visual_style}}<br>
                    <strong>Hook Type:</strong> ${{data.hook_type}}<br>
                    <strong>Tone:</strong> ${{data.tone}}
                    ${{data.note ? `<br><em style="color:#888;font-size:11px">${{data.note}}</em>` : ''}}`;
    el.style.display = 'block';
    document.getElementById(`flux-prompt-${{id}}`).value = data.flux_prompt;
    document.getElementById(`hf-prompt-${{id}}`).value   = data.higgsfield_prompt;
    document.getElementById(`prompts-${{id}}`).style.display  = 'grid';
    document.getElementById(`outputs-${{id}}`).style.display  = 'grid';
    document.getElementById(`flux-btn-${{id}}`).disabled = false;
    btn.textContent = '✓ Analyzed';
  }} catch(e) {{
    btn.textContent = '❌ Error — retry';
    btn.disabled = false;
  }}
}}

async function generateImage(id, btn) {{
  const prompt = document.getElementById(`flux-prompt-${{id}}`).value;
  const out    = document.getElementById(`flux-out-${{id}}`);
  btn.textContent = '⏳ Generating…';
  btn.disabled    = true;
  out.innerHTML   = '⏳ Calling Higgsfield…';
  const showImage = (url) => {{
    out.innerHTML = `<img src="${{url}}" alt="Generated">`;
    const hfBtn = document.getElementById(`hf-btn-${{id}}`);
    hfBtn.disabled = false;
    hfBtn.dataset.imgUrl = url;
    btn.textContent = '🖼 Regenerate';
    btn.disabled = false;
  }};
  try {{
    const r    = await fetch('/generate/image', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{ ad_id:id, prompt }}) }});
    const data = await r.json();
    if (data.image_url) {{ showImage(data.image_url); return; }}
    if (data.status === 'error' || !data.request_id) {{
      out.textContent = data.message || 'No image returned';
      btn.textContent = '🖼 Generate Image'; btn.disabled = false; return;
    }}
    // Poll for the image
    const rid = data.request_id;
    out.textContent = '🖼 Rendering image…';
    let tries = 0;
    const poll = async () => {{
      tries++;
      try {{
        const sr = await fetch('/generate/image/status', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{ request_id: rid }}) }});
        const sd = await sr.json();
        if (sd.image_url) {{ showImage(sd.image_url); return; }}
        if (['failed','nsfw','error'].includes(sd.status)) {{ out.textContent = '❌ ' + (sd.message || sd.status); btn.textContent='🖼 Generate Image'; btn.disabled=false; return; }}
        if (tries > 60) {{ out.textContent = '⚠️ Timed out'; btn.textContent='🖼 Generate Image'; btn.disabled=false; return; }}
        setTimeout(poll, 4000);
      }} catch(e) {{ if (tries > 60) {{ out.textContent='❌ Polling error'; btn.textContent='🖼 Generate Image'; btn.disabled=false; return; }} setTimeout(poll, 4000); }}
    }};
    poll();
  }} catch(e) {{
    out.textContent = '❌ Error';
    btn.textContent = '🖼 Generate Image';
    btn.disabled    = false;
  }}
}}

async function generateVideo(id, btn) {{
  const prompt  = document.getElementById(`hf-prompt-${{id}}`).value;
  const imgUrl  = btn.dataset.imgUrl || '';
  const out     = document.getElementById(`hf-out-${{id}}`);
  btn.textContent = '⏳ Submitting…';
  btn.disabled    = true;
  out.textContent = '⏳ Submitting to Higgsfield…';
  try {{
    const r    = await fetch('/generate/video', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{ ad_id:id, prompt, image_url:imgUrl }}) }});
    const data = await r.json();
    if (data.status === 'error' || !data.request_id) {{
      out.textContent = data.message || 'Submit failed';
      btn.textContent = '🎬 Animate'; btn.disabled = false;
      return;
    }}
    // Poll for completion (video gen takes 30s–a few minutes)
    const rid = data.request_id;
    out.textContent = '🎬 Generating video… (this can take a minute or two)';
    btn.textContent = '⏳ Rendering…';
    let tries = 0;
    const poll = async () => {{
      tries++;
      try {{
        const sr = await fetch('/generate/video/status', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{ request_id: rid }}) }});
        const sd = await sr.json();
        if (sd.status === 'completed' && sd.video_url) {{
          out.innerHTML = `<video src="${{sd.video_url}}" controls style="width:100%"></video>`;
          btn.textContent = '🎬 Reanimate'; btn.disabled = false;
          return;
        }}
        if (sd.status === 'failed' || sd.status === 'nsfw' || sd.status === 'error') {{
          out.textContent = '❌ ' + (sd.message || sd.status);
          btn.textContent = '🎬 Animate'; btn.disabled = false;
          return;
        }}
        if (tries > 144) {{ out.textContent = '⚠️ Timed out — try again'; btn.textContent = '🎬 Animate'; btn.disabled = false; return; }}
        setTimeout(poll, 5000);  // poll every 5s, up to ~5 min
      }} catch(e) {{
        if (tries > 60) {{ out.textContent = '❌ Polling error'; btn.textContent = '🎬 Animate'; btn.disabled = false; return; }}
        setTimeout(poll, 5000);
      }}
    }};
    poll();
  }} catch(e) {{
    out.textContent = '❌ Error';
    btn.textContent = '🎬 Animate';
    btn.disabled    = false;
  }}
}}

// ── Full multi-beat ad generation
function falImage(prompt) {{
  // Submit to Higgsfield image, then poll until the URL is ready.
  return new Promise(async (resolve) => {{
    try {{
      const r = await fetch('/generate/image', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{ prompt }}) }});
      const d = await r.json();
      if (d.image_url) return resolve({{ url: d.image_url, message: '' }});
      if (!d.request_id) return resolve({{ url: null, message: d.message || 'failed' }});
      let tries = 0;
      const poll = async () => {{
        tries++;
        try {{
          const sr = await fetch('/generate/image/status', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{ request_id: d.request_id }}) }});
          const sd = await sr.json();
          if (sd.image_url) return resolve({{ url: sd.image_url, message: '' }});
          if (['failed','nsfw','error'].includes(sd.status)) return resolve({{ url: null, message: sd.message || sd.status }});
          if (tries > 60) return resolve({{ url: null, message: 'timed out' }});
          setTimeout(poll, 4000);
        }} catch(e) {{ if (tries > 60) return resolve({{ url: null, message: 'poll error' }}); setTimeout(poll, 4000); }}
      }};
      poll();
    }} catch(e) {{ resolve({{ url: null, message: String(e) }}); }}
  }});
}}

async function genVoice(text) {{
  try {{
    const r = await fetch('/generate/voice', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{ text }}) }});
    const d = await r.json();
    return d.audio_url || null;
  }} catch(e) {{ return null; }}
}}

function higgsAnimate(prompt, imageUrl) {{
  return new Promise(async (resolve) => {{
    try {{
      const r = await fetch('/generate/video', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{ prompt, image_url:imageUrl }}) }});
      const d = await r.json();
      if (!d.request_id) return resolve(null);
      let tries = 0;
      const poll = async () => {{
        tries++;
        try {{
          const sr = await fetch('/generate/video/status', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{ request_id:d.request_id }}) }});
          const sd = await sr.json();
          if (sd.status === 'completed' && sd.video_url) return resolve(sd.video_url);
          if (['failed','nsfw','error'].includes(sd.status)) return resolve(null);
          if (tries > 144) return resolve(null);
          setTimeout(poll, 5000);
        }} catch(e) {{ if (tries > 60) return resolve(null); setTimeout(poll, 5000); }}
      }};
      poll();
    }} catch(e) {{ resolve(null); }}
  }});
}}

async function generateFullAd(id, btn) {{
  btn.textContent = '⏳ Scripting…';
  btn.disabled    = true;
  const box = document.getElementById(`beats-${{id}}`);
  box.style.display = 'block';
  box.innerHTML = '<div style="font-size:13px;color:#666;padding:8px">🎬 Gemini is breaking this ad into 5 beats…</div>';

  // 1. Get the multi-beat script
  let data;
  try {{
    const r = await fetch('/analyze/beats', {{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{
        advertiser: btn.dataset.adv, title: btn.dataset.title, body: btn.dataset.body,
        orig_imgs: (btn.dataset.origImgs||'').split(',').filter(Boolean),
        orig_vids: (btn.dataset.origVids||'').split(',').filter(Boolean),
      }})
    }});
    data = await r.json();
  }} catch(e) {{ data = {{ error: String(e) }}; }}

  if (data.error || !data.beats) {{
    box.innerHTML = `<div style="color:#c00;font-size:13px;padding:8px">⚠️ ${{data.error || 'No beats returned'}}</div>`;
    btn.textContent = '🎞 Generate Full Ad (5 beats)'; btn.disabled = false;
    return;
  }}

  // 2. Render a card per beat
  box.innerHTML = `
    ${{data.transcript ? `<div style="font-size:12px;color:#555;margin-bottom:8px;background:#fffef0;padding:8px 10px;border-radius:6px"><strong>📝 Competitor transcript (reference):</strong><br><span style="white-space:pre-wrap">${{data.transcript}}</span></div>` : ''}}
    <div style="font-size:12px;color:#555;margin-bottom:8px"><strong>👤 Character (locked across beats):</strong> ${{data.character_description || '—'}}</div>`;
  data.beats.forEach((b, i) => {{
    const card = document.createElement('div');
    card.className = 'gen-ad-row';
    card.style.marginBottom = '10px';
    card.innerHTML = `
      <div style="background:#f0f6ff;padding:8px 12px;font-weight:bold;font-size:13px">Beat ${{i+1}} — ${{b.beat}}</div>
      <div style="padding:10px 12px">
        <div style="font-size:12px;color:#444;margin-bottom:8px"><em>"${{b.script_line || ''}}"</em></div>
        <div class="gen-outputs" style="display:grid">
          <div class="gen-out-box"><div class="gen-out-label">🖼 Still</div><div class="gen-out-content" id="beat-img-${{id}}-${{i}}">⏳ queued</div></div>
          <div class="gen-out-box"><div class="gen-out-label">🎬 Clip</div><div class="gen-out-content" id="beat-vid-${{id}}-${{i}}">⏳ waiting for still</div></div>
        </div>
        <div class="gen-out-box" style="margin-top:8px"><div class="gen-out-label">🔊 Voiceover</div><div class="gen-out-content" id="beat-vox-${{id}}-${{i}}" style="min-height:0;padding:8px">⏳ waiting</div></div>
      </div>`;
    box.appendChild(card);
  }});

  // 3. Generate each beat sequentially (still → clip), keeps API load sane
  btn.textContent = '⏳ Rendering beats…';
  for (let i = 0; i < data.beats.length; i++) {{
    const b       = data.beats[i];
    const imgCell = document.getElementById(`beat-img-${{id}}-${{i}}`);
    const vidCell = document.getElementById(`beat-vid-${{id}}-${{i}}`);

    imgCell.textContent = '⏳ generating still…';
    const imgRes = await falImage(b.flux_prompt);
    if (!imgRes.url) {{ imgCell.innerHTML = `<span style="color:#c00;font-size:11px">❌ ${{imgRes.message || 'still failed'}}</span>`; vidCell.textContent = '— skipped'; continue; }}
    const imgUrl = imgRes.url;
    imgCell.innerHTML = `<img src="${{imgUrl}}" style="width:100%">`;

    vidCell.textContent = '🎬 animating…';
    const vidUrl = await higgsAnimate(b.higgsfield_prompt, imgUrl);
    if (!vidUrl) {{ vidCell.textContent = '❌ clip failed'; }}
    else {{ vidCell.innerHTML = `<video src="${{vidUrl}}" controls style="width:100%"></video>`; }}

    // Voiceover from the beat's script line (one consistent voice)
    const voxCell = document.getElementById(`beat-vox-${{id}}-${{i}}`);
    if (b.script_line) {{
      voxCell.textContent = '🔊 generating voice…';
      const vox = await genVoice(b.script_line);
      if (vox) voxCell.innerHTML = `<audio controls src="${{vox}}" style="width:100%"></audio>`;
      else     voxCell.textContent = '❌ voice failed';
    }} else {{
      voxCell.textContent = '— no line';
    }}
  }}

  btn.textContent = '✓ Full Ad Generated';
}}

function analyzeAll() {{
  document.querySelectorAll('.gbtn.analyze:not([disabled])').forEach(btn => btn.click());
}}

async function generateAll() {{
  const btns = [...document.querySelectorAll('.gbtn.analyze')];
  for (const btn of btns) {{
    if (!btn.textContent.includes('✓')) {{ btn.click(); await new Promise(r => setTimeout(r, 500)); }}
  }}
  await new Promise(r => setTimeout(r, btns.length * 1500 + 2000));
  document.querySelectorAll('.gbtn.flux:not([disabled])').forEach(btn => btn.click());
}}

// ── Download video
function dlVideo(btn) {{
  const url = btn.dataset.src;
  fetch(url).then(r => r.blob()).then(blob => {{
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
    a.download = 'ad-video.mp4'; a.click();
  }}).catch(() => window.open(url, '_blank'));
}}
</script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/jszip/3.10.1/jszip.min.js"></script>
<script>
// Hide checkboxes until Select mode is on
document.querySelectorAll('.card-cb-wrap').forEach(w => w.style.display = 'none');
</script>
</body></html>"""


# ── HTML templates ────────────────────────────────────────────────────────────

HOME_HTML = """<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>Meta Ad Intelligence Scraper</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:Arial,sans-serif;background:#f0f2f5;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}
.card{background:white;border-radius:14px;padding:36px 40px;width:560px;box-shadow:0 4px 20px rgba(0,0,0,.1)}
.logo{display:flex;align-items:center;gap:10px;margin-bottom:4px}
.logo-icon{width:36px;height:36px;background:#1877f2;border-radius:8px;display:flex;align-items:center;justify-content:center;color:white;font-size:20px}
h1{font-size:22px;color:#1a1a1a}
p.sub{font-size:13px;color:#888;margin:6px 0 24px}
label{display:block;font-size:12px;font-weight:bold;color:#555;margin-bottom:5px;margin-top:16px}
input,select{width:100%;padding:9px 12px;border:1px solid #ddd;border-radius:7px;font-size:14px;outline:none;color:#1a1a1a}
input:focus,select:focus{border-color:#1877f2;box-shadow:0 0 0 3px rgba(24,119,242,.1)}
.hint{font-size:11px;color:#aaa;margin-top:4px}
.divider{margin-top:20px;padding-top:16px;border-top:1px solid #f0f0f0}
.search-row{display:flex;gap:6px;margin-bottom:6px}
.search-row input{flex:1}
.remove-btn{background:none;border:1px solid #ddd;color:#999;border-radius:6px;padding:0 9px;cursor:pointer;font-size:14px;flex-shrink:0;line-height:1}
.remove-btn:hover{border-color:#f66;color:#c00}
.add-btn{background:none;border:1px dashed #bbb;color:#888;border-radius:7px;padding:7px;width:100%;cursor:pointer;font-size:13px;margin-top:4px}
.add-btn:hover{border-color:#1877f2;color:#1877f2}
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.submit-btn{background:#1877f2;color:white;border:none;border-radius:8px;padding:13px;width:100%;font-size:15px;font-weight:bold;cursor:pointer;margin-top:28px}
.submit-btn:hover{background:#1565c0}
</style></head><body>
<div class="card">
  <div class="logo">
    <div class="logo-icon">📘</div>
    <h1>Meta Ad Intelligence</h1>
  </div>
  <p class="sub">Scrape Facebook & Instagram ads via the Meta Ad Library</p>

  <form method="POST" action="/start">
    <div class="two-col">
      <div>
        <label>Country</label>
        <select name="country">COUNTRY_OPTIONS</select>
      </div>
      <div>
        <label>Ad Status</label>
        <select name="ad_status">
          <option value="active">Active Only</option>
          <option value="all">All (active + inactive)</option>
        </select>
      </div>
    </div>

    <label style="margin-top:16px">Ads per page <span style="font-weight:normal;color:#aaa">(discovery mode — see more advertisers, fewer ads each)</span></label>
    <select name="per_page">
      <option value="0">All ads per page</option>
      <option value="3">Max 3 per page (discover)</option>
      <option value="5">Max 5 per page</option>
      <option value="10">Max 10 per page</option>
    </select>

    <div class="divider">
      <label>Keywords / Search Terms <span style="font-weight:normal;color:#aaa">(one search per line — paste as many as you want)</span></label>
      <textarea name="keywords_bulk" placeholder="weight loss&#10;fat burner&#10;glp1&#10;semaglutide"
        style="width:100%;height:90px;padding:9px 12px;border:1px solid #ddd;border-radius:7px;font-size:14px;resize:vertical;outline:none;color:#1a1a1a"></textarea>
    </div>

    <div class="divider">
      <label>Landing Pages / Domains <span style="font-weight:normal;color:#aaa">(optional — one per line — finds advertisers driving traffic to each)</span></label>
      <textarea name="domains_bulk" placeholder="get-novaburn.com&#10;trimrx.com&#10;quad.medvi.org"
        style="width:100%;height:70px;padding:9px 12px;border:1px solid #ddd;border-radius:7px;font-size:14px;resize:vertical;outline:none;color:#1a1a1a"></textarea>

      <label style="margin-top:14px">Competitor Facebook Pages <span style="font-weight:normal;color:#aaa">(optional — one URL or page ID per line)</span></label>
      <textarea name="pages_bulk" placeholder="153085624560230&#10;https://www.facebook.com/ads/library/?...view_all_page_id=123..."
        style="width:100%;height:70px;padding:9px 12px;border:1px solid #ddd;border-radius:7px;font-size:14px;resize:vertical;outline:none;color:#1a1a1a"></textarea>
    </div>

    <div class="divider">
      <details>
        <summary style="cursor:pointer;font-size:12px;font-weight:bold;color:#555;list-style:none;display:flex;align-items:center;gap:6px">
          <span>🔐</span> Authenticated scraping <span style="font-weight:normal;color:#aaa">(optional — unlocks login-restricted creatives)</span>
        </summary>
        <div style="margin-top:10px">
          <p style="font-size:11px;color:#888;line-height:1.5;margin-bottom:8px">
            Install <a href="https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm" target="_blank" style="color:#1877f2">Cookie-Editor</a> in Chrome → log into Facebook → click the extension → <strong>Export → Export as JSON</strong> → paste below.<br>
            When provided, scraping goes directly to Meta's API (no Apify cost) and can see all ad creatives.
          </p>
          <textarea name="cookies" id="cookies-box" placeholder='[{"name":"datr","value":"..."},{"name":"c_user","value":"..."},...]'
            style="width:100%;height:72px;padding:8px 10px;border:1px solid #ddd;border-radius:7px;font-size:11px;font-family:monospace;resize:vertical;outline:none;color:#444"></textarea>
          <div style="display:flex;align-items:center;gap:8px;margin-top:8px">
            <button type="button" id="lock-btn" onclick="lockCookies()"
              style="background:#f0f2f5;border:1px solid #ddd;border-radius:7px;padding:6px 14px;cursor:pointer;font-size:12px;font-weight:bold;color:#333">🔒 Lock cookies</button>
            <span id="cookie-status" style="font-size:11px;color:#888"></span>
          </div>
          <p style="font-size:10px;color:#aaa;margin-top:6px;line-height:1.4">
            Locking saves your cookies on the server so you don't re-paste them each scrape. Cleared on redeploy.
          </p>
        </div>
      </details>
    </div>

    <button type="submit" class="submit-btn">🔍 Run Scrape</button>
  </form>
</div>
<script>
function addRow() {
  const d = document.getElementById('searches');
  const r = document.createElement('div');
  r.className = 'search-row';
  r.innerHTML = '<input name="search[]" placeholder="Keywords…"><button type="button" class="remove-btn" onclick="removeRow(this)" title="Remove">&#x2715;</button>';
  d.appendChild(r);
  r.querySelector('input').focus();
}
function removeRow(b) {
  if (document.querySelectorAll('.search-row').length > 1) b.parentElement.remove();
}

// ── Cookie lock/unlock
function renderCookieStatus(locked, count) {
  const status = document.getElementById('cookie-status');
  const btn    = document.getElementById('lock-btn');
  const box    = document.getElementById('cookies-box');
  if (locked) {
    status.innerHTML = '🟢 <strong>' + count + ' cookies locked</strong> — reused every scrape';
    btn.textContent  = '🔓 Unlock';
    btn.onclick      = unlockCookies;
    box.placeholder  = 'Cookies locked — leave blank to reuse, or paste new ones to replace.';
  } else {
    status.textContent = '⚪ Not locked';
    btn.textContent    = '🔒 Lock cookies';
    btn.onclick        = lockCookies;
  }
}
async function lockCookies() {
  const raw = document.getElementById('cookies-box').value.trim();
  if (!raw) { alert('Paste your cookies JSON first, then click Lock.'); return; }
  const btn = document.getElementById('lock-btn');
  btn.textContent = '⏳ Locking…';
  try {
    const r = await fetch('/cookies/lock', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ cookies: raw })
    });
    const d = await r.json();
    if (d.ok) { document.getElementById('cookies-box').value = ''; renderCookieStatus(true, d.count); }
    else      { alert('Lock failed: ' + (d.error || 'unknown')); renderCookieStatus(false, 0); }
  } catch(e) { alert('Lock failed: ' + e); renderCookieStatus(false, 0); }
}
async function unlockCookies() {
  await fetch('/cookies/unlock', { method: 'POST' });
  renderCookieStatus(false, 0);
}
// Check lock state on page load
fetch('/cookies/status').then(r => r.json()).then(d => renderCookieStatus(d.locked, d.count)).catch(() => {});
</script>
</body></html>"""

PROGRESS_HTML = """<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>Scraping…</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:Arial,sans-serif;background:#f0f2f5;min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:white;border-radius:14px;padding:36px 40px;width:520px;box-shadow:0 4px 20px rgba(0,0,0,.1)}
h2{font-size:18px;margin-bottom:6px;display:flex;align-items:center;gap:10px}
p.sub{font-size:13px;color:#666;margin-bottom:20px}
#log{background:#0d1117;color:#7ee787;font-family:monospace;font-size:12px;padding:16px;border-radius:8px;height:260px;overflow-y:auto;line-height:1.6}
.spinner{width:22px;height:22px;border:3px solid #e0e0e0;border-top-color:#1877f2;border-radius:50%;animation:spin .8s linear infinite;flex-shrink:0}
@keyframes spin{to{transform:rotate(360deg)}}
</style></head><body>
<div class="card">
  <h2><div class="spinner"></div>Scraping Meta ads for <strong>{{ brand }}</strong></h2>
  <p class="sub">This takes 30–90 seconds — stay on this page.</p>
  <div id="log"></div>
  <a id="view-btn" href="/result/{{ job_id }}" style="display:none;margin-top:20px;background:#1877f2;color:white;text-decoration:none;border-radius:8px;padding:13px 24px;font-size:15px;font-weight:bold;text-align:center;display:none;width:100%;box-sizing:border-box">View Ads →</a>
</div>
<script>
const jobId = "{{ job_id }}";
const logEl = document.getElementById('log');
let seen = 0;
function poll() {
  fetch('/status/' + jobId)
    .then(r => r.json())
    .then(d => {
      d.log.slice(seen).forEach(line => {
        const el = document.createElement('div');
        el.textContent = line;
        logEl.appendChild(el);
      });
      seen = d.log.length;
      logEl.scrollTop = logEl.scrollHeight;
      if (d.status === 'done') {
        document.querySelector('.spinner').style.display = 'none';
        document.querySelector('h2').innerHTML = '✅ Scrape complete!';
        document.getElementById('view-btn').style.display = 'block';
      } else if (d.status === 'error') {
        document.querySelector('.spinner').style.display = 'none';
        logEl.innerHTML += '<div style="color:#f85149">❌ Error — check log above</div>';
      } else setTimeout(poll, 2000);
    })
    .catch(() => setTimeout(poll, 3000));
}
poll();
</script>
</body></html>"""


# ── Scrape job launch (shared by POST /start and POST /api/v1/scrapes) ───────

def prepare_scrape(kw_lines, domain_lines, page_urls, country, ad_status, per_page, cookies_raw=""):
    """Turn raw scrape inputs into run_job arguments.

    Both the browser form and the JSON API go through here and then
    launch_scrape_job(), so they hand identical arguments to the same run_job.
    """
    # Bulk keywords — one search per line (comma within a line = OR group)
    searches = [[q.strip() for q in ln.split(",") if q.strip()] for ln in kw_lines]

    # Bulk domains — clean each to a bare hostname
    domains = []
    for d in domain_lines:
        raw = d if "://" in d else "https://" + d
        host = urlparse(raw).netloc or d
        if host:
            domains.append(host)

    first_kw = kw_lines[0] if kw_lines else ""
    brand    = first_kw or (domains[0] if domains else "") or (page_urls[0] if page_urls else "") or "Meta Ads"

    # If only domains/pages provided (no keywords), still fire one thread
    if not searches:
        searches = [[]]

    # Parse optional Facebook session cookies (Cookie-Editor JSON export).
    # If none were supplied, fall back to locked cookies (if any).
    cookies = None
    cookies_raw = (cookies_raw or "").strip()
    if cookies_raw:
        try:
            cookies = json.loads(cookies_raw)
            if not isinstance(cookies, list):
                cookies = None
        except Exception:
            cookies = None
    if cookies is None:
        locked = get_locked_cookies()
        if locked:
            cookies = locked

    return {
        "brand":     brand,
        "country":   country,
        "searches":  searches,
        "domains":   domains,
        "page_urls": page_urls,
        "ad_status": ad_status,
        "cookies":   cookies,
        "per_page":  per_page,
    }


def launch_scrape_job(params):
    """Register a job and start run_job on a background thread; returns job_id.

    Cookies go to the worker thread only -- they are never stored on the job
    record, which is what /status and the JSON API read from. The retained
    `params` keep only page refs that resolve to a page ID, so junk pasted
    into the pages box (e.g. cookie JSON lines) is never echoed back.
    """
    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "status": "running",
        "log":    [],
        "html":   None,
        "params": {
            "country":   params["country"],
            "ad_status": params["ad_status"],
            "per_page":  params["per_page"],
            "searches":  [list(s) for s in params["searches"] if s],
            "domains":   list(params["domains"]),
            "page_urls": [p for p in params["page_urls"]
                          if fb_page_to_adlib_url(p, params["ad_status"], params["country"])],
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    global last_job_id
    last_job_id = job_id

    threading.Thread(
        target=run_job,
        args=(job_id, params["brand"], params["country"], params["searches"],
              params["domains"], params["page_urls"], params["ad_status"]),
        kwargs={"cookies": params["cookies"], "per_page": params["per_page"]},
        daemon=True
    ).start()
    return job_id


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def home():
    return HOME_HTML.replace("COUNTRY_OPTIONS", COUNTRY_OPTIONS)

@app.route("/start", methods=["POST"])
def start():
    country   = request.form.get("country", "US")
    ad_status = request.form.get("ad_status", "active")
    try:    per_page = int(request.form.get("per_page", "0"))
    except: per_page = 0

    def split_lines(field):
        return [ln.strip() for ln in request.form.get(field, "").splitlines() if ln.strip()]

    # Bulk keywords / domains / Facebook pages (URLs or bare IDs), one per line.
    # If the cookies textarea is empty, prepare_scrape falls back to locked cookies.
    params = prepare_scrape(
        kw_lines     = split_lines("keywords_bulk"),
        domain_lines = split_lines("domains_bulk"),
        page_urls    = split_lines("pages_bulk"),
        country      = country,
        ad_status    = ad_status,
        per_page     = per_page,
        cookies_raw  = request.form.get("cookies", ""),
    )
    job_id = launch_scrape_job(params)

    return render_template_string(PROGRESS_HTML, job_id=job_id, brand=params["brand"])

@app.route("/status/<job_id>")
def status(job_id):
    job = jobs.get(job_id, {})
    return jsonify({
        "status": job.get("status", "unknown"),
        "log": job.get("log", []),
        "auth_state": job.get("auth_state"),
    })

@app.route("/cookies/status")
def cookies_status():
    """Report whether cookies are currently locked."""
    locked = get_locked_cookies()
    n = len(locked) if locked else 0
    return jsonify({"locked": bool(locked), "count": n})

@app.route("/cookies/lock", methods=["POST"])
def cookies_lock():
    """Save cookies to disk so they're reused on every scrape (survives across workers)."""
    raw = (request.json or {}).get("cookies", "").strip()
    if not raw:
        return jsonify({"ok": False, "error": "No cookies provided"}), 400
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, list) or not parsed:
            return jsonify({"ok": False, "error": "Cookies must be a non-empty JSON array"}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"Invalid JSON: {e}"}), 400
    set_locked_cookies(parsed)
    return jsonify({"ok": True, "count": len(parsed)})

@app.route("/cookies/unlock", methods=["POST"])
def cookies_unlock():
    """Clear locked cookies."""
    clear_locked_cookies()
    return jsonify({"ok": True})

# ── Safe media fetch boundary (fbcdn only) ──────────────────────────────────
# The ONLY path by which server-side code fetches ad media: /img, /vid and
# _fetch_media (Gemini analysis) all go through safe_fetch_media(). Every URL,
# including every redirect target, must pass validate_media_url(); anything
# malformed or ambiguous is rejected rather than "cleaned up".

MEDIA_ALLOWED_DOMAIN  = "fbcdn.net"
MEDIA_MAX_REDIRECTS   = 3
MEDIA_IMAGE_MAX_BYTES = 15 * 1024 * 1024
MEDIA_VIDEO_MAX_BYTES = 150 * 1024 * 1024
MEDIA_TIMEOUTS        = {"image": 15, "video": 60, "media": 60}
MEDIA_ALLOWED_TYPES   = {"image": ("image/",), "video": ("video/",), "media": ("image/", "video/")}
MEDIA_BLOCKED_MIMES   = frozenset({"image/svg+xml"})  # scriptable; never serve it from our origin
MEDIA_REDIRECT_CODES  = frozenset({301, 302, 303, 307, 308})
_MEDIA_NETLOC_RE      = re.compile(r"[A-Za-z0-9.-]+(?::443)?")
_MEDIA_LABEL_RE       = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_MEDIA_MIME_RE        = re.compile(r"(?:image|video)/[a-z0-9][a-z0-9.+-]*")
_MEDIA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer":    "https://www.facebook.com/",
}

class MediaFetchError(Exception):
    """A media URL or upstream response failed the safe-fetch policy."""

def validate_media_url(url):
    """Return `url` unchanged if it is an allowed fbcdn HTTPS URL, else raise
    MediaFetchError. The query string is preserved byte-for-byte so signed
    URLs (oh=/oe= params) keep working."""
    if not isinstance(url, str) or not url:
        raise MediaFetchError("empty media URL")
    # Control chars, whitespace, backslashes and non-ASCII are all ways to make
    # different URL parsers disagree about the host — refuse them outright.
    if any(ord(c) <= 0x20 or ord(c) >= 0x7f or c == "\\" for c in url):
        raise MediaFetchError("media URL contains disallowed characters")
    if url[:8].lower() != "https://":
        raise MediaFetchError("media URL must use https")
    try:
        parts = urlsplit(url)
    except ValueError:
        raise MediaFetchError("malformed media URL")
    if parts.scheme != "https":
        raise MediaFetchError("media URL must use https")
    # Allows only host[:443] — rejects userinfo (@), other ports, empty port,
    # IPv6 brackets and anything else unexpected in the authority.
    if not _MEDIA_NETLOC_RE.fullmatch(parts.netloc):
        raise MediaFetchError("media URL authority not allowed")
    host = parts.netloc.split(":", 1)[0].lower()
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise MediaFetchError("IP literal media hosts are not allowed")
    labels = host.split(".")
    if not all(_MEDIA_LABEL_RE.fullmatch(label) for label in labels):
        raise MediaFetchError("malformed media host")
    if host != MEDIA_ALLOWED_DOMAIN and not host.endswith("." + MEDIA_ALLOWED_DOMAIN):
        raise MediaFetchError("media host not allowed")
    return url

def _media_open(url, timeout):
    """Issue ONE GET for an already-validated URL. The opener has only HTTPS
    (plus env proxy) handlers — no file/ftp/data/http handlers and no redirect
    or error processors — so 3xx responses are returned to safe_fetch_media
    for revalidation instead of being followed automatically."""
    opener = urllib.request.OpenerDirector()
    for handler in (urllib.request.ProxyHandler(), urllib.request.HTTPSHandler(),
                    urllib.request.UnknownHandler()):
        opener.add_handler(handler)
    return opener.open(urllib.request.Request(url, headers=_MEDIA_HEADERS), timeout=timeout)

def _read_bounded(resp, limit):
    """Read the body, failing as soon as it exceeds `limit` bytes."""
    chunks, total = [], 0
    while True:
        chunk = resp.read(min(64 * 1024, limit + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise MediaFetchError("media body exceeds size limit")
        chunks.append(chunk)
    return b"".join(chunks)

def safe_fetch_media(url, kind):
    """Fetch fbcdn media under the safe-fetch policy. `kind` is "image",
    "video" or "media" (either). Returns (bytes, mime). Raises
    MediaFetchError on any policy violation; network errors propagate."""
    allowed = MEDIA_ALLOWED_TYPES[kind]
    current = validate_media_url(url)
    for hop in range(MEDIA_MAX_REDIRECTS + 1):
        resp = _media_open(current, MEDIA_TIMEOUTS[kind])
        try:
            status = resp.status
            if status in MEDIA_REDIRECT_CODES:
                location = resp.headers.get("Location")
                if not location:
                    raise MediaFetchError("redirect without Location")
                if hop >= MEDIA_MAX_REDIRECTS:
                    raise MediaFetchError("too many media redirects")
                current = validate_media_url(urljoin(current, location))
                continue
            if status != 200:
                raise MediaFetchError(f"upstream media status {status}")
            mime = (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if (not _MEDIA_MIME_RE.fullmatch(mime) or not mime.startswith(allowed)
                    or mime in MEDIA_BLOCKED_MIMES):
                raise MediaFetchError("media Content-Type not allowed")
            limit = MEDIA_IMAGE_MAX_BYTES if mime.startswith("image/") else MEDIA_VIDEO_MAX_BYTES
            length = resp.headers.get("Content-Length")
            if length is not None:
                length = length.strip()
                if not re.fullmatch(r"[0-9]{1,15}", length):
                    raise MediaFetchError("malformed Content-Length")
                if int(length) > limit:
                    raise MediaFetchError("media Content-Length exceeds size limit")
            return _read_bounded(resp, limit), mime
        finally:
            resp.close()
    raise MediaFetchError("too many media redirects")

def _media_proxy_response(data, mimetype, status=200):
    """Proxy response with nosniff and private (never shared-cache) caching."""
    resp = app.response_class(data, status=status, mimetype=mimetype)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "private, max-age=3600" if status == 200 else "no-store"
    return resp

def _proxy_media_route(kind):
    url = request.args.get("u", "")
    try:
        validate_media_url(url)
    except MediaFetchError:
        return _media_proxy_response(b"", "text/plain", 400)
    try:
        data, mime = safe_fetch_media(url, kind)
    except Exception:
        return _media_proxy_response(b"", "text/plain", 502)
    return _media_proxy_response(data, mime)

@app.route("/img")
def proxy_img():
    """Server-side proxy for Facebook CDN images.
    Facebook signed URLs (oh= hash) are tied to the requester's session/IP.
    Fetching server-side avoids browser-level auth failures.
    """
    return _proxy_media_route("image")

@app.route("/vid")
def proxy_vid():
    """Server-side proxy for Facebook CDN videos — bypasses browser CORS."""
    return _proxy_media_route("video")

# ── Gemini creative analysis ──────────────────────────────────────────────

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview")
GEMINI_BASE  = "https://generativelanguage.googleapis.com/v1beta"

# Keeps generated prompts clear of AI content-moderation filters (Higgsfield/fal)
# while preserving the persuasive hook energy. The suggestive punch lives in the
# COPY and landing page, not the generated visual.
FILTER_SAFE_RULES = (
    "\n==== FILTER-SAFE VISUAL RULES (critical for AI generation) ====\n"
    "The image/video generators reject sexual, explicit, or overtly suggestive content. "
    "Keep every flux_prompt and higgsfield_prompt strictly WHOLESOME and BRAND-SAFE:\n"
    "- Frame benefits as vitality, energy, confidence, wellness, healthy lifestyle — never sexual acts, arousal, or explicit anatomy.\n"
    "- Wardrobe: everyday casual/modest clothing. No lingerie, nudity, swimwear, or revealing/suggestive poses.\n"
    "- Setting: normal home/kitchen/gym/outdoor lifestyle. Subject simply speaking to camera, smiling, holding the product.\n"
    "- No sexual innuendo in the VISUAL description. The persuasion/edge belongs in the ad COPY and landing page, not the rendered footage.\n"
    "- If the source ad is sexual/explicit, KEEP THE SAME creative FORMAT and energy (surreal graphic, bold product hero, comparison, etc.) but make it clean — remove nudity, bikinis, and suggestive posing. Do NOT switch a graphic/product ad into a generic person testimonial just to sanitize it.\n"
    "This is how compliant advertisers pass generation filters: same bold format, clean visual, punchy copy.\n"
    "==============================================================\n\n"
)

def _fetch_media(url, kind="media"):
    """Download an image/video from Facebook CDN via safe_fetch_media.
    Returns (bytes, content_type) or (None, None)."""
    try:
        return safe_fetch_media(url, kind)
    except Exception as e:
        print(f"[GEMINI] media fetch failed: {e}")
        return None, None

def _gemini_upload_file(data_bytes, mime, api_key):
    """Upload a media file to Gemini File API (needed for video). Returns file_uri or None."""
    try:
        # Start resumable upload
        start = urllib.request.Request(
            f"{GEMINI_BASE.replace('/v1beta','')}/upload/v1beta/files?key={api_key}",
            data=json.dumps({"file": {"display_name": "ad_media"}}).encode(),
            headers={
                "X-Goog-Upload-Protocol":       "resumable",
                "X-Goog-Upload-Command":        "start",
                "X-Goog-Upload-Header-Content-Length": str(len(data_bytes)),
                "X-Goog-Upload-Header-Content-Type":   mime,
                "Content-Type":                 "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(start, timeout=30) as r:
            upload_url = r.headers.get("X-Goog-Upload-URL")
        if not upload_url:
            return None
        # Upload the bytes and finalize
        up = urllib.request.Request(
            upload_url, data=data_bytes,
            headers={
                "Content-Length":        str(len(data_bytes)),
                "X-Goog-Upload-Offset":  "0",
                "X-Goog-Upload-Command": "upload, finalize",
            },
            method="POST",
        )
        with urllib.request.urlopen(up, timeout=120) as r:
            info = json.loads(r.read())
        file_uri  = info["file"]["uri"]
        file_name = info["file"]["name"]
        # Poll until the file finishes processing (videos need this)
        for _ in range(20):
            chk = urllib.request.Request(f"{GEMINI_BASE}/{file_name}?key={api_key}")
            with urllib.request.urlopen(chk, timeout=30) as r:
                st = json.loads(r.read())
            if st.get("state") == "ACTIVE":
                return file_uri
            if st.get("state") == "FAILED":
                return None
            time.sleep(3)
        return file_uri
    except Exception as e:
        print(f"[GEMINI] upload failed: {e}")
        return None

def gemini_analyze(adv, title, body, img_urls, vid_urls):
    """Analyze the ad creative (video or image) with Gemini. Returns dict or None."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return None

    parts = []
    media_kind = None

    # Prefer the video if present, else the first image
    if vid_urls:
        raw, ct = _fetch_media(vid_urls[0], "video")
        if raw:
            mime = ct if ct.startswith("video/") else "video/mp4"
            file_uri = _gemini_upload_file(raw, mime, api_key)
            if file_uri:
                parts.append({"file_data": {"mime_type": mime, "file_uri": file_uri}})
                media_kind = "video"
    if media_kind is None and img_urls:
        raw, ct = _fetch_media(img_urls[0], "image")
        if raw:
            mime = ct if ct.startswith("image/") else "image/jpeg"
            b64  = __import__("base64").b64encode(raw).decode()
            parts.append({"inline_data": {"mime_type": mime, "data": b64}})
            media_kind = "image"

    is_video = media_kind == "video"
    instruction = (
        f"You are an elite direct-response creative strategist and prompt engineer for "
        f"Big Wave Media, studying a competitor's {media_kind or 'creative'} ad for the brand \"{adv}\".\n\n"
        f"Ad headline: {title}\nAd copy: {body}\n\n"
        + (
            "You have been given the ACTUAL VIDEO FILE. Watch it start to finish before answering. "
            "Pay attention to: the exact opening frame, how the scene changes over time, the "
            "presenter's appearance/wardrobe/age, the setting and props, camera angles and movement, "
            "lighting, color grade, any on-screen text or captions, and the pacing.\n\n"
            if is_video else
            "You have been given the ACTUAL IMAGE. Study every detail: subject, wardrobe, props, "
            "setting, composition, lighting, color grade, and any on-screen text.\n\n"
        )
        + (
            "==== BIG WAVE MEDIA HOUSE PROMPT RULES (follow exactly) ====\n"
            "STEP 1 — REPLICATE AS CLOSELY AS POSSIBLE. Recreate this exact ad as faithfully as you can: match the "
            "FORMAT, composition/layout, subject and pose, props, color palette, color grade, lighting style, mood, "
            "and where any bold text sits. The goal is a near-twin in the same style. Change ONLY what's required to "
            "(a) keep it original — no copied brand logos or verbatim claims, and (b) keep it filter-safe — no nudity, "
            "bikinis, or explicit/suggestive content. Do NOT default to a UGC talking-head. If the ad is a surreal "
            "graphic product ad, your recreation must ALSO be a surreal graphic product ad with the same composition and energy.\n"
            "STEP 2 — Build the flux_prompt from these SEVEN elements in order, describing the ACTUAL subject of "
            "THIS ad (which may be a person, a product, or a surreal scene — not necessarily a person):\n"
            "  1. SUBJECT — the main subject exactly as this ad presents it (person / product / scene), specific\n"
            "  2. MATERIALS / TEXTURES — fabric, surface, finish (matte, glossy, brushed metal, condensation, etc.)\n"
            "  3. COMPOSITION / FRAMING — camera angle, distance, crop; use a named lens + f-stop ONLY if it's a photographic ad\n"
            "  4. LIGHTING — source, quality, direction, and color temperature (or the graphic lighting/glow if illustrated)\n"
            "  5. STYLE — the aesthetic anchor of THIS ad (photoreal UGC, 3D render, surreal illustration, etc.)\n"
            "  6. BACKGROUND — setting, color, depth\n"
            "  7. RESOLUTION / FORMAT — vertical 9:16, high-res quality tag\n"
            "SPECIFICITY: name exact finishes, colors, and (for photographic ads) lenses/color temps — NEVER vague terms like 'nice lighting'.\n"
            "REALISM (photographic ads only): include a deliberate imperfection (film grain, authentic skin texture, slight asymmetry).\n"
            "TEXT OVERLAYS: if the ad relies on bold on-image text, note where text would go as a placeholder region, but do NOT invent brand logos.\n"
            "The higgsfield_prompt is a MOTION LAYER matched to the format: for UGC, natural talking/blinking + handheld drift; for a product/graphic ad, product glints, light flares, floating elements, subtle text shimmer, slow push-in. Specify WHAT MOVES, WHAT STAYS STILL, CAMERA BEHAVIOR, and ATMOSPHERE.\n"
            "============================================================\n\n"
        )
        + "Respond ONLY with valid JSON in this EXACT shape:\n"
        "{\n"
        '  "transcript": "'
        + ("the competitor's ACTUAL spoken words transcribed verbatim from the audio, plus any on-screen text/captions in [brackets]. This is for research study only. If there is no speech, describe the on-screen text."
           if is_video else
           "any on-screen text/copy visible in the image, transcribed verbatim")
        + '",\n'
        '  "scene_breakdown": "'
        + ("a shot-by-shot description of what actually happens across the video timeline (opening / middle / end), including any scene or camera changes — this proves you watched it"
           if is_video else
           "a detailed description of exactly what is shown in the image")
        + '",\n'
        '  "visual_style": "2-3 sentences: format (UGC/studio/lifestyle), framing, setting, wardrobe, lighting, color grade, and production quality",\n'
        '  "hook_type": "1-2 sentences describing the opening hook and persuasion angle",\n'
        '  "tone": "a few descriptive words",\n'
        '  "flux_prompt": "A single richly detailed text-to-image prompt (90-130 words) that REPLICATES this ad as closely as possible — same format, composition/layout, subject and pose, props, color palette/grade, lighting, and mood — built from the 7 house elements IN ORDER. Match the original tightly; only deviate to stay original (no copied logos/verbatim claims) and filter-safe (no nudity/explicit). For photographic ads include a named lens + color temp and one imperfection; for graphic/illustrated ads describe the render style and text-overlay regions. End with the 9:16 format + quality tag.",\n'
        '  "higgsfield_prompt": "A still-to-video motion prompt (50-80 words) following the house motion-layer rules: what moves, what stays still, camera behavior, motion duration/quality, and atmosphere — believable front-camera UGC phone footage with natural blinking."\n'
        "}\n\n"
        + FILTER_SAFE_RULES +
        "COMPETITIVE RESEARCH ONLY: describe the STYLE so a NEW, original ad can be produced. "
        "Do NOT reproduce exact wording, medical/health claims, logos, or the creative verbatim."
    )
    parts.append({"text": instruction})

    try:
        payload = json.dumps({
            "contents": [{"parts": parts}],
            "generationConfig": {"temperature": 0.4, "response_mime_type": "application/json"},
        }).encode()
        req = urllib.request.Request(
            f"{GEMINI_BASE}/models/{GEMINI_MODEL}:generateContent?key={api_key}",
            data=payload, headers={"Content-Type": "application/json"}, method="POST",
        )
        # Retry on 429 (rate limit) / 503 (overloaded) with backoff
        resp = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    resp = json.loads(r.read())
                break
            except urllib.error.HTTPError as he:
                if he.code in (429, 503) and attempt < 3:
                    time.sleep(5 * (attempt + 1))  # 5s, 10s, 15s
                    continue
                raise
        text = resp["candidates"][0]["content"]["parts"][0]["text"].strip()
        # Strip code fences if present
        text = re.sub(r"^```(?:json)?|```$", "", text.strip()).strip()
        parsed = json.loads(text)
        parsed["media_kind"] = media_kind
        return parsed
    except urllib.error.HTTPError as e:
        try:    err = e.read().decode()[:200]
        except Exception: err = ""
        print(f"[GEMINI] HTTP {e.code}: {err}")
        return {"error": f"HTTP {e.code}: {err}"}
    except Exception as e:
        print(f"[GEMINI] error: {e}")
        return {"error": str(e)}


def gemini_analyze_beats(adv, title, body, img_urls, vid_urls):
    """Break the ad into a multi-beat UGC script with a locked character,
    each beat carrying its own Flux + Higgsfield prompt. Returns dict or {error}."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return {"error": "GEMINI_API_KEY not set"}

    parts = []
    media_kind = None
    if vid_urls:
        raw, ct = _fetch_media(vid_urls[0], "video")
        if raw:
            mime = ct if ct.startswith("video/") else "video/mp4"
            file_uri = _gemini_upload_file(raw, mime, api_key)
            if file_uri:
                parts.append({"file_data": {"mime_type": mime, "file_uri": file_uri}})
                media_kind = "video"
    if media_kind is None and img_urls:
        raw, ct = _fetch_media(img_urls[0], "image")
        if raw:
            mime = ct if ct.startswith("image/") else "image/jpeg"
            b64  = __import__("base64").b64encode(raw).decode()
            parts.append({"inline_data": {"mime_type": mime, "data": b64}})
            media_kind = "image"

    instruction = (
        f"You are a UGC ad director for Big Wave Media, reverse-engineering a competitor's "
        f"{media_kind or 'creative'} ad for \"{adv}\" into a fresh, original 30-second UGC ad script.\n\n"
        f"Ad headline: {title}\nAd copy: {body}\n\n"
        "FIRST, transcribe the competitor's actual spoken words (and on-screen captions) verbatim for study. "
        "THEN, using that transcript's hook, structure, and pacing as reference, design a NEW original ad "
        "broken into 5 beats: HOOK, PROBLEM, DISCOVERY, PROOF, RESULT+CTA (about 5-6 seconds each). "
        "The new script_lines must be freshly reworded — same persuasive beats, NOT the competitor's exact words or claims.\n\n"
        "CRITICAL — CHARACTER CONSISTENCY: invent ONE everyday-person spokesperson and describe them in "
        "vivid, fixed detail (age, ethnicity, hair, face shape, distinctive features, wardrobe). This EXACT "
        "same description must be embedded verbatim at the start of every beat's flux_prompt so the person "
        "looks the same in every clip.\n\n"
        "BIG WAVE HOUSE RULES for every flux_prompt: build from the 7 elements in order — Subject (the locked "
        "character + their action this beat), Materials/Textures, Composition/Framing (named lens + f-stop, "
        "front-facing phone-camera framing), Lighting (direction + color temp), Style, Background, then 9:16 "
        "format + quality tag. Include a deliberate imperfection (natural skin texture, slight asymmetry). "
        "No brand names, logos, or on-image text. Each higgsfield_prompt is a still-to-video motion layer: what "
        "moves, what stays still, camera behavior, motion quality, atmosphere — believable front-camera UGC with "
        "natural blinking and talking.\n\n"
        "Respond ONLY with valid JSON in this EXACT shape:\n"
        "{\n"
        '  "transcript": "the competitor\'s actual spoken words + on-screen captions, transcribed verbatim (research reference only)",\n'
        '  "character_description": "the locked spokesperson description reused across all beats",\n'
        '  "beats": [\n'
        '    {"beat": "HOOK", "script_line": "the spoken line for this beat (original, no medical claims)", '
        '"flux_prompt": "full 7-part still prompt with the locked character baked in", '
        '"higgsfield_prompt": "still-to-video motion prompt"},\n'
        '    {"beat": "PROBLEM", ...},\n'
        '    {"beat": "DISCOVERY", ...},\n'
        '    {"beat": "PROOF", ...},\n'
        '    {"beat": "RESULT_CTA", ...}\n'
        "  ]\n"
        "}\n\n"
        + FILTER_SAFE_RULES +
        "COMPETITIVE RESEARCH ONLY: original script and visuals in the same winning STYLE. "
        "Do NOT copy exact wording, medical/health claims, logos, or the creative verbatim."
    )
    parts.append({"text": instruction})

    try:
        payload = json.dumps({
            "contents": [{"parts": parts}],
            "generationConfig": {"temperature": 0.5, "response_mime_type": "application/json"},
        }).encode()
        req = urllib.request.Request(
            f"{GEMINI_BASE}/models/{GEMINI_MODEL}:generateContent?key={api_key}",
            data=payload, headers={"Content-Type": "application/json"}, method="POST",
        )
        resp = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    resp = json.loads(r.read())
                break
            except urllib.error.HTTPError as he:
                if he.code in (429, 503) and attempt < 3:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise
        text = resp["candidates"][0]["content"]["parts"][0]["text"].strip()
        text = re.sub(r"^```(?:json)?|```$", "", text.strip()).strip()
        parsed = json.loads(text)
        parsed["media_kind"] = media_kind
        return parsed
    except urllib.error.HTTPError as e:
        try:    err = e.read().decode()[:200]
        except Exception: err = ""
        print(f"[GEMINI BEATS] HTTP {e.code}: {err}")
        return {"error": f"HTTP {e.code}: {err}"}
    except Exception as e:
        print(f"[GEMINI BEATS] error: {e}")
        return {"error": str(e)}


@app.route("/analyze", methods=["POST"])
def analyze_ad():
    """Analyze ad creative with Gemini and generate Flux + Higgsfield prompts."""
    data  = request.json or {}
    adv   = data.get("advertiser", "the brand")
    fmt   = data.get("format", "IMAGE")
    title = data.get("title", "")
    body  = data.get("body", "")
    imgs  = data.get("orig_imgs", [])
    vids  = data.get("orig_vids", [])

    print(f"[ANALYZE] {adv} | {fmt} | imgs={len(imgs)} vids={len(vids)}")

    # No key → fall back to a generic (placeholder) analysis
    if not os.environ.get("GEMINI_API_KEY"):
        return jsonify({
            "ad_id":             data.get("ad_id"),
            "visual_style":      "UGC talking head — single speaker, casual authentic setting",
            "hook_type":         "Problem-aware hook — opens with pain point before solution",
            "tone":              "Conversational, trust-building, authoritative",
            "flux_prompt":       f"High-quality commercial lifestyle photography, authentic UGC aesthetic, natural lighting, photorealistic, 4K",
            "higgsfield_prompt": "Smooth cinematic camera movement, subject speaking to camera, warm lighting, slow zoom in, shallow depth of field",
            "note":              "Placeholder — set GEMINI_API_KEY for real creative analysis",
        })

    result = gemini_analyze(adv, title, body, imgs, vids)
    if result and result.get("error"):
        return jsonify({
            "ad_id": data.get("ad_id"),
            "visual_style": "—", "hook_type": "—", "tone": "—",
            "flux_prompt": "", "higgsfield_prompt": "",
            "note": f"⚠️ Gemini error: {result['error']}",
        })
    if not result:
        return jsonify({
            "ad_id": data.get("ad_id"),
            "visual_style": "—", "hook_type": "—", "tone": "—",
            "flux_prompt": "", "higgsfield_prompt": "",
            "note": "⚠️ Could not fetch creative media to analyze",
        })

    return jsonify({
        "ad_id":             data.get("ad_id"),
        "transcript":        result.get("transcript", ""),
        "scene_breakdown":   result.get("scene_breakdown", ""),
        "visual_style":      result.get("visual_style", ""),
        "hook_type":         result.get("hook_type", ""),
        "tone":              result.get("tone", ""),
        "flux_prompt":       result.get("flux_prompt", ""),
        "higgsfield_prompt": result.get("higgsfield_prompt", ""),
        "note":              f"Analyzed {result.get('media_kind','creative')} with Gemini",
    })


@app.route("/analyze/beats", methods=["POST"])
def analyze_beats():
    """Break an ad into a multi-beat UGC script with a locked character."""
    data  = request.json or {}
    adv   = data.get("advertiser", "the brand")
    title = data.get("title", "")
    body  = data.get("body", "")
    imgs  = data.get("orig_imgs", [])
    vids  = data.get("orig_vids", [])

    if not os.environ.get("GEMINI_API_KEY"):
        return jsonify({"error": "Set GEMINI_API_KEY to generate multi-beat scripts"}), 400

    result = gemini_analyze_beats(adv, title, body, imgs, vids)
    if result.get("error"):
        return jsonify({"error": result["error"]}), 502
    beats = result.get("beats") or []
    if not beats:
        return jsonify({"error": "No beats returned"}), 502
    return jsonify({
        "transcript": result.get("transcript", ""),
        "character_description": result.get("character_description", ""),
        "beats": beats,
    })


_HF_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# request_id is caller-supplied and goes into an authenticated upstream URL
# path, so it must be validated locally before any request is built.
HIGGSFIELD_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")

def valid_higgsfield_request_id(rid):
    return isinstance(rid, str) and HIGGSFIELD_REQUEST_ID_RE.fullmatch(rid) is not None

# Higgsfield text-to-image model — overridable via env var
HIGGSFIELD_IMAGE_MODEL = os.environ.get("HIGGSFIELD_IMAGE_MODEL", "higgsfield-ai/soul/standard")

def _hf_extract_image(resp):
    """Pull an image URL out of a Higgsfield status/result response (several shapes)."""
    imgs = resp.get("images")
    if isinstance(imgs, list) and imgs:
        first = imgs[0]
        return first.get("url") if isinstance(first, dict) else first
    out = resp.get("output")
    if isinstance(out, dict):
        return out.get("url") or out.get("image_url")
    return None

@app.route("/generate/image", methods=["POST"])
def generate_image():
    """Submit a 9:16 image job to Higgsfield (Soul). Returns request_id for polling."""
    data   = request.json or {}
    prompt = data.get("prompt", "")
    auth = _higgsfield_auth()
    if not auth:
        return jsonify({"status": "error",
                        "message": "Set HIGGSFIELD_API_KEY + HIGGSFIELD_API_SECRET in Railway env vars"}), 400
    if not prompt.strip():
        return jsonify({"status": "error", "message": "Empty prompt"}), 400
    try:
        payload = json.dumps({
            "prompt":       prompt,
            "aspect_ratio": "9:16",
            "resolution":   "1080p",
        }).encode()
        req = urllib.request.Request(
            f"https://platform.higgsfield.ai/{HIGGSFIELD_IMAGE_MODEL}",
            data=payload,
            headers={"Authorization": auth, "Content-Type": "application/json",
                     "Accept": "application/json", "User-Agent": _HF_UA},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
        # Some models return the image immediately; otherwise a request_id to poll
        url = _hf_extract_image(resp)
        if url:
            return jsonify({"status": "completed", "image_url": url})
        return jsonify({"status": resp.get("status", "queued"),
                        "request_id": resp.get("request_id", "")})
    except urllib.error.HTTPError as e:
        try:    err = e.read().decode()[:250]
        except Exception: err = ""
        print(f"[HF IMAGE] HTTP {e.code}: {err}")
        return jsonify({"status": "error", "image_url": None, "message": f"HTTP {e.code}: {err}"}), 502
    except Exception as e:
        print(f"[HF IMAGE] error: {e}")
        return jsonify({"status": "error", "image_url": None, "message": str(e)}), 502

@app.route("/generate/image/status", methods=["POST"])
def generate_image_status():
    """Poll Higgsfield for an image request. Returns image_url when completed."""
    data = request.json or {}
    rid  = data.get("request_id", "")
    if not valid_higgsfield_request_id(rid):
        return jsonify({"status": "error", "message": "Invalid request_id"}), 400
    auth = _higgsfield_auth()
    if not auth or not rid:
        return jsonify({"status": "error", "message": "Missing request_id or credentials"}), 400
    try:
        req = urllib.request.Request(
            f"https://platform.higgsfield.ai/requests/{rid}/status",
            headers={"Authorization": auth, "Accept": "application/json", "User-Agent": _HF_UA},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
        status = resp.get("status", "")
        url    = _hf_extract_image(resp)
        if status in ("failed", "nsfw"):
            print(f"[HF IMAGE FAIL] {rid[:8]} raw={json.dumps(resp)[:400]}")
        return jsonify({"status": status, "image_url": url})
    except urllib.error.HTTPError as e:
        try:    err = e.read().decode()[:250]
        except Exception: err = ""
        return jsonify({"status": "error", "message": f"HTTP {e.code}: {err}"}), 502
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 502


# Higgsfield image-to-video model — overridable via env var
HIGGSFIELD_MODEL = os.environ.get("HIGGSFIELD_MODEL", "higgsfield-ai/dop/standard")

def _higgsfield_auth():
    key    = os.environ.get("HIGGSFIELD_API_KEY", "").strip()
    secret = os.environ.get("HIGGSFIELD_API_SECRET", "").strip()
    if not key or not secret:
        return None
    return f"Key {key}:{secret}"

@app.route("/generate/video", methods=["POST"])
def generate_video():
    """Submit an image-to-video job to Higgsfield. Returns request_id for polling."""
    data      = request.json or {}
    prompt    = data.get("prompt", "")
    image_url = data.get("image_url", "")

    auth = _higgsfield_auth()
    if not auth:
        return jsonify({"status": "error",
                        "message": "Set HIGGSFIELD_API_KEY + HIGGSFIELD_API_SECRET in Railway env vars"}), 400
    if not image_url:
        return jsonify({"status": "error", "message": "Generate an image first"}), 400

    try:
        payload = json.dumps({
            "image_url": image_url,
            "prompt":    prompt,
            "duration":  5,
        }).encode()
        req = urllib.request.Request(
            f"https://platform.higgsfield.ai/{HIGGSFIELD_MODEL}",
            data=payload,
            headers={
                "Authorization": auth,
                "Content-Type":  "application/json",
                "Accept":        "application/json",
                "User-Agent":    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
        return jsonify({
            "status":     resp.get("status", "queued"),
            "request_id": resp.get("request_id", ""),
        })
    except urllib.error.HTTPError as e:
        try:    err = e.read().decode()[:250]
        except Exception: err = ""
        print(f"[HIGGSFIELD] HTTP {e.code}: {err}")
        return jsonify({"status": "error", "message": f"HTTP {e.code}: {err}"}), 502
    except Exception as e:
        print(f"[HIGGSFIELD] error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 502

@app.route("/generate/video/status", methods=["POST"])
def generate_video_status():
    """Poll Higgsfield for a request's status. Returns video_url when completed."""
    data = request.json or {}
    rid  = data.get("request_id", "")
    if not valid_higgsfield_request_id(rid):
        return jsonify({"status": "error", "message": "Invalid request_id"}), 400
    auth = _higgsfield_auth()
    if not auth or not rid:
        return jsonify({"status": "error", "message": "Missing request_id or credentials"}), 400
    try:
        req = urllib.request.Request(
            f"https://platform.higgsfield.ai/requests/{rid}/status",
            headers={
                "Authorization": auth,
                "Accept":        "application/json",
                "User-Agent":    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
        status    = resp.get("status", "")
        # Video URL can be nested a few ways depending on model — check them all
        video_url = None
        v = resp.get("video")
        if isinstance(v, dict):
            video_url = v.get("url")
        if not video_url and isinstance(resp.get("videos"), list) and resp["videos"]:
            first = resp["videos"][0]
            video_url = first.get("url") if isinstance(first, dict) else first
        if not video_url and isinstance(resp.get("output"), dict):
            video_url = resp["output"].get("url") or resp["output"].get("video_url")
        print(f"[HIGGSFIELD status] {rid[:8]} → status={status} url={'yes' if video_url else 'no'}")
        if status in ("failed", "nsfw"):
            print(f"[HIGGSFIELD FAIL] {rid[:8]} raw={json.dumps(resp)[:500]}")
        return jsonify({"status": status, "video_url": video_url, "raw": resp})
    except urllib.error.HTTPError as e:
        try:    err = e.read().decode()[:250]
        except Exception: err = ""
        return jsonify({"status": "error", "message": f"HTTP {e.code}: {err}"}), 502
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 502


# Gemini TTS — voice + model overridable via env vars
GEMINI_TTS_MODEL = os.environ.get("GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts")
GEMINI_VOICE     = os.environ.get("GEMINI_VOICE", "Kore")  # one consistent voice per SOP

@app.route("/generate/voice", methods=["POST"])
def generate_voice():
    """Text-to-speech via Gemini. Returns a playable WAV as a base64 data URL."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return jsonify({"error": "GEMINI_API_KEY not set"}), 400
    data  = request.json or {}
    text  = (data.get("text") or "").strip()
    voice = (data.get("voice") or GEMINI_VOICE).strip()
    if not text:
        return jsonify({"error": "empty text"}), 400
    try:
        payload = json.dumps({
            "contents": [{"parts": [{"text": text}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}
                },
            },
        }).encode()
        req = urllib.request.Request(
            f"{GEMINI_BASE}/models/{GEMINI_TTS_MODEL}:generateContent?key={api_key}",
            data=payload, headers={"Content-Type": "application/json"}, method="POST",
        )
        resp = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=90) as r:
                    resp = json.loads(r.read())
                break
            except urllib.error.HTTPError as he:
                if he.code in (429, 503) and attempt < 3:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise
        part = resp["candidates"][0]["content"]["parts"][0]
        inline = part.get("inlineData") or part.get("inline_data") or {}
        b64pcm = inline.get("data")
        mime   = inline.get("mimeType") or inline.get("mime_type") or ""
        if not b64pcm:
            return jsonify({"error": "no audio returned"}), 502
        rate = 24000
        m = re.search(r"rate=(\d+)", mime)
        if m:
            rate = int(m.group(1))
        pcm = base64.b64decode(b64pcm)
        # Wrap raw PCM (16-bit mono) in a WAV container so browsers can play it
        buf = io.BytesIO()
        w = wave.open(buf, "wb")
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(pcm); w.close()
        wav_b64 = base64.b64encode(buf.getvalue()).decode()
        return jsonify({"audio_url": f"data:audio/wav;base64,{wav_b64}"})
    except urllib.error.HTTPError as e:
        try:    err = e.read().decode()[:250]
        except Exception: err = ""
        print(f"[TTS] HTTP {e.code}: {err}")
        return jsonify({"error": f"HTTP {e.code}: {err}"}), 502
    except Exception as e:
        print(f"[TTS] error: {e}")
        return jsonify({"error": str(e)}), 502


@app.route("/logs")
@app.route("/logs/<job_id>")
def logs(job_id=None):
    jid = job_id or last_job_id
    if not jid or jid not in jobs:
        return "No job found yet — run a scrape first.", 404
    job = jobs[jid]
    lines = "\n".join(job.get("log", []))
    return (f"<pre style='font-family:monospace;font-size:13px;background:#0d1117;color:#7ee787;"
            f"padding:24px;min-height:100vh;margin:0;white-space:pre-wrap'>"
            f"Job: {jid}  |  Status: {job.get('status','?')}\n"
            f"{'─'*60}\n{lines}</pre>")

@app.route("/result/<job_id>")
def result(job_id):
    job = jobs.get(job_id)
    if not job:
        return "Job not found", 404
    if job["status"] == "done" and job.get("html"):
        return job["html"]
    return "Still running or error", 202

# ── JSON scrape API (/api/v1/scrapes) ────────────────────────────────────────
# Programmatic front door to the same prepare_scrape → launch_scrape_job →
# run_job path the browser form uses. Responses are assembled from an explicit
# allowlist of job fields (the job record never holds cookies to begin with).
# The API does not accept cookies: authenticated runs use the locked cookies,
# exactly as the form does when its cookies box is left empty.

API_SCRAPE_FIELDS = ("page_ids", "page_urls", "keywords", "domains", "country", "ad_status", "per_page")
API_COUNTRY_CODES = frozenset(code for code, _label in COUNTRIES)
API_JOB_STATUS    = {"running": "running", "done": "completed", "error": "failed"}
API_PAGE_ID_RE    = re.compile(r"\d{6,20}")


class ScrapeRequestError(ValueError):
    """A POST /api/v1/scrapes body failed validation."""


def parse_scrape_request(body):
    """Validate a POST /api/v1/scrapes body into prepare_scrape() kwargs.

    Raises ScrapeRequestError with a caller-safe message (it names fields and
    list positions, never echoes submitted values).
    """
    if not isinstance(body, dict):
        raise ScrapeRequestError("Request body must be a JSON object")
    unknown = sorted(set(body) - set(API_SCRAPE_FIELDS))
    if unknown:
        raise ScrapeRequestError(
            f"Unsupported field(s): {', '.join(unknown)}. Allowed: {', '.join(API_SCRAPE_FIELDS)}")

    country = body.get("country", "US")
    if not isinstance(country, str) or country.strip().upper() not in API_COUNTRY_CODES:
        codes = ", ".join(sorted(c for c in API_COUNTRY_CODES if c))
        raise ScrapeRequestError(f'country must be one of: {codes} (or "" for all regions)')
    country = country.strip().upper()

    ad_status = body.get("ad_status", "active")
    if ad_status not in ("active", "all"):
        raise ScrapeRequestError('ad_status must be "active" or "all"')

    per_page = body.get("per_page", 0)
    if isinstance(per_page, bool) or not isinstance(per_page, int) or per_page < 0:
        raise ScrapeRequestError("per_page must be a non-negative integer (0 = no per-page cap)")

    def str_list(field, allow_int=False):
        val = body.get(field)
        if val is None:
            return []
        ok_types = (str, int) if allow_int else (str,)
        if not isinstance(val, list) or any(isinstance(v, bool) or not isinstance(v, ok_types) for v in val):
            kind = "strings or integers" if allow_int else "strings"
            raise ScrapeRequestError(f"{field} must be a list of {kind}")
        return [str(v).strip() for v in val if str(v).strip()]

    page_ids  = str_list("page_ids", allow_int=True)
    page_urls = str_list("page_urls")
    keywords  = str_list("keywords")
    domains   = str_list("domains")

    for i, pid in enumerate(page_ids):
        if not API_PAGE_ID_RE.fullmatch(pid):
            raise ScrapeRequestError(f"page_ids[{i}] must be a numeric Facebook page ID (6-20 digits)")
    for i, url in enumerate(page_urls):
        if not fb_page_to_adlib_url(url, ad_status, country):
            raise ScrapeRequestError(f"page_urls[{i}] does not contain a recognizable Facebook page ID")

    if not (page_ids or page_urls or keywords or domains):
        raise ScrapeRequestError("Provide at least one of: page_ids, page_urls, keywords, domains")

    return {
        "kw_lines":     keywords,
        "domain_lines": domains,
        "page_urls":    page_ids + page_urls,
        "country":      country,
        "ad_status":    ad_status,
        "per_page":     per_page,
    }


def _api_error(code, message, http_status, **extra):
    body = dict(extra)
    body["error"] = {"code": code, "message": message}
    return jsonify(body), http_status


def _api_job_summary(job_id, job):
    return {
        "job_id":       job_id,
        "status":       API_JOB_STATUS.get(job.get("status"), job.get("status") or "unknown"),
        "auth_state":   job.get("auth_state"),
        "params":       job.get("params"),
        "created_at":   job.get("created_at"),
        "completed_at": job.get("completed_at"),
    }


def _api_job_failure(job):
    return {
        "code":     "scrape_failed",
        "message":  job.get("error") or "Scrape failed",
        "log_tail": list(job.get("log", []))[-5:],
    }


@app.route("/api/v1/scrapes", methods=["POST"])
def api_start_scrape():
    try:
        inputs = parse_scrape_request(request.get_json(silent=True))
    except ScrapeRequestError as e:
        return _api_error("invalid_request", str(e), 400)
    job_id = launch_scrape_job(prepare_scrape(**inputs))
    return jsonify({
        "job_id":     job_id,
        "status":     "running",
        "status_url": f"/api/v1/scrapes/{job_id}",
        "result_url": f"/api/v1/scrapes/{job_id}/result",
    }), 202

@app.route("/api/v1/scrapes/<job_id>")
def api_scrape_status(job_id):
    job = jobs.get(job_id)
    if job is None:
        return _api_error("not_found", "Unknown job_id", 404, job_id=job_id)
    body = _api_job_summary(job_id, job)
    body["log"] = list(job.get("log", []))
    if job.get("status") == "done":
        body["count"] = len(job.get("ads") or [])
    elif job.get("status") == "error":
        body["error"] = _api_job_failure(job)
    return jsonify(body)

@app.route("/api/v1/scrapes/<job_id>/result")
def api_scrape_result(job_id):
    job = jobs.get(job_id)
    if job is None:
        return _api_error("not_found", "Unknown job_id", 404, job_id=job_id)
    body = _api_job_summary(job_id, job)
    if job.get("status") == "done":
        ads = list(job.get("ads") or [])
        body.update({"completed": True, "count": len(ads), "ads": ads})
        return jsonify(body)
    # Not completed: never return partial ads, whatever the job record holds.
    body["completed"] = False
    if job.get("status") == "error":
        body["error"] = _api_job_failure(job)
    else:
        body["error"] = {"code": "not_completed",
                         "message": "Scrape has not completed; poll the status endpoint and retry"}
    return jsonify(body), 409

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
