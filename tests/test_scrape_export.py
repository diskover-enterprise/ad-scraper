"""Tests for the programmatic ZIP export (GET /api/v1/scrapes/<id>/export).

The export is a server-side twin of the viewer's bulkZip() ("Download ZIP"),
whose output the Meta Mock Uploader already accepts. These tests cover:
- A completed job exports a valid ZIP with bulkZip()'s per-ad folders,
  imageN.jpg/png, videoN.mp4 and ad_copy.txt.
- Folder names and ad_copy.txt text follow bulkZip()'s conventions exactly
  (golden values worked out by hand from the JS).
- Media is fetched through the real safe_fetch_media() boundary shared with
  /img and /vid; failures follow bulkZip()'s skip / videoN_url.txt fallbacks.
- Running / failed / unknown jobs do not export.
- The browser side is unchanged: bulkZip()'s source is pinned verbatim, and
  (on Python 3.12+) the real build_viewer() output's card data-* values are
  checked against the values the export derives from the retained ads.

Why these tests load source text instead of `import app`
----------------------------------------------------------
Same reason as tests/test_scrape_api.py (a PEP 701 f-string in app.py). The
job/route namespace comes from that file's loader, and the media boundary is
exec'd from app.py with its single network seam (_media_open) replaced by the
in-memory FakeNetwork from tests/test_media_fetch_boundary.py.

Network safety
--------------
Every fbcdn URL below is a string fixture served by FakeNetwork; nothing is
ever contacted. No Apify, Facebook, Anthropic or Railway call is made.
"""
from __future__ import annotations

import io
import ipaddress
import re
import sys
import time
import unittest
import urllib
import urllib.request
import zipfile
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote as urlquote, urljoin, urlparse, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_media_fetch_boundary as media_tests  # noqa: E402  (module import: its TestCases aren't re-collected here)
import test_scrape_api as api_tests  # noqa: E402

flask = api_tests.flask
APP_SRC = api_tests.APP_SRC

IMG_1001   = "https://scontent.xx.fbcdn.net/1001.jpg"
THUMB_1002 = "https://scontent.xx.fbcdn.net/1002_thumb.jpg"
VID_1002   = "https://video.xx.fbcdn.net/1002.mp4"
JPEG_1001  = b"\xff\xd8\xff\xe0jpeg-1001"
PNG_THUMB  = b"\x89PNG\r\n\x1a\nthumb-1002"
MP4_1002   = b"\x00\x00\x00\x18ftypmp42-1002"

FOLDER_1001 = "2026-09-01 - 111111111 - STATIC - Acme_Co - 1001"
FOLDER_1002 = "2026-09-01 - 111111111 - VIDEO - Acme_Co - 1002"


def actor_network():
    return media_tests.FakeNetwork({
        IMG_1001:   media_tests.ok("image/jpeg", JPEG_1001),
        THUMB_1002: media_tests.ok("image/png", PNG_THUMB),
        VID_1002:   media_tests.ok("video/mp4; codecs=avc1", MP4_1002),
    })


def load_export_namespace(network, **kwargs):
    """test_scrape_api's job/route namespace (real run_job, launch and API
    region -- which now includes the export route) plus the real media
    boundary and viewer_brand_slug, with _media_open replaced by `network`."""
    ns, calls, threads = api_tests.load_api_namespace(**kwargs)
    ns.update({"io": io, "zipfile": zipfile, "time": time, "ipaddress": ipaddress,
               "urllib": urllib, "urlsplit": urlsplit, "urljoin": urljoin})
    exec(api_tests.extract_region("# ── Safe media fetch boundary", '@app.route("/img")'), ns)  # noqa: S102
    exec(api_tests.extract_block(r"^def viewer_brand_slug\("), ns)  # noqa: S102
    ns["_media_open"] = network
    return ns, calls, threads


def read_zip(data):
    zf = zipfile.ZipFile(io.BytesIO(data))
    return zf, {n: zf.read(n) for n in zf.namelist()}


def edge_case_raw_ad():
    """No title / CTA / landing / date / creative; quotes, newline, accents,
    an emoji and an over-long body -- every bulkZip() convention in one ad."""
    raw = api_tests.make_ad("3003", name='Zoë "Best" Café 🚀')
    snap = raw["snapshot"]
    snap.update({"title": {"text": ""}, "images": [],
                 "body": {"text": 'He said "hi"\nline2 ' + "x" * 400}})
    del snap["cta_text"], snap["link_url"]
    raw["start_date"] = ""
    return raw


@unittest.skipIf(flask is None, "flask not installed")
class ExportTestCase(unittest.TestCase):

    def setUpNamespace(self, network=None, **kwargs):
        self.net = network if network is not None else actor_network()
        self.ns, self.calls, self.threads = load_export_namespace(self.net, **kwargs)
        self.client = self.ns["app"].test_client()

    def start_job(self):
        return self.client.post("/api/v1/scrapes", json=api_tests.VALID_BODY).get_json()["job_id"]

    def put_done_job(self, raw_ads, brand="111111111", job_id="job00001"):
        self.ns["jobs"][job_id] = {"status": "done", "log": [], "brand": brand,
                                   "ads": [self.ns["ad_record"](a) for a in raw_ads]}
        return job_id

    def export(self, job_id):
        return self.client.get(f"/api/v1/scrapes/{job_id}/export")


# 1. Completed job → uploader-compatible ZIP ─────────────────────────────────

class TestCompletedExport(ExportTestCase):

    def test_completed_job_returns_valid_zip(self):
        self.setUpNamespace(locked_cookies=api_tests.LOCKED_COOKIES)
        resp = self.export(self.start_job())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/zip")
        self.assertEqual(resp.headers["Content-Disposition"], 'attachment; filename="ads_download.zip"')
        data = resp.get_data()
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(data)))
        zf, _ = read_zip(data)
        self.assertIsNone(zf.testzip())

    def test_expected_folders_and_files(self):
        self.setUpNamespace(locked_cookies=api_tests.LOCKED_COOKIES)
        zf, files = read_zip(self.export(self.start_job()).get_data())
        self.assertEqual(zf.namelist(), [
            f"{FOLDER_1001}/", f"{FOLDER_1001}/image1.jpg", f"{FOLDER_1001}/ad_copy.txt",
            f"{FOLDER_1002}/", f"{FOLDER_1002}/image1.png", f"{FOLDER_1002}/video1.mp4",
            f"{FOLDER_1002}/ad_copy.txt",
        ])
        self.assertTrue(zf.getinfo(f"{FOLDER_1001}/").is_dir())

    def test_image_ad_includes_image_bytes(self):
        self.setUpNamespace(locked_cookies=api_tests.LOCKED_COOKIES)
        _, files = read_zip(self.export(self.start_job()).get_data())
        self.assertEqual(files[f"{FOLDER_1001}/image1.jpg"], JPEG_1001)
        self.assertFalse(any(n.startswith(f"{FOLDER_1001}/video") for n in files))

    def test_video_ad_includes_mp4_and_png_thumbnail(self):
        self.setUpNamespace(locked_cookies=api_tests.LOCKED_COOKIES)
        _, files = read_zip(self.export(self.start_job()).get_data())
        self.assertEqual(files[f"{FOLDER_1002}/video1.mp4"], MP4_1002)
        # bulkZip names an image .png when the fetched type contains "png"
        self.assertEqual(files[f"{FOLDER_1002}/image1.png"], PNG_THUMB)

    def test_ad_copy_matches_browser_convention(self):
        self.setUpNamespace(locked_cookies=api_tests.LOCKED_COOKIES)
        _, files = read_zip(self.export(self.start_job()).get_data())
        self.assertEqual(files[f"{FOLDER_1001}/ad_copy.txt"].decode("utf-8"), (
            "ADVERTISER: Acme Co\n"
            "STATUS: ACTIVE  |  FORMAT: IMAGE  |  DATE: 2026-09-01\n"
            "\n"
            "HEADLINE:\nTitle 1001\n"
            "\n"
            "AD COPY:\nBody copy 1001\n"
            "\n"
            "CTA: Shop Now\n"
            "LANDING PAGE: https://example.com/landing\n"
            "AD LIBRARY: https://www.facebook.com/ads/library/?id=1001"
        ))
        self.assertIn("FORMAT: VIDEO", files[f"{FOLDER_1002}/ad_copy.txt"].decode("utf-8"))

    def test_edge_case_folder_name_and_ad_copy(self):
        self.setUpNamespace()
        job_id = self.put_done_job([edge_case_raw_ad()], brand="https://www.facebook.com/AcmeShoes?ref=x")
        zf, files = read_zip(self.export(job_id).get_data())
        folder = "nodate - AcmeShoes - STATIC - Zo___Best__Caf____ - 3003"
        self.assertEqual(zf.namelist(), [f"{folder}/", f"{folder}/ad_copy.txt"])
        body = ("He said 'hi' line2 " + "x" * 400)[:300]
        self.assertEqual(files[f"{folder}/ad_copy.txt"].decode("utf-8"), (
            "ADVERTISER: Zoë 'Best' Café 🚀\n"
            "STATUS: ACTIVE  |  FORMAT: UNKNOWN  |  DATE: nodate\n"
            "\n\n\n"                        # blank, (no headline), blank
            f"AD COPY:\n{body}\n"
            "\n\n"                          # blank, (no CTA)
            "LANDING PAGE: #\n"             # data-lp falls back to "#"
            "AD LIBRARY: https://www.facebook.com/ads/library/?id=3003"
        ))
        self.assertEqual(self.net.calls, [])

    def test_viewer_order_and_index_fallback_for_missing_ad_id(self):
        self.setUpNamespace()
        older, newer = api_tests.make_ad("x"), api_tests.make_ad("y")
        for raw, date in ((older, "2026-01-01"), (newer, "2026-05-01")):
            raw.pop("ad_archive_id")  # lib_url "#" → no id=… → n<idx>
            raw["start_date"] = date
            raw["snapshot"]["images"] = []
        zf, _ = read_zip(self.export(self.put_done_job([older, newer], brand="shoes")).get_data())
        folders = [n for n in zf.namelist() if n.endswith("/")]
        # Viewer sorts newest-first on load, so Select All numbers cards that way
        self.assertEqual(folders, ["2026-05-01 - shoes - STATIC - Acme_Co - n1/",
                                   "2026-01-01 - shoes - STATIC - Acme_Co - n2/"])

    def test_completed_job_with_no_ads_is_an_empty_zip(self):
        self.setUpNamespace()
        resp = self.export(self.put_done_job([]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(read_zip(resp.get_data())[0].namelist(), [])

    def test_export_leaves_job_and_json_result_unchanged(self):
        self.setUpNamespace(locked_cookies=api_tests.LOCKED_COOKIES)
        job_id = self.start_job()
        before = self.client.get(f"/api/v1/scrapes/{job_id}/result").get_json()
        self.export(job_id)
        self.assertEqual(self.client.get(f"/api/v1/scrapes/{job_id}/result").get_json(), before)

    def test_zip_carries_no_cookie_data(self):
        self.setUpNamespace(locked_cookies=api_tests.LOCKED_COOKIES)
        data = self.export(self.start_job()).get_data()
        self.assertNotIn(api_tests.SECRET_COOKIE_VALUE.encode(), data)
        self.assertNotIn(b"999999999", data)


# 2. Media goes through the shared safe-fetch boundary ──────────────────────

class TestExportMedia(ExportTestCase):

    def test_media_fetched_through_safe_fetch_media(self):
        self.setUpNamespace(locked_cookies=api_tests.LOCKED_COOKIES)
        real, seen = self.ns["safe_fetch_media"], []
        self.ns["safe_fetch_media"] = lambda url, kind: (seen.append((url, kind)), real(url, kind))[1]
        self.export(self.start_job())
        self.assertEqual(seen, [(IMG_1001, "image"), (THUMB_1002, "image"), (VID_1002, "video")])
        self.assertEqual(self.net.calls, [IMG_1001, THUMB_1002, VID_1002])

    def test_image_slice_skip_on_failure_and_numbering(self):
        urls = [f"https://scontent.xx.fbcdn.net/multi{i}.jpg" for i in range(1, 6)]
        net = media_tests.FakeNetwork({
            urls[0]: media_tests.ok("image/jpeg", b"one"),
            urls[1]: media_tests.ok("text/html", b"<html>"),  # policy failure → skipped
            urls[2]: media_tests.ok("image/png", b"three"),
            urls[3]: media_tests.ok("image/webp", b"four"),   # not png → .jpg, as in bulkZip
            urls[4]: media_tests.ok("image/jpeg", b"five"),   # 5th: beyond data-imgs' [:4]
        })
        self.setUpNamespace(network=net)
        raw = api_tests.make_ad("4004")
        raw["snapshot"]["images"] = [{"original_image_url": u} for u in urls]
        _, files = read_zip(self.export(self.put_done_job([raw])).get_data())
        folder = "2026-09-01 - 111111111 - STATIC - Acme_Co - 4004"
        images = {n.split("/", 1)[1]: d for n, d in files.items() if "/image" in n}
        self.assertEqual(images, {"image1.jpg": b"one", "image3.png": b"three", "image4.jpg": b"four"})
        self.assertEqual(net.calls, urls[:4])
        self.assertIn(f"{folder}/ad_copy.txt", files)

    def test_video_failure_writes_url_txt_like_browser(self):
        bad_redirect = "https://video.xx.fbcdn.net/redirects.mp4"
        offsite = "https://example.com/not-fbcdn.mp4"
        net = media_tests.FakeNetwork({bad_redirect: media_tests.redirect("https://example.com/x.mp4")})
        self.setUpNamespace(network=net)
        raw = api_tests.make_ad("5005", video=True)
        raw["snapshot"]["videos"] = [{"video_hd_url": bad_redirect}, {"video_hd_url": offsite}]
        _, files = read_zip(self.export(self.put_done_job([raw])).get_data())
        folder = "2026-09-01 - 111111111 - VIDEO - Acme_Co - 5005"
        self.assertEqual(files[f"{folder}/video1_url.txt"].decode(), f"/vid?u={urlquote(bad_redirect)}")
        self.assertEqual(files[f"{folder}/video2_url.txt"].decode(), f"/vid?u={urlquote(offsite)}")
        self.assertFalse(any(n.endswith(".mp4") for n in files))
        self.assertEqual(net.calls, [bad_redirect])  # off-site URL never requested

    def test_browser_slug_counts_utf16_code_units(self):
        self.setUpNamespace()
        slug = self.ns["_browser_slug"]
        self.assertEqual(slug("a🚀b-é"), "a__b__")
        self.assertEqual(slug("A" * 40), "A" * 30)
        self.assertEqual(slug("🚀" * 20), "_" * 30)


# 3. Non-completed jobs do not export ────────────────────────────────────────

class TestNonCompletedExport(ExportTestCase):

    def test_running_job_does_not_export(self):
        self.setUpNamespace(locked_cookies=api_tests.LOCKED_COOKIES, defer=True)
        job_id = self.start_job()
        self.ns["jobs"][job_id]["ads"] = [{"ad_id": "partial"}]
        resp = self.export(job_id)
        self.assertEqual(resp.status_code, 409)
        self.assertTrue(resp.is_json)
        data = resp.get_json()
        self.assertEqual(data["status"], "running")
        self.assertIs(data["completed"], False)
        self.assertEqual(data["error"]["code"], "not_completed")
        self.assertNotIn("partial", resp.get_data(as_text=True))
        self.assertEqual(self.net.calls, [])

        self.threads.run_deferred()
        resp = self.export(job_id)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/zip")

    def test_failed_job_does_not_export(self):
        self.setUpNamespace(locked_cookies=api_tests.LOCKED_COOKIES, viewer_error="viewer exploded")
        resp = self.export(self.start_job())
        self.assertEqual(resp.status_code, 409)
        data = resp.get_json()
        self.assertEqual(data["status"], "failed")
        self.assertIs(data["completed"], False)
        self.assertEqual(data["error"]["code"], "scrape_failed")
        self.assertIn("viewer exploded", data["error"]["message"])
        self.assertIsInstance(data["error"]["log_tail"], list)
        self.assertEqual(self.net.calls, [])

    def test_unknown_job_is_json_404(self):
        self.setUpNamespace()
        resp = self.export("nope1234")
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(resp.is_json)
        self.assertEqual(resp.get_json(), {"job_id": "nope1234",
                                           "error": {"code": "not_found", "message": "Unknown job_id"}})


# 4. Browser Download ZIP is unchanged ───────────────────────────────────────

# bulkZip() exactly as shipped when the export was written (app.py source,
# so JS braces are doubled). If this changes, update the export to match.
BULK_ZIP_SOURCE = r"""// ── Bulk ZIP download — one subfolder per ad with all files
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
"""


class TestBrowserZipUnchanged(unittest.TestCase):

    def test_bulk_zip_source_is_unchanged(self):
        lines = APP_SRC.splitlines()
        start = next(i for i, l in enumerate(lines) if l.startswith("// ── Bulk ZIP download"))
        end = next(j for j in range(start, len(lines)) if lines[j].startswith("// ── Lightbox"))
        self.assertEqual("\n".join(lines[start:end]).rstrip() + "\n", BULK_ZIP_SOURCE)

    def test_viewer_zip_wiring_is_unchanged(self):
        for snippet in (
            '<button class="sel-bar-btn primary" onclick="bulkZip()">⬇ Download ZIP</button>',
            '<button class="sel-bar-btn" onclick="selectAllVisible()">Select All</button>',
            'const KEYWORD = "{brand_slug}";',
            "sortCards('date_desc');",
            "if (by === 'date_desc')  return (b.dataset.date||'').localeCompare(a.dataset.date||'');",
            '<script src="https://cdnjs.cloudflare.com/ajax/libs/jszip/3.10.1/jszip.min.js"></script>',
        ):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, APP_SRC)


class _CardAttrs(HTMLParser):
    """Collects each rendered .card's (entity-decoded) attributes."""

    def __init__(self):
        super().__init__()
        self.cards = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div" and attrs.get("class") == "card":
            self.cards.append(attrs)


@unittest.skipIf(sys.version_info < (3, 12), "build_viewer needs Python 3.12+ (PEP 701 f-string)")
class TestExportMatchesRealViewerData(unittest.TestCase):
    """Renders the real build_viewer() and checks every data-* value bulkZip()
    reads against what the export derives from the retained ad_record()s."""

    def test_card_dataset_and_keyword_match_rendered_viewer(self):
        ns = {"re": re, "urlquote": urlquote, "urlparse": urlparse, "datetime": datetime}
        for pattern in (r"^def extract_urls\(", r"^def normalize_ad\(", r"^def ad_record\(",
                        r"^_JS_TRIM_CHARS = \(", r"^def _browser_slug\(", r"^def _browser_attr\(",
                        r"^def browser_card_dataset\("):
            exec(api_tests.extract_block(pattern), ns)  # noqa: S102
        exec(api_tests.extract_region("# ── Viewer builder", "# ── HTML templates"), ns)  # noqa: S102

        many_images = api_tests.make_ad("6006")
        many_images["snapshot"]["images"] = [
            {"original_image_url": f"https://scontent.xx.fbcdn.net/m{i},x.jpg"} for i in range(6)]
        raw_ads = [api_tests.make_ad("1001"), api_tests.make_ad("1002", video=True),
                   edge_case_raw_ad(), many_images]

        for brand in ("https://www.facebook.com/AcmeShoes?ref=x", "www.get-novaburn.com",
                      "running shoes", "111111111"):
            with self.subTest(brand=brand):
                html = ns["build_viewer"](brand, "US", raw_ads)
                keyword = re.search(r'const KEYWORD = "(.*)";', html).group(1)
                self.assertEqual(keyword, ns["viewer_brand_slug"](brand))

                parser = _CardAttrs()
                parser.feed(html)
                self.assertEqual(len(parser.cards), len(raw_ads))
                for attrs, raw in zip(parser.cards, raw_ads):
                    rec = ns["ad_record"](raw)
                    ds = ns["browser_card_dataset"](rec)
                    for key, value in ds.items():
                        self.assertEqual(attrs[f"data-{key}"], value, key)
                    self.assertEqual(attrs["data-imgs"].split(",") if attrs["data-imgs"] else [],
                                     [f"/img?u={urlquote(u)}" for u in rec["images"][:4]])
                    self.assertEqual(attrs["data-vids"].split(",") if attrs["data-vids"] else [],
                                     [f"/vid?u={urlquote(u)}" for u in rec["videos"][:3]])


if __name__ == "__main__":
    unittest.main()
