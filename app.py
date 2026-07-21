"""
Meta Ad Intelligence Scraper
Uses curious_coder/facebook-ads-library-scraper (actor: XtaWFhbtfxyzqrFmd)
Deploy on Railway — set APIFY_TOKEN env var.
"""

import json, time, threading, uuid, urllib.request, os, re
from urllib.parse import urlparse, quote as urlquote, parse_qs
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string

app  = Flask(__name__)
jobs = {}        # { job_id: {status, log, html} }
last_job_id = None  # track most recent job for /logs endpoint

# In-memory locked cookies — set via /cookies/lock, reused across scrapes until unlocked.
# Temporary: cleared when the server restarts/redeploys.
LOCKED_COOKIES = None

# ── Config ───────────────────────────────────────────────────────────────────

APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")
META_ACTOR  = "XtaWFhbtfxyzqrFmd"   # curious_coder/facebook-ads-library-scraper (unauthenticated)
AUTH_ACTOR  = os.environ.get("AUTH_ACTOR_ID", "")  # your custom meta-ads-auth-scraper actor ID
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
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def api_post(path, payload):
    return apify_req("POST", path, payload)

def api_get(path):
    return apify_req("GET", path)

def wait_for_run(run_id, log, poll=5, timeout=300):
    deadline = time.time() + timeout
    r = {}
    while time.time() < deadline:
        r      = api_get(f"actor-runs/{run_id}")
        status = r["data"]["status"]
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            break
        time.sleep(poll)
    ds_id = r["data"]["defaultDatasetId"]
    raw = api_get(f"datasets/{ds_id}/items?limit=200")
    # Apify may return items as a raw list OR wrapped in {"data": {"items": [...]}}
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        if "data" in raw:
            return raw["data"].get("items", [])
        return raw.get("items", [])
    return []


# ── Ad data helpers ───────────────────────────────────────────────────────────

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

    # snapshot.images[] — objects with resized_image_url / original_image_url / url
    for img_obj in snap.get("images") or []:
        if isinstance(img_obj, dict):
            add_img(img_obj.get("resized_image_url"))
            add_img(img_obj.get("original_image_url"))
            add_img(img_obj.get("url"))
        elif isinstance(img_obj, str):
            add_img(img_obj)

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

    # Carousel cards
    for card in snap.get("cards") or []:
        add_img(card.get("resized_image_url"))
        add_img(card.get("original_image_url"))
        add_img(card.get("url"))
        add_vid(card.get("video_hd_url"))
        add_vid(card.get("video_sd_url"))

    # extra_images / extra_videos (confirmed in snapshot keys)
    for img_obj in snap.get("extra_images") or []:
        if isinstance(img_obj, dict):
            add_img(img_obj.get("resized_image_url"))
            add_img(img_obj.get("original_image_url"))
            add_img(img_obj.get("url"))
        elif isinstance(img_obj, str):
            add_img(img_obj)
    for vid_obj in snap.get("extra_videos") or []:
        if isinstance(vid_obj, dict):
            add_vid(vid_obj.get("video_hd_url"))
            add_vid(vid_obj.get("video_sd_url"))
            add_vid(vid_obj.get("url"))
        elif isinstance(vid_obj, str):
            add_vid(vid_obj)

    # Top-level snapshot fallbacks
    add_img(snap.get("resized_image_url"))
    add_img(snap.get("original_image_url"))
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
    }


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
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())

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
        return []

    log(f"  🔐 Authenticated mode — running actor {AUTH_ACTOR}")
    try:
        run = api_post(f"acts/{AUTH_ACTOR}/runs", {
            "urls":    search_urls,
            "cookies": cookies_list,
            "count":   count,
        })
        run_id = run["data"]["id"]
        ads = wait_for_run(run_id, log)
        log(f"  🔐 {len(ads)} ads from auth actor")
        # Actor returns same snake_case format as curious_coder
        # but force gated_type to ELIGIBLE since we're authenticated
        for ad in ads:
            ad["gated_type"] = "ELIGIBLE"
        return ads
    except Exception as e:
        log(f"  ❌ Auth actor failed: {e}")
        return []


# ── Scrape worker ─────────────────────────────────────────────────────────────

def run_job(job_id, brand, country, searches, domain, page_url, ad_status, cookies=None):
    job = jobs[job_id]
    def log(msg): job["log"].append(msg)

    try:
        _country = country or "US"
        _status  = ad_status if ad_status in ("active", "all") else "active"
        results  = [[] for _ in searches]

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
                    # Domain search: find all advertisers running ads to this domain
                    if domain:
                        urls.append({"url": (
                            f"https://www.facebook.com/ads/library/"
                            f"?active_status={_status}&ad_type=all&country={_country}"
                            f"&q={urlquote(domain)}&search_type=page_like_and_ads_using_domain&media_type=all"
                        )})
                    # Page URL: pull all ads from a specific Facebook page
                    if page_url:
                        urls.append({"url": page_url})

                if not urls:
                    results[i] = []
                    return

                if cookies:
                    ads = meta_auth_search(urls, cookies, count=20,
                                           country=_country, ad_status=_status, log=log)
                else:
                    run    = api_post(f"acts/{META_ACTOR}/runs", {"urls": urls, "count": 15, "scrapeAdDetails": True})
                    run_id = run["data"]["id"]
                    ads    = wait_for_run(run_id, log)
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

        log(f"📊 {len(unique)} unique ads")
        translate_ads_bulk(unique, log)
        job["html"]   = build_viewer(brand, country, unique)
        job["status"] = "done"
        log("✅ Done!")

    except Exception as e:
        import traceback
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
            f' data-orig-imgs="{orig_imgs_attr}" data-orig-vids="{orig_vids_attr}">'
            f'<label class="card-cb-wrap" onclick="event.stopPropagation()"><input type="checkbox" class="card-cb" onchange="toggleSelect(this)"></label>'
            f'<div class="card-header">'
            f'<div class="card-name">{n["name"]}</div>'
            f'<div class="card-meta">{n["date"]} · {n["plats"]}</div>'
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
  <select class="sort-sel" onchange="sortCards(this.value)">
    <option value="">Sort: default</option>
    <option value="imp_desc" selected>Impressions ↓</option>
    <option value="date_desc">Newest first</option>
    <option value="date_asc">Oldest first</option>
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
let selectMode = false;
const selected = new Set();
const KEYWORD = "{brand_slug}";  // search term / brand used for this scrape

// Default sort: impressions descending
sortCards('imp_desc');

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
  if (curView === 'card') {{
    document.querySelectorAll('.card').forEach(c => {{
      const show = matchF(c) && (!q || [c.dataset.advertiser, c.dataset.body, c.dataset.title].some(s => (s||'').toLowerCase().includes(q)));
      c.style.display = show ? '' : 'none';
      if (show) n++;
    }});
  }} else {{
    document.querySelectorAll('#tbody tr').forEach(r => {{
      const show = matchF(r) && (!q || r.textContent.toLowerCase().includes(q));
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
    const folder = zip.folder(`${{date}} - ${{kw}} - ${{type}} - ${{adv}}`);

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
    const id       = (card.dataset.lib || '').match(/id=(\d+)/)?.[1] || idx;
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
    el.innerHTML = `<strong>Visual Style:</strong> ${{data.visual_style}}<br>
                    <strong>Hook Type:</strong> ${{data.hook_type}}<br>
                    <strong>Tone:</strong> ${{data.tone}}
                    ${{data.note ? `<br><em style="color:#888;font-size:11px">⚠️ ${{data.note}}</em>` : ''}}`;
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
  btn.textContent = '⏳ Generating…';
  btn.disabled    = true;
  document.getElementById(`flux-out-${{id}}`).innerHTML = '⏳ Calling Flux…';
  try {{
    const r    = await fetch('/generate/image', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{ ad_id:id, prompt }}) }});
    const data = await r.json();
    const out  = document.getElementById(`flux-out-${{id}}`);
    if (data.image_url) {{
      out.innerHTML = `<div><img src="${{data.image_url}}" alt="Generated">${{data.message ? `<div style="padding:6px 8px;font-size:11px;color:#888">${{data.message}}</div>` : ''}}</div>`;
      const hfBtn = document.getElementById(`hf-btn-${{id}}`);
      hfBtn.disabled    = false;
      hfBtn.dataset.imgUrl = data.image_url;
    }} else {{
      out.textContent = data.message || 'No image returned';
    }}
    btn.textContent = '🖼 Regenerate';
    btn.disabled    = false;
  }} catch(e) {{
    document.getElementById(`flux-out-${{id}}`).textContent = '❌ Error';
    btn.textContent = '🖼 Generate Image';
    btn.disabled    = false;
  }}
}}

async function generateVideo(id, btn) {{
  const prompt  = document.getElementById(`hf-prompt-${{id}}`).value;
  const imgUrl  = btn.dataset.imgUrl || '';
  btn.textContent = '⏳ Animating…';
  btn.disabled    = true;
  document.getElementById(`hf-out-${{id}}`).innerHTML = '⏳ Calling Higgsfield…';
  try {{
    const r    = await fetch('/generate/video', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{ ad_id:id, prompt, image_url:imgUrl }}) }});
    const data = await r.json();
    const out  = document.getElementById(`hf-out-${{id}}`);
    if (data.video_url) {{
      out.innerHTML = `<video src="${{data.video_url}}" controls style="width:100%"></video>`;
    }} else {{
      out.textContent = data.message || 'No video returned';
    }}
    btn.textContent = '🎬 Reanimate';
    btn.disabled    = false;
  }} catch(e) {{
    document.getElementById(`hf-out-${{id}}`).textContent = '❌ Error';
    btn.textContent = '🎬 Animate';
    btn.disabled    = false;
  }}
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

    <div class="divider">
      <label>Keywords / Search Terms <span style="font-weight:normal;color:#aaa">(one group per row, comma-separated)</span></label>
      <div id="searches">
        <div class="search-row">
          <input name="search[]" placeholder="e.g. weight loss, fat burner">
          <button type="button" class="remove-btn" onclick="removeRow(this)" title="Remove">&#x2715;</button>
        </div>
      </div>
      <button type="button" class="add-btn" onclick="addRow()">+ Add keyword group</button>
    </div>

    <div class="divider">
      <label>Landing Page / Domain <span style="font-weight:normal;color:#aaa">(optional — finds all advertisers driving traffic to this domain)</span></label>
      <input name="domain" placeholder="e.g. get-novaburn.com">

      <label style="margin-top:14px">Competitor Facebook Page URL <span style="font-weight:normal;color:#aaa">(optional)</span></label>
      <input name="page_url" placeholder="e.g. https://www.facebook.com/BeyondTheScale">
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


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def home():
    return HOME_HTML.replace("COUNTRY_OPTIONS", COUNTRY_OPTIONS)

@app.route("/start", methods=["POST"])
def start():
    country      = request.form.get("country", "US")
    ad_status    = request.form.get("ad_status", "active")
    domain_input = request.form.get("domain", "").strip()
    page_url     = request.form.get("page_url", "").strip()
    searches_raw = request.form.getlist("search[]")
    searches     = [[q.strip() for q in s.split(",") if q.strip()] for s in searches_raw if s.strip()]
    first_kw     = searches_raw[0].strip() if searches_raw else ""
    brand        = domain_input or page_url or first_kw or "Meta Ads"  # label for results page only

    # Extract clean domain (handles full URLs like https://trimrx.com/path?query=1)
    domain = ""
    if domain_input:
        raw = domain_input if "://" in domain_input else "https://" + domain_input
        domain = urlparse(raw).netloc  # strips path, query, fragment — just hostname

    # If domain/page_url provided but no keywords, fire one thread with just those
    if not searches:
        searches = [[]]

    # Parse optional Facebook session cookies (Cookie-Editor JSON export).
    # If the textarea is empty, fall back to locked cookies (if any).
    cookies = None
    cookies_raw = request.form.get("cookies", "").strip()
    if cookies_raw:
        try:
            cookies = json.loads(cookies_raw)
            if not isinstance(cookies, list):
                cookies = None
        except Exception:
            cookies = None
    if cookies is None and LOCKED_COOKIES:
        cookies = LOCKED_COOKIES

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {"status": "running", "log": [], "html": None}
    global last_job_id
    last_job_id = job_id

    threading.Thread(
        target=run_job,
        args=(job_id, brand, country, searches, domain, page_url, ad_status),
        kwargs={"cookies": cookies},
        daemon=True
    ).start()

    return render_template_string(PROGRESS_HTML, job_id=job_id, brand=brand)

@app.route("/status/<job_id>")
def status(job_id):
    job = jobs.get(job_id, {})
    return jsonify({"status": job.get("status", "unknown"), "log": job.get("log", [])})

@app.route("/cookies/status")
def cookies_status():
    """Report whether cookies are currently locked."""
    n = len(LOCKED_COOKIES) if LOCKED_COOKIES else 0
    return jsonify({"locked": bool(LOCKED_COOKIES), "count": n})

@app.route("/cookies/lock", methods=["POST"])
def cookies_lock():
    """Save cookies in memory so they're reused on every scrape."""
    global LOCKED_COOKIES
    raw = (request.json or {}).get("cookies", "").strip()
    if not raw:
        return jsonify({"ok": False, "error": "No cookies provided"}), 400
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, list) or not parsed:
            return jsonify({"ok": False, "error": "Cookies must be a non-empty JSON array"}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"Invalid JSON: {e}"}), 400
    LOCKED_COOKIES = parsed
    return jsonify({"ok": True, "count": len(parsed)})

@app.route("/cookies/unlock", methods=["POST"])
def cookies_unlock():
    """Clear locked cookies."""
    global LOCKED_COOKIES
    LOCKED_COOKIES = None
    return jsonify({"ok": True})

@app.route("/img")
def proxy_img():
    """Server-side proxy for Facebook CDN images.
    Facebook signed URLs (oh= hash) are tied to the requester's session/IP.
    Fetching server-side avoids browser-level auth failures.
    """
    url = request.args.get("u", "")
    if not url or "fbcdn.net" not in url:
        return "", 400
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.facebook.com/",
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read()
            ct   = r.headers.get("Content-Type", "image/jpeg")
        resp = app.response_class(data, mimetype=ct)
        resp.headers["Cache-Control"] = "public, max-age=3600"
        return resp
    except Exception:
        return "", 502

@app.route("/vid")
def proxy_vid():
    """Server-side proxy for Facebook CDN videos — bypasses browser CORS."""
    url = request.args.get("u", "")
    if not url or "fbcdn.net" not in url:
        return "", 400
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.facebook.com/",
        })
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
            ct   = r.headers.get("Content-Type", "video/mp4")
        resp = app.response_class(data, mimetype=ct)
        resp.headers["Cache-Control"] = "public, max-age=3600"
        return resp
    except Exception:
        return "", 502

@app.route("/analyze", methods=["POST"])
def analyze_ad():
    """
    Analyze ad creative and generate prompts for Flux + Higgsfield.
    PLACEHOLDER — replace with Claude Vision + Gemini calls when API keys are set.
    Set ANTHROPIC_API_KEY for image analysis, GEMINI_API_KEY for video analysis.
    """
    data = request.json or {}
    adv  = data.get("advertiser", "the brand")
    fmt  = data.get("format", "IMAGE")
    title = data.get("title", "")
    body  = data.get("body", "")
    imgs  = data.get("orig_imgs", [])
    vids  = data.get("orig_vids", [])

    print(f"[ANALYZE] {adv} | {fmt} | imgs={len(imgs)} vids={len(vids)}")
    print(f"[ANALYZE] Title: {title[:80]}")
    print(f"[ANALYZE] Body:  {body[:200]}")

    is_video = fmt == "VIDEO" or bool(vids)

    flux_prompt = (
        f"High-quality commercial lifestyle photography, {adv} brand advertisement, "
        f"authentic UGC aesthetic, natural lighting, photorealistic, 4K, "
        f"compelling composition, aspirational yet relatable"
    )
    hf_prompt = (
        "Smooth cinematic camera movement, subject speaking directly to camera, "
        "warm professional lighting, authentic natural feel, slow zoom in, "
        "shallow depth of field, high production value UGC style"
    )

    return jsonify({
        "ad_id":             data.get("ad_id"),
        "visual_style":      "UGC talking head — single speaker, casual authentic setting",
        "hook_type":         "Problem-aware hook — opens with pain point before solution",
        "tone":              "Conversational, trust-building, authoritative",
        "flux_prompt":       flux_prompt,
        "higgsfield_prompt": hf_prompt,
        "note":              "Placeholder analysis — add ANTHROPIC_API_KEY + GEMINI_API_KEY for real vision analysis",
    })


@app.route("/generate/image", methods=["POST"])
def generate_image():
    """
    Generate static image with Flux via fal.ai.
    PLACEHOLDER — set FAL_API_KEY env var to enable real generation.
    """
    data   = request.json or {}
    prompt = data.get("prompt", "")
    print(f"[FLUX PLACEHOLDER] {prompt[:120]}")
    # TODO: fal_client.submit("fal-ai/flux/dev", arguments={"prompt": prompt})
    return jsonify({
        "status":    "placeholder",
        "image_url": "https://placehold.co/800x800/6366f1/white?text=Flux+Image%0AAdd+FAL_API_KEY",
        "message":   "Placeholder — set FAL_API_KEY in Railway env vars to enable Flux generation",
    })


@app.route("/generate/video", methods=["POST"])
def generate_video():
    """
    Animate image to video with Higgsfield.
    PLACEHOLDER — set HIGGSFIELD_API_KEY env var to enable real animation.
    """
    data      = request.json or {}
    prompt    = data.get("prompt", "")
    image_url = data.get("image_url", "")
    print(f"[HIGGSFIELD PLACEHOLDER] img={image_url[:60]} prompt={prompt[:80]}")
    # TODO: POST https://platform.higgsfield.ai/higgsfield-ai/dop/standard
    #       headers: Authorization: Key {HIGGSFIELD_API_KEY}
    #       body: { image_url, prompt, duration: 5 }
    return jsonify({
        "status":    "placeholder",
        "video_url": None,
        "message":   "Placeholder — set HIGGSFIELD_API_KEY in Railway env vars to enable Higgsfield animation",
    })


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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
