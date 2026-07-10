"""
Meta Ad Intelligence Scraper
Uses curious_coder/facebook-ads-library-scraper (actor: XtaWFhbtfxyzqrFmd)
Deploy on Railway — set APIFY_TOKEN env var.
"""

import json, time, threading, uuid, urllib.request, os
from urllib.parse import urlparse, quote as urlquote
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string

app  = Flask(__name__)
jobs = {}  # { job_id: {status, log, html} }

# ── Config ───────────────────────────────────────────────────────────────────

APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "")
META_ACTOR  = "XtaWFhbtfxyzqrFmd"  # curious_coder/facebook-ads-library-scraper
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
    url  = f"{APIFY_BASE}/{path}?token={APIFY_TOKEN}"
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
    items = api_get(f"datasets/{ds_id}/items?limit=200")
    return items.get("items", [])


# ── Ad data helpers ───────────────────────────────────────────────────────────

def extract_urls(ad):
    """Return (images[], videos[]) from a curious_coder ad record."""
    imgs, vids = [], []
    snap = ad.get("snapshot") or {}

    # Top-level images
    for key in ("resized_image_url", "original_image_url"):
        v = snap.get(key) or ad.get(key)
        if v and v not in imgs:
            imgs.append(v)

    # Carousel cards
    for card in snap.get("cards", []):
        for key in ("resized_image_url", "original_image_url"):
            v = card.get(key)
            if v and v not in imgs:
                imgs.append(v)
        for key in ("video_hd_url", "video_sd_url"):
            v = card.get(key)
            if v and v not in vids:
                vids.append(v)

    # Top-level videos
    for key in ("video_hd_url", "video_sd_url"):
        v = snap.get(key) or ad.get(key)
        if v and v not in vids:
            vids.append(v)

    # Use video preview as image fallback
    if vids and not imgs:
        prev = snap.get("video_preview_image_url") or ad.get("video_preview_image_url")
        if prev:
            imgs.append(prev)

    return imgs, vids


def normalize_ad(ad):
    """Flatten a curious_coder record into a display dict."""
    snap = ad.get("snapshot") or {}

    name = ad.get("pageName") or snap.get("page_name") or "Unknown"

    status = "ACTIVE" if ad.get("isActive") else "INACTIVE"

    raw_date = ad.get("startDate", "")
    if isinstance(raw_date, (int, float)) and raw_date > 0:
        try:
            raw_date = datetime.fromtimestamp(raw_date).strftime("%Y-%m-%d")
        except Exception:
            raw_date = ""

    body  = (snap.get("body")  or {})
    body  = body.get("text", "") if isinstance(body, dict) else str(body or "")
    title = (snap.get("title") or {})
    title = title.get("text", "") if isinstance(title, dict) else str(title or "")

    cta = snap.get("cta_text") or snap.get("call_to_action_type") or ""

    landing = snap.get("link_url") or snap.get("landing_page_url") or ""
    if not landing:
        for c in snap.get("cards", []):
            landing = c.get("link_url") or ""
            if landing:
                break

    ad_id   = str(ad.get("adArchiveID") or ad.get("ad_archive_id") or "")
    lib_url = f"https://www.facebook.com/ads/library/?id={ad_id}" if ad_id else "#"

    # Impressions index → human range
    impressions = ""
    imp = ad.get("impressionsWithIndex") or {}
    if isinstance(imp, dict):
        idx    = imp.get("impressionsIndex", -1)
        ranges = ["<1K", "1K–5K", "5K–20K", "20K–50K", "50K–100K", "100K–500K", "500K–1M", ">1M"]
        if 0 <= idx < len(ranges):
            impressions = ranges[idx]

    variants = ad.get("collationCount", 0) or 0

    pubs  = ad.get("publisherPlatform") or []
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
        "variants":    int(variants),
        "plats":       plats,
        "ad_id":       ad_id,
    }


# ── Scrape worker ─────────────────────────────────────────────────────────────

def run_job(job_id, brand, country, searches, domain, page_url, ad_status):
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
                    for q in queries if q
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

                run    = api_post(f"acts/{META_ACTOR}/runs", {"urls": urls, "count": 15})
                run_id = run["data"]["id"]
                ads    = wait_for_run(run_id, log)
                log(f"   ✓ {len(ads)} ads")
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
    active_cnt = sum(1 for a in ads if a.get("isActive"))
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
        if vids:
            dl_links = " ".join(
                f'<a href="{v}" target="_blank" class="vid-dl">⬇ Video {i+1}</a>'
                for i, v in enumerate(vids[:3]))
            media = (f'<div class="media-wrap">'
                     f'<video controls preload="metadata" src="{vids[0]}" crossorigin="anonymous"></video>'
                     f'<div class="vid-dl-row">{dl_links}</div></div>')
        elif imgs:
            img_html = "".join(
                f'<img src="{img}" onclick="openFull(this.src)" crossorigin="anonymous">'
                for img in imgs[:4])
            media = (f'<div class="media-wrap img-grid img-count-{min(len(imgs),4)}">'
                     f'{img_html}</div>')
        else:
            icon  = "🎬" if fmt == "VIDEO" else "🖼️"
            media = (f'<div class="media-placeholder"><span>{icon}</span>'
                     f'<a href="{lib_url}" target="_blank">View in Ad Library →</a></div>')

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

        return (
            f'<div class="card" data-status="{n["status"]}" data-fmt="{fmt}"'
            f' data-advertiser="{adv_slug}" data-body="{body_slug}" data-title="{ttl_slug}"'
            f' data-date="{n["date"]}" data-cta="{cta}" data-lp="{lp_slug}" data-lib="{lib_slug}">'
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
            f'</div></div></div>'
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

        if vids:
            media_cell = f'<video src="{vids[0]}" class="table-thumb" controls></video>'
        elif thumb:
            media_cell = f'<img src="{thumb}" class="table-thumb" onclick="openFull(this.src)">'
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
    brand_slug = brand.replace(" ", "_")

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
  <input id="srch" class="search-box" placeholder="Search advertiser or copy…" oninput="applyFilters()">
  <select class="sort-sel" onchange="sortCards(this.value)">
    <option value="">Sort: default</option>
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

<div id="lb" onclick="this.classList.remove('open')"><img id="lbi" src=""></div>

<script>
// ── State
let curFilter = 'all', curView = 'card';

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
    <label>Brand Name</label>
    <input name="brand" placeholder="e.g. NovaBurn" required>

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
      if (d.status === 'done')  window.location.href = '/result/' + jobId;
      else if (d.status === 'error') logEl.innerHTML += '<div style="color:#f85149">❌ Error — check log above</div>';
      else setTimeout(poll, 2000);
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
    brand        = request.form.get("brand", "Brand").strip()
    country      = request.form.get("country", "US")
    ad_status    = request.form.get("ad_status", "active")
    domain_input = request.form.get("domain", "").strip()
    page_url     = request.form.get("page_url", "").strip()
    searches_raw = request.form.getlist("search[]")
    searches     = [[q.strip() for q in s.split(",") if q.strip()] for s in searches_raw if s.strip()]

    # Extract clean domain
    domain = ""
    if domain_input:
        raw = domain_input if "://" in domain_input else "https://" + domain_input
        domain = urlparse(raw).netloc

    # Fallback: use brand name if no keywords entered
    if not searches:
        searches = [[brand]]

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {"status": "running", "log": [], "html": None}

    threading.Thread(
        target=run_job,
        args=(job_id, brand, country, searches, domain, page_url, ad_status),
        daemon=True
    ).start()

    return render_template_string(PROGRESS_HTML, job_id=job_id, brand=brand)

@app.route("/status/<job_id>")
def status(job_id):
    job = jobs.get(job_id, {})
    return jsonify({"status": job.get("status", "unknown"), "log": job.get("log", [])})

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
