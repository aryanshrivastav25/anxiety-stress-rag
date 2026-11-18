#!/usr/bin/env python3
"""
collect_kb.py - staged, auditable knowledge-base collector for the anxiety/stress RAG project.

Run from the folder that CONTAINS rag_knowledge_base/ (or pass --root).

Stages (run in this order):
  fetch           Download the 10 Priority-1 documents (+ WHO PDF, + NIMH fact-sheet PDF) and register them
  extract         HTML/PDF -> extracted_text/ and cleaned_documents/ ; validates extraction
  links           Level-1 (or --level 2) link audit -> metadata/link_audit.csv  (NO downloads)
  fetch-approved  Download only link_audit rows where you typed  yes  in the `approve` column
  add             Add one specific URL by hand (Priority 2 / reference-only manuals)
  analyze         Duplicate/overlap analysis + topic coverage matrix (heuristic, needs your review)
  report          Generate collection / source-quality / coverage-gap reports + README (if empty)
  all             fetch -> extract -> links(1) -> analyze -> report

Design rules: polite (2s delay), obeys robots.txt, never bypasses 403/paywalls (logs failure instead),
never invents metadata ("Unknown"/"Not specified"), never modifies original files.
"""
import argparse, csv, hashlib, re, sys, time
from datetime import date
from itertools import combinations
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup

ROOT = Path("rag_knowledge_base")
FORCE = False
TODAY = date.today().isoformat()
UA = "Mozilla/5.0 (compatible; AcademicKBCollector/1.0; B.Tech student research project)"

REG_FIELDS = ["document_id", "title", "organization", "source_url", "original_file_path", "file_format",
              "publication_date", "last_updated_date", "date_accessed", "language", "country_or_region",
              "primary_topic", "secondary_topics", "intended_audience", "source_type", "authority_level",
              "licence", "attribution_requirements", "collection_priority", "parent_document_id",
              "crawl_level", "overlap_status", "inclusion_reason", "extraction_status"]
LINK_FIELDS = ["parent_document_id", "linked_url", "linked_page_title", "organization", "crawl_level",
               "relevance", "decision", "decision_reason", "downloaded_document_id", "approve"]
EXCL_FIELDS = ["url", "title", "organization", "category", "reason", "date_logged"]

# ----------------------------------------------------------------------------- Priority 1 seeds
SEEDS = [
    dict(id="WHO_001", org="WHO", folder="who", stem="WHO_anxiety_disorders", title="Anxiety disorders (WHO fact sheet)",
         url="https://www.who.int/news-room/fact-sheets/detail/anxiety-disorders/", topic="Anxiety disorders", sec="symptoms; treatment overview; prevalence"),
    dict(id="WHO_002", org="WHO", folder="who", stem="WHO_stress_questions_answers", title="Stress - Questions and Answers (WHO)",
         url="https://www.who.int/news-room/questions-and-answers/item/stress", topic="Stress", sec="coping; definition"),
    dict(id="WHO_003", org="WHO", folder="who", stem="WHO_doing_what_matters_stress", title="Doing What Matters in Times of Stress (WHO)",
         url="https://www.who.int/publications/i/item/9789240003927", topic="Coping with stress", sec="grounding; self-help", pdf="primary"),
    dict(id="NIMH_001", org="NIMH", folder="nimh", stem="NIMH_anxiety_disorders", title="Anxiety Disorders (NIMH)",
         url="https://www.nimh.nih.gov/health/topics/anxiety-disorders", topic="Anxiety disorders", sec="symptoms; treatment; help-seeking"),
    dict(id="NIMH_002", org="NIMH", folder="nimh", stem="NIMH_generalized_anxiety_disorder", title="Generalized Anxiety Disorder (NIMH)",
         url="https://www.nimh.nih.gov/health/publications/generalized-anxiety-disorder-gad", topic="Generalized anxiety / worry", sec="symptoms"),
    dict(id="NIMH_003", org="NIMH", folder="nimh", stem="NIMH_panic_disorder", title="Panic Disorder (NIMH)",
         url="https://www.nimh.nih.gov/health/publications/panic-disorder-when-fear-overwhelms", topic="Panic attacks / panic disorder", sec="symptoms"),
    dict(id="NIMH_004", org="NIMH", folder="nimh", stem="NIMH_social_anxiety_disorder", title="Social Anxiety Disorder (NIMH)",
         url="https://www.nimh.nih.gov/health/publications/social-anxiety-disorder-more-than-just-shyness", topic="Social anxiety", sec="symptoms"),
    dict(id="NIMH_005", org="NIMH", folder="nimh", stem="NIMH_phobias", title="Phobias and Phobia-Related Disorders (NIMH)",
         url="https://www.nimh.nih.gov/health/publications/phobias-and-phobia-related-disorders", topic="Phobias / avoidance", sec="symptoms"),
    dict(id="NIMH_006", org="NIMH", folder="nimh", stem="NIMH_caring_for_mental_health", title="Caring for Your Mental Health (NIMH)",
         url="https://www.nimh.nih.gov/health/topics/caring-for-your-mental-health", topic="Self-care / well-being", sec="sleep; exercise; social support; help-seeking"),
    dict(id="NIMH_007", org="NIMH", folder="nimh", stem="NIMH_im_so_stressed_out", title="I'm So Stressed Out! Fact Sheet (NIMH)",
         url="https://www.nimh.nih.gov/health/publications/so-stressed-out-fact-sheet", topic="Stress", sec="coping; stress vs anxiety", pdf="secondary"),
]

DOMAIN_ORG = {  # host suffix -> (org label, raw folder)
    "telemanas.mohfw.gov.in": ("TELEMANAS", "india_government"), "mohfw.gov.in": ("MOHFW", "india_government"),
    "iris.who.int": ("WHO", "who"), "who.int": ("WHO", "who"), "nimh.nih.gov": ("NIMH", "nimh"),
    "nhs.uk": ("NHS", "nhs"), "medlineplus.gov": ("MEDLINEPLUS", "medlineplus"), "samhsa.gov": ("SAMHSA", "samhsa")}
AUTH = {"WHO": ("High - intergovernmental health agency", "Global"), "NIMH": ("High - US federal research institute (NIH)", "United States"),
        "NHS": ("High - UK national health service", "United Kingdom"), "MOHFW": ("High - Government of India", "India"),
        "TELEMANAS": ("High - Government of India", "India"), "MEDLINEPLUS": ("High - US National Library of Medicine", "United States"),
        "SAMHSA": ("High - US federal agency", "United States")}

# ----------------------------------------------------------------------------- small helpers
def P(*a): return ROOT.joinpath(*a)
def M(name): return P("metadata", name)
def slug(s): return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:60] or "doc"

def read_csv(p):
    if not p.exists() or p.stat().st_size == 0: return []
    with open(p, encoding="utf-8-sig", newline="") as f: return list(csv.DictReader(f))

def write_csv(p, fields, rows):
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore"); w.writeheader(); w.writerows(rows)

def load_reg(): return {r["document_id"]: r for r in read_csv(M("source_registry.csv"))}
def save_reg(reg): write_csv(M("source_registry.csv"), REG_FIELDS, [reg[k] for k in sorted(reg)])

def org_for(url):
    host = urlparse(url).netloc.lower()
    for suf, v in sorted(DOMAIN_ORG.items(), key=lambda kv: -len(kv[0])):
        if host == suf or host.endswith("." + suf): return v
    return ("OTHER", "other_authoritative_sources")

def extracted_dir(folder): return P("extracted_text", folder if folder in ("who", "nimh") else "additional_sources")

def next_id(reg, prefix):
    n = [int(m.group(1)) for k in reg if (m := re.match(rf"^{prefix}_(\d{{3}})(?:_|$)", k))]
    return f"{prefix}_{(max(n) + 1 if n else 1):03d}"

def log_exclusion(url, title, org, category, reason):
    rows = read_csv(M("exclusions.csv"))
    if any(r["url"] == url and r["category"] == category for r in rows): return
    rows.append(dict(url=url, title=title, organization=org, category=category, reason=reason, date_logged=TODAY))
    write_csv(M("exclusions.csv"), EXCL_FIELDS, rows)

# ----------------------------------------------------------------------------- polite fetcher
class Fetcher:
    def __init__(self, delay=2.0):
        self.s = requests.Session(); self.s.headers.update({"User-Agent": UA, "Accept-Language": "en"})
        self.delay, self.last, self.robots = delay, 0.0, {}

    def allowed(self, url):
        p = urlparse(url); base = f"{p.scheme}://{p.netloc}"
        if base not in self.robots:
            rp = robotparser.RobotFileParser()
            try:
                r = self.s.get(base + "/robots.txt", timeout=20)
                if r.status_code in (401, 403): rp.disallow_all = True
                elif r.status_code == 200: rp.parse(r.text.splitlines())
                else: rp.parse([])
            except requests.RequestException: rp.parse([])
            self.robots[base] = rp
        return self.robots[base].can_fetch(UA, url)

    def get(self, url):
        if not self.allowed(url): return None, "Blocked by robots.txt (not circumvented)"
        wait = self.delay - (time.time() - self.last)
        if wait > 0: time.sleep(wait)
        try: r = self.s.get(url, timeout=60, allow_redirects=True)
        except requests.RequestException as e: return None, f"Request error: {e}"
        finally: self.last = time.time()
        if r.status_code != 200: return None, f"HTTP {r.status_code}"
        return r, None

# ----------------------------------------------------------------------------- metadata detection (never guesses)
DATE_RX = r"(\d{1,2} [A-Z][a-z]+ \d{4}|[A-Z][a-z]+ \d{1,2}, \d{4}|[A-Z][a-z]+ \d{4})"
LIC_RX = re.compile(r"[^.\n]{0,160}(Creative Commons|CC BY|public domain|licen[cs]e|copyright|reproduc)[^.\n]{0,200}", re.I)

def page_meta(html):
    soup = BeautifulSoup(html, "html.parser"); text = soup.get_text(" ", strip=True); out = {}
    t = soup.find("meta", property="og:title"); out["title"] = (t.get("content") if t and t.get("content") else (soup.title.string if soup.title and soup.title.string else "")).strip()
    def mt(*names):
        for n in names:
            tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
            if tag and tag.get("content"): return tag["content"][:10] if re.match(r"\d{4}-\d{2}-\d{2}", tag["content"]) else tag["content"]
    m = re.search(r"Last (?:Reviewed|Updated|updated|modified)[:\s]+" + DATE_RX, text)
    out["last_updated"] = mt("article:modified_time", "dcterms.modified", "last-modified") or (m.group(1) if m else "Unknown")
    m = re.search(r"(?:Publication date|Published|Date published)[:\s]+" + DATE_RX, text)
    out["published"] = mt("article:published_time", "dcterms.issued", "dcterms.created") or (m.group(1) if m else "Unknown")
    m = LIC_RX.search(text)
    out["licence"] = ("Detected on page (VERIFY MANUALLY): " + re.sub(r"\s+", " ", m.group(0)).strip()) if m else "Not specified"
    return out

def pdf_licence(path):
    try:
        import fitz
        d = fitz.open(path); txt = " ".join(d[i].get_text() for i in range(min(4, len(d))))
        m = LIC_RX.search(txt)
        return ("Detected in PDF (VERIFY MANUALLY): " + re.sub(r"\s+", " ", m.group(0)).strip()) if m else "Not specified"
    except Exception: return "Not specified"

# ----------------------------------------------------------------------------- acquire + register
def acquire(f, reg, url, org, folder, title, stem, doc_id, *, priority="1", parent="", level=0, topic="Unknown",
            sec="", audience="General public", reason="", overlap="Pending analysis", inherit=None, ref_dir=None, source_type="Official web page / publication"):
    base_dir = ref_dir or P("raw_documents", folder); base_dir.mkdir(parents=True, exist_ok=True)
    existing = next((base_dir / (stem + e) for e in (".html", ".pdf") if (base_dir / (stem + e)).exists()), None)
    if existing and not FORCE:
        data, dest, ext = existing.read_bytes(), existing, existing.suffix[1:]; print(f"  [cached] {dest.name}")
    else:
        r, err = f.get(url)
        if err:
            print(f"  [FAILED] {url} -> {err}\n           (If it's a 403, open it in your browser, save the page/PDF as "
                  f"{base_dir / (stem + '.html')} (or .pdf) and re-run; the script will pick it up.)")
            log_exclusion(url, title, org, "Excluded — inaccessible", err); return None, None, None
        ct = r.headers.get("content-type", "").lower()
        ext = "pdf" if ("pdf" in ct or r.content[:4] == b"%PDF") else "html"
        data, dest = r.content, base_dir / f"{stem}.{ext}"
        if ext == "pdf" and data[:4] != b"%PDF": log_exclusion(url, title, org, "Excluded — inaccessible", "Not a valid PDF"); return None, None, None
        dest.write_bytes(data); print(f"  [saved]  {dest}")
    meta = page_meta(data.decode("utf-8", "replace")) if ext == "html" else {}
    inherit = inherit or {}
    auth, country = AUTH.get(org, ("Needs manual assessment", "Unknown"))
    lic = meta.get("licence") or inherit.get("licence") or (pdf_licence(dest) if ext == "pdf" else "Not specified")
    row = {k: "Unknown" for k in REG_FIELDS}
    row.update(document_id=doc_id, title=title, organization=org, source_url=url, original_file_path=str(dest), file_format=ext.upper(),
               publication_date=meta.get("published") or inherit.get("published") or "Unknown",
               last_updated_date=meta.get("last_updated") or inherit.get("last_updated") or "Unknown",
               date_accessed=TODAY, language="English", country_or_region=country, primary_topic=topic, secondary_topics=sec,
               intended_audience=audience, source_type=source_type, authority_level=auth, licence=lic,
               attribution_requirements="Not specified (verify on source site)", collection_priority=priority,
               parent_document_id=parent, crawl_level=str(level), overlap_status=overlap, inclusion_reason=reason or "Approved Priority 1 source",
               extraction_status="Pending")
    reg[doc_id] = row; save_reg(reg)
    return row, data, ext

def find_pdf_link(html, base_url):
    soup = BeautifulSoup(html, "html.parser"); cands = []
    for a in soup.find_all("a", href=True):
        h = urljoin(base_url, a["href"]); lo = (h + " " + a.get_text(" ")).lower()
        if ".pdf" in h.lower() or "download" in lo:
            score = (3 if ".pdf" in h.lower() else 0) + (2 if "eng" in lo or "english" in lo else 0) + (1 if "download" in lo else 0)
            if any(x in lo for x in ("-fre", "-spa", "-ara", "-rus", "-chi", "french", "spanish", "arabic", "russian", "chinese")): score -= 5
            cands.append((score, h))
    cands.sort(reverse=True)
    return cands[0][1] if cands and cands[0][0] >= 3 else None

# ----------------------------------------------------------------------------- stage: fetch
def cmd_fetch(a):
    f, reg = Fetcher(a.delay), load_reg()
    for s in SEEDS:
        print(f"\n{s['id']}: {s['title']}")
        pdf_primary = s.get("pdf") == "primary"
        page_stem = s["stem"] + ("_landing" if pdf_primary else "")
        page_id = s["id"] + ("_PAGE" if pdf_primary else "")
        row, data, ext = acquire(f, reg, s["url"], s["org"], s["folder"], s["title"] + (" - landing page (metadata only)" if pdf_primary else ""),
                                 page_stem, page_id, topic=s["topic"], sec=s["sec"],
                                 overlap="Metadata page for " + s["id"] if pdf_primary else "Pending analysis",
                                 reason="Landing page kept for date/licence metadata" if pdf_primary else "")
        if not row: continue
        if s.get("pdf") and data and ext == "html":
            pdf_url = find_pdf_link(data.decode("utf-8", "replace"), s["url"])
            if not pdf_url: print("  [WARN] no PDF link found on page - download manually and re-run"); log_exclusion(s["url"], s["title"], s["org"], "Review later", "PDF link not auto-detected"); continue
            print(f"  PDF candidate: {pdf_url}")
            pid = s["id"] if pdf_primary else s["id"] + "_PDF"
            acquire(f, reg, pdf_url, s["org"], s["folder"], s["title"] + " (PDF)", s["stem"], pid, topic=s["topic"], sec=s["sec"],
                    overlap="Pending analysis" if pdf_primary else "Alternate format of " + s["id"],
                    inherit=dict(published=row["publication_date"], last_updated=row["last_updated_date"], licence=row["licence"]),
                    reason="Official English PDF" if pdf_primary else "Official PDF version of the fact sheet", source_type="Official PDF publication")
    ok = [s["id"] for s in SEEDS if s["id"] in reg]
    print(f"\nPriority-1 registered: {len(ok)}/10 ->", ok)
    missing = [s["id"] for s in SEEDS if s["id"] not in reg]
    if missing: print("MISSING (see exclusions.csv):", missing)

# ----------------------------------------------------------------------------- stage: extract / clean
def extract_html(path, url):
    html = path.read_text(encoding="utf-8", errors="replace")
    try:
        import trafilatura
        try: txt = trafilatura.extract(html, url=url, include_tables=True, include_comments=False, output_format="markdown")
        except Exception: txt = trafilatura.extract(html, url=url, include_tables=True)
        if txt and len(txt) > 500: return txt, "OK (trafilatura)"
    except ImportError: pass
    soup = main_soup(html)
    for t in soup(["script", "style", "nav", "header", "footer", "aside", "form"]): t.decompose()
    lines = []
    for el in soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "tr"]):
        t = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
        if not t: continue
        lines.append("#" * int(el.name[1]) + " " + t if el.name[0] == "h" and len(el.name) == 2 else ("- " + t if el.name == "li" else t))
    txt = "\n".join(lines)
    return txt, ("OK (bs4 fallback - review)" if len(txt) > 500 else "CHECK_MANUALLY: very little text extracted")

def extract_pdf(path):
    import fitz
    d = fitz.open(path); parts, total = [], 0
    for i, pg in enumerate(d, 1):
        t = pg.get_text("text"); total += len(t.strip()); parts.append(f"\n\n=== PAGE {i} ===\n{t}")
    txt = "".join(parts); n = max(len(d), 1)
    if total / n < 80: return txt, "NEEDS_OCR (little/no embedded text)"
    if txt.count("\ufffd") / max(len(txt), 1) > 0.01: return txt, "CHECK_MANUALLY: garbled characters"
    return txt, f"OK ({len(d)} pages, PyMuPDF; check column/heading order manually)"

BOILER = re.compile(r"^(skip to (main )?content|menu|search|share|print|cookie|accept all|follow us|back to top)\b", re.I)
def clean_text(txt):
    out = [re.sub(r"[ \t]+", " ", l).rstrip() for l in txt.splitlines() if not BOILER.match(l.strip())]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"

def main_soup(html):
    soup = BeautifulSoup(html, "html.parser")
    return soup.select_one("main, article, [role=main], #main-content") or soup.body or soup

def cmd_extract(a):
    reg = load_reg()
    for did, r in sorted(reg.items()):
        if did.endswith("_PAGE"): r["extraction_status"] = "Skipped (metadata-only landing page)"; continue
        path = Path(r["original_file_path"])
        if not path.exists(): r["extraction_status"] = "FAILED: original file missing"; continue
        folder = path.parent.name
        try: txt, status = extract_html(path, r["source_url"]) if path.suffix == ".html" else extract_pdf(path)
        except Exception as e: txt, status = "", f"FAILED: {e}"
        header = f"SOURCE_URL: {r['source_url']}\nTITLE: {r['title']}\nORGANIZATION: {r['organization']}\nDATE_ACCESSED: {r['date_accessed']}\n\n"
        ed = extracted_dir(folder); ed.mkdir(parents=True, exist_ok=True)
        (ed / (path.stem + ".txt")).write_text(header + txt, encoding="utf-8")
        P("cleaned_documents").mkdir(exist_ok=True)
        (P("cleaned_documents") / (path.stem + ".txt")).write_text(header + clean_text(txt), encoding="utf-8")
        r["extraction_status"] = status; print(f"{did:12s} {status}")
    save_reg(reg)

# ----------------------------------------------------------------------------- stage: link audit
EXCL_IRREL = re.compile(r"/(news|press|media|events?|careers?|jobs|about|contact|search|login|subscribe|privacy|accessibility|sitemap|funding|grants?|research-priorities|clinical-trials|outreach|multimedia|social)(/|$|\?)|^mailto:|^tel:|facebook|twitter|x\.com|linkedin|youtube|instagram|\.(jpg|png|gif|svg|zip|mp4|mp3)(\?|$)", re.I)
CLINICAL = re.compile(r"medication|prescri|training|facilitator|manual|clinician|provider|professional-education|guideline|psychotherap|clinical", re.I)
COUNTRY = re.compile(r"988|lifeline|crisis-text|911", re.I)
RELEVANT = re.compile(r"anxi|stress|worr|panic|phobi|coping|well-?being|mental-health|caring-for|sleep|self-care|help|support|relax|mindful|child|teen|adolescen|older-adult|school|exam|burnout|trauma|ocd|obsessive", re.I)
ALLOWED_HOSTS = tuple(DOMAIN_ORG)

def classify(url, text):
    blob = url + " " + text; host = urlparse(url).netloc.lower()
    if not any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS): return "Excluded — irrelevant", "Domain not in authoritative allow-list", 0
    if EXCL_IRREL.search(url): return "Excluded — irrelevant", "Navigation/news/admin/social/media link", 0
    if COUNTRY.search(blob): return "Excluded — country-specific", "US-specific service (not applicable to Indian users)", 0
    score = len(set(m.lower() for m in RELEVANT.findall(blob)))
    if CLINICAL.search(blob): return "Excluded — clinical/training material", "Treatment/medication/professional material outside chatbot scope (override manually if public-facing overview needed)", score
    if score == 0: return "Excluded — irrelevant", "No anxiety/stress/coping/help-seeking keywords", 0
    return "Review later", "Keyword-relevant; needs human judgement on novelty & audience", score

def cmd_links(a):
    reg, audit = load_reg(), read_csv(M("link_audit.csv"))
    seen = {r["linked_url"] for r in audit} | {urldefrag(r["source_url"])[0].rstrip("/") for r in reg.values()}
    f = Fetcher(a.delay); level = a.level
    for did, r in sorted(reg.items()):
        if r["crawl_level"] != str(level - 1) or r["file_format"] != "HTML": continue
        path = Path(r["original_file_path"])
        if not path.exists(): continue
        soup = main_soup(path.read_text(encoding="utf-8", errors="replace"))
        for t in soup(["nav", "header", "footer", "aside"]): t.decompose()
        n = 0
        for an in soup.find_all("a", href=True):
            url = urldefrag(urljoin(r["source_url"], an["href"]))[0]
            if not url.startswith("http"): continue
            key = url.rstrip("/"); text = re.sub(r"\s+", " ", an.get_text(" ", strip=True))
            if key in seen:
                audit.append(dict(parent_document_id=did, linked_url=url, linked_page_title=text, organization=org_for(url)[0], crawl_level=level,
                                  relevance="n/a", decision="Excluded — duplicate", decision_reason="URL already registered or already audited", downloaded_document_id="", approve=""))
                continue
            seen.add(key); dec, why, score = classify(url, text); n += 1
            audit.append(dict(parent_document_id=did, linked_url=url, linked_page_title=text, organization=org_for(url)[0], crawl_level=level,
                              relevance=f"keyword_score={score}", decision=dec, decision_reason=why, downloaded_document_id="", approve=""))
            if dec.startswith("Excluded"): log_exclusion(url, text, org_for(url)[0], dec, why)
        print(f"{did}: {n} new links audited")
    write_csv(M("link_audit.csv"), LINK_FIELDS, audit)
    rl = sum(1 for r in audit if r["decision"] == "Review later")
    print(f"\n{rl} rows are 'Review later'. Open metadata/link_audit.csv, open each URL, and type 'yes' in the `approve` column\n"
          f"ONLY for pages that add new, public-facing, in-scope information. Then run: python collect_kb.py fetch-approved --level {level}")

def cmd_fetch_approved(a):
    reg, audit, f = load_reg(), read_csv(M("link_audit.csv")), Fetcher(a.delay)
    for r in audit:
        if r["approve"].strip().lower() not in ("y", "yes") or r["downloaded_document_id"]: continue
        url = r["linked_url"]; org, folder = org_for(url)
        did = next_id(reg, org); title = r["linked_page_title"] or url
        stem = f"{org}_{slug(title if len(title) > 3 else urlparse(url).path.split('/')[-1])}"
        print(f"\n{did}: {title}")
        row, _, _ = acquire(f, reg, url, org, folder, title, stem, did, priority="1-linked", parent=r["parent_document_id"],
                            level=int(r["crawl_level"]), topic="Unknown (fill in)", reason=f"Approved from link audit: {r['decision_reason']}")
        if row: r["decision"], r["downloaded_document_id"] = "Downloaded", did
    write_csv(M("link_audit.csv"), LINK_FIELDS, audit)
    print("\nNow run: extract, then analyze.  Edit primary_topic/overlap_status in source_registry.csv by hand.")

def cmd_add(a):
    reg, f = load_reg(), Fetcher(a.delay)
    org, folder = (a.org, org_for(a.url)[1]) if a.org else org_for(a.url)
    did = a.doc_id or next_id(reg, org); stem = a.stem or f"{org}_{slug(a.title)}"
    ref = P("reference_only", "intervention_manuals") if a.reference_only else None
    row, _, _ = acquire(f, reg, a.url, org, folder, a.title, stem, did, priority=str(a.priority), level=a.level, parent=a.parent,
                        topic=a.topic, reason=a.reason or ("Reference-only: NOT part of core chatbot KB" if a.reference_only else "Priority 2 addition - justify in registry"),
                        audience="Professionals / trained facilitators (reference only)" if a.reference_only else "General public", ref_dir=ref)
    print("OK" if row else "FAILED (see exclusions.csv)")

# ----------------------------------------------------------------------------- stage: analyze (duplicates + coverage)
TOPICS = {
    "1 Definition of anxiety": r"anxiety is|what is anxiety|anxiety disorders? (are|is)",
    "2 Definition of stress": r"stress is|what is stress|stress (can be|refers)",
    "3 Stress vs anxiety": r"difference between stress and anxiety|stress and anxiety|anxiety and stress|stress.{0,40}anxiety",
    "4 Symptoms of anxiety": r"symptoms",
    "5 Physical/emotional/behavioural effects": r"physical|emotional|behaviou?ral|racing heart|muscle tension",
    "6 Triggers/contributing factors": r"trigger|risk factors?|causes?",
    "7 GAD / excessive worry": r"generali[sz]ed anxiety|excessive(ly)? worr|worry",
    "8 Panic attacks/disorder": r"panic attack|panic disorder",
    "9 Social anxiety": r"social anxiety",
    "10 Phobias/avoidance": r"phobia|avoidance|avoid",
    "11 Academic/exam stress": r"exam|academic|student|school",
    "12 Work-related stress": r"work-related|workplace|burnout|job stress|at work",
    "13 Coping/self-help": r"coping|self-help|self-care|cope",
    "14 Grounding/relaxation": r"grounding|breathing|relaxation|mindfulness|relax",
    "15 Sleep/routines": r"sleep|routine",
    "16 Exercise/lifestyle": r"exercise|physical activity|caffeine|alcohol|healthy diet|nutrition",
    "17 Social support": r"social support|friends?|family|connect(ed|ion)?",
    "18 When to seek professional help": r"seek help|professional help|health care provider|doctor|find help|mental health professional",
    "19 Age-specific": r"children|adolescen|teen|older adults|young people|youth",
    "20 India-specific resources": r"tele-?manas|india|nimhans|14416"}

def shingles(t, k=6):
    w = re.findall(r"\w+", t.lower()); return {" ".join(w[i:i + k]) for i in range(max(len(w) - k + 1, 0))}

def read_extracted(r):
    p = Path(r["original_file_path"]); folder = p.parent.name
    f = extracted_dir(folder) / (p.stem + ".txt")
    return f.read_text(encoding="utf-8", errors="replace").split("\n\n", 1)[-1] if f.exists() else None

def cmd_analyze(a):
    reg = load_reg(); docs = {k: read_extracted(v) for k, v in reg.items() if not k.endswith("_PAGE")}
    docs = {k: v for k, v in docs.items() if v and len(v) > 200}
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    ids = sorted(docs); rows = []
    if len(ids) > 1:
        sim = cosine_similarity(TfidfVectorizer(stop_words="english").fit_transform([docs[i] for i in ids]))
        sh = {i: shingles(docs[i]) for i in ids}
        for (i, x), (j, y) in combinations(enumerate(ids), 2):
            inter = len(sh[x] & sh[y]); cont = inter / max(min(len(sh[x]), len(sh[y])), 1)
            exact = hashlib.sha256(re.sub(r"\s+", " ", docs[x]).encode()).hexdigest() == hashlib.sha256(re.sub(r"\s+", " ", docs[y]).encode()).hexdigest()
            alt = x.split("_PDF")[0] == y.split("_PDF")[0]
            sug = ("Duplicate" if exact or cont >= 0.9 else "Partially overlapping" if cont >= 0.3 or sim[i][j] >= 0.6 else "Complementary/Unique")
            rows.append(dict(doc_a=x, doc_b=y, cosine_similarity=f"{sim[i][j]:.3f}", shingle_containment=f"{cont:.3f}", exact_duplicate=exact,
                             suggested_status=sug, note=("Same item in alternate formats (HTML vs PDF)" if alt else "Heuristic only - read both documents, then decide"), final_decision=""))
    rows.sort(key=lambda r: -float(r["shingle_containment"]))
    write_csv(M("duplicate_analysis.csv"), ["doc_a", "doc_b", "cosine_similarity", "shingle_containment", "exact_duplicate", "suggested_status", "note", "final_decision"], rows)
    print(f"duplicate_analysis.csv: {len(rows)} pairs")
    cov = []
    for t, rx in TOPICS.items():
        hits = {k: len(re.findall(rx, v, re.I)) for k, v in docs.items()}; covering = {k: n for k, n in hits.items() if n >= 2}
        s = "missing" if not covering else "strong" if len(covering) >= 3 or max(covering.values()) >= 15 else "moderate" if len(covering) >= 2 or max(covering.values()) >= 6 else "weak"
        cov.append(dict(topic=t, covered="Yes" if covering else "No", documents=";".join(sorted(covering, key=covering.get, reverse=True)), coverage_strength=s,
                        additional_source_needed="Likely" if s in ("weak", "missing") else "Probably not", note="Keyword heuristic - verify by reading"))
    write_csv(M("topic_coverage.csv"), ["topic", "covered", "documents", "coverage_strength", "additional_source_needed", "note"], cov)
    for c in cov: print(f"{c['topic']:45s} {c['coverage_strength']}")

# ----------------------------------------------------------------------------- stage: report
def md_table(rows, cols):
    return "\n".join(["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)] + ["| " + " | ".join(str(r.get(c, "")).replace("|", "/")[:90] for c in cols) + " |" for r in rows])

def cmd_report(a):
    reg = list(load_reg().values()); excl = read_csv(M("exclusions.csv")); cov = read_csv(M("topic_coverage.csv")); dup = read_csv(M("duplicate_analysis.csv"))
    (P("reports") / "collection_report.md").write_text(
        f"# Collection report\nGenerated {TODAY}\n\n## Downloaded documents ({len(reg)})\n" + md_table(reg, ["document_id", "organization", "file_format", "source_url", "original_file_path", "extraction_status"]) +
        f"\n\n## Exclusions / failures ({len(excl)})\n" + md_table(excl[:200], ["url", "category", "reason"]) +
        f"\n\n## Duplicate/overlap pairs flagged (top 25)\n" + md_table(dup[:25], ["doc_a", "doc_b", "shingle_containment", "suggested_status", "final_decision"]) +
        "\n\n## Priority-1 status\n" + "\n".join(f"- {s['id']}: {'OK' if s['id'] in {r['document_id'] for r in reg} else 'MISSING'}" for s in SEEDS) + "\n", encoding="utf-8")
    (P("reports") / "source_quality_report.md").write_text(
        f"# Source quality report\nGenerated {TODAY}. Licence/date columns are auto-detected - VERIFY MANUALLY before redistribution.\n\n" +
        md_table(reg, ["document_id", "organization", "authority_level", "publication_date", "last_updated_date", "intended_audience", "licence"]) + "\n\n## Manual review notes\n_(write your assessment of accuracy, recency, audience, licensing here)_\n", encoding="utf-8")
    gaps = [c for c in cov if c["coverage_strength"] in ("weak", "missing")]
    (P("reports") / "coverage_gap_report.md").write_text(
        f"# Coverage gap report\nGenerated {TODAY}. Heuristic keyword matching - confirm by reading.\n\n## Full matrix\n" + md_table(cov, ["topic", "coverage_strength", "documents"]) +
        "\n\n## Weak / missing topics\n" + ("\n".join(f"- {g['topic']} ({g['coverage_strength']})" for g in gaps) or "None") + "\n\n## Recommended Priority-2 sources\n_(fill in after reviewing NHS, MoHFW, Tele-MANAS, MedlinePlus, SAMHSA for the gaps above)_\n", encoding="utf-8")
    rd = P("README.md")
    if not rd.exists() or rd.stat().st_size < 50:
        rd.write_text(f"# RAG Knowledge Base - Anxiety & Stress Support\n\nCollected {TODAY} with collect_kb.py.\n\n## Method\nPriority-1 seeds -> controlled link audit (levels 1-2, human-approved) -> Priority-2 by hand (`add`).\n\n"
                      "## Folders\nraw_documents (untouched originals) / extracted_text / cleaned_documents / metadata / reports / reference_only.\n\n"
                      "## Naming\nORG_topic.ext, IDs ORG_###. HTML+PDF of one item share a base ID (PDF gets _PDF unless it is the primary).\n\n"
                      "## Known limitations\nLicence/date fields are auto-detected and must be verified; overlap and coverage are heuristics; no chunking/embeddings yet.\n\n"
                      "## Update\n`python collect_kb.py all --force`, then review link_audit.csv.\n", encoding="utf-8")
    print("Reports written to", P("reports"))

# ----------------------------------------------------------------------------- main
def main():
    global ROOT, FORCE
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default="rag_knowledge_base"); ap.add_argument("--delay", type=float, default=2.0); ap.add_argument("--force", action="store_true", help="re-download existing files")
    sp = ap.add_subparsers(dest="cmd", required=True)
    for n in ("fetch", "extract", "analyze", "report", "all"): sp.add_parser(n)
    for n in ("links", "fetch-approved"): sp.add_parser(n).add_argument("--level", type=int, default=1, choices=[1, 2])
    ad = sp.add_parser("add"); ad.add_argument("--url", required=True); ad.add_argument("--title", required=True); ad.add_argument("--org"); ad.add_argument("--stem")
    ad.add_argument("--doc-id"); ad.add_argument("--topic", default="Unknown"); ad.add_argument("--reason"); ad.add_argument("--priority", type=int, default=2)
    ad.add_argument("--level", type=int, default=0); ad.add_argument("--parent", default=""); ad.add_argument("--reference-only", action="store_true")
    a = ap.parse_args(); ROOT, FORCE = Path(a.root), a.force
    if not ROOT.exists(): sys.exit(f"{ROOT} not found - run from the folder containing it or pass --root")
    for d in ("metadata", "reports", "cleaned_documents"): (ROOT / d).mkdir(exist_ok=True)
    if a.cmd == "all":
        a.level = 1; [fn(a) for fn in (cmd_fetch, cmd_extract, cmd_links, cmd_analyze, cmd_report)]
    else: {"fetch": cmd_fetch, "extract": cmd_extract, "links": cmd_links, "fetch-approved": cmd_fetch_approved, "add": cmd_add,
           "analyze": cmd_analyze, "report": cmd_report}[a.cmd](a)

if __name__ == "__main__":
    main()