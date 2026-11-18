#!/usr/bin/env python3
"""Step 1+2: inspect the collection, decide which documents are eligible for core RAG ingestion.

Outputs:
  reports/document_validation_report.md
  metadata/ingestion_manifest.csv

Inclusion authority = source_registry.csv + your manual decisions:
  * metadata/duplicate_analysis.csv  -> `final_decision` column. Recognised text:
        "exclude NIMH_007_PDF" / "drop WHO_003_PAGE"   (excludes that document id)
        "exclude_b" / "exclude b"                      (excludes doc_b of that row);  "exclude_a" likewise
  * metadata/ingestion_overrides.csv (optional) -> columns: document_id,action,reason   action = include | exclude
Nothing is guessed: anything unresolved is listed in the report.
"""
import argparse, re, shutil
from pathlib import Path
from kb_common import *

REQUIRED_META = ["title", "organization", "source_url", "primary_topic", "language"]
NICE_META = ["publication_date", "last_updated_date", "licence"]
MANIFEST_FIELDS = ["document_id", "title", "organization", "source_url", "cleaned_text_path", "inclusion_status", "exclusion_reason"]
MIN_CHARS = 300


def parse_dup_decisions(rows):
    """Returns ({doc_id: reason}, [unresolved Duplicate pairs])."""
    excl, unresolved = {}, []
    for r in rows:
        dec = (r.get("final_decision") or "").strip()
        a, b = r["doc_a"], r["doc_b"]
        if dec:
            m = re.search(r"\b(?:exclude|drop|reject|remove)\s+([A-Za-z]+_\d+(?:_[A-Za-z]+)?)", dec, re.I)
            if m:
                excl[m.group(1)] = f"duplicate analysis decision ({a} vs {b}): {dec}"
            elif re.search(r"exclude[_ ]?b", dec, re.I):
                excl[b] = f"duplicate analysis decision ({a} vs {b}): {dec}"
            elif re.search(r"exclude[_ ]?a", dec, re.I):
                excl[a] = f"duplicate analysis decision ({a} vs {b}): {dec}"
        elif r.get("suggested_status") == "Duplicate":
            unresolved.append((a, b))
    return excl, unresolved


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root"); a = ap.parse_args()
    root = get_root(a.root); log = setup_logging("validate", root / "reports" / "pipeline.log")
    reg_rows = read_csv(root / "metadata" / "source_registry.csv")
    if not reg_rows:
        raise SystemExit("metadata/source_registry.csv missing/empty")
    dup_rows = read_csv(root / "metadata" / "duplicate_analysis.csv")
    overrides = {r["document_id"]: r for r in read_csv(root / "metadata" / "ingestion_overrides.csv")}
    dup_excl, unresolved = parse_dup_decisions(dup_rows)

    manifest, details, seen_hash = [], [], {}
    for r in sorted(reg_rows, key=lambda x: x["document_id"]):
        did = r["document_id"]; cp = cleaned_path_for(root, r); rel = str(cp.relative_to(root)) if cp.exists() else str(Path("cleaned_documents") / cp.name)
        text = cp.read_text(encoding="utf-8", errors="replace") if cp.exists() else ""
        _, body = split_header(text); chars, words = len(body.strip()), len(body.split())
        orig = resolve_original(root, r["original_file_path"])
        missing = [f for f in REQUIRED_META if is_unknown(r.get(f))]
        unknown_nice = [f for f in NICE_META if is_unknown(r.get(f))]
        status, reason = "included", ""
        h = sha256(re.sub(r"\s+", " ", body).strip()) if chars else None

        ov = overrides.get(did)
        if ov and ov["action"].strip().lower() == "exclude":
            status, reason = "excluded", f"manual override: {ov.get('reason', '')}"
        elif did.endswith("_PAGE"):
            status, reason = "excluded", "metadata-only landing page (the PDF is the document)"
        elif "reference_only" in str(r["original_file_path"]).replace("\\", "/") or "reference" in (r.get("collection_priority") or "").lower():
            status, reason = "excluded", "reference-only material (never ingested)"
        elif not (ov and ov["action"].strip().lower() == "include"):
            if not r.get("extraction_status", "").strip().upper().startswith("OK"):
                status, reason = "excluded", f"extraction status not OK: {r.get('extraction_status')}"
            elif not cp.exists():
                status, reason = "excluded", "cleaned text file missing"
            elif chars < MIN_CHARS:
                status, reason = "excluded", f"cleaned text empty/too short ({chars} chars)"
            elif did in dup_excl:
                status, reason = "excluded", dup_excl[did]
            elif h in seen_hash and seen_hash[h][1] == "included":
                status, reason = "excluded", f"identical text to {seen_hash[h][0]} (same underlying file/content)"
            elif any(did == b for _, b in unresolved):
                a_id = next(x for x, b in unresolved if b == did)
                status, reason = "excluded", f"unresolved duplicate of {a_id} (no final_decision recorded) - resolve in duplicate_analysis.csv"
        if h and status == "included":
            seen_hash.setdefault(h, (did, status))
        manifest.append(dict(document_id=did, title=r["title"], organization=r["organization"], source_url=r["source_url"],
                             cleaned_text_path=rel, inclusion_status=status, exclusion_reason=reason))
        details.append(dict(id=did, status=status, reason=reason, chars=chars, words=words, extraction=r.get("extraction_status", ""),
                            orig_exists=orig.exists(), missing=missing, unknown_nice=unknown_nice, fmt=r.get("file_format", "")))

    mp = root / "metadata" / "ingestion_manifest.csv"
    if mp.exists():
        shutil.copy2(mp, str(mp) + ".bak")
    write_csv(mp, MANIFEST_FIELDS, manifest)

    ref_files = sorted(str(p.relative_to(root)) for p in (root / "reference_only").rglob("*") if p.is_file()) if (root / "reference_only").exists() else []
    inc = [d for d in details if d["status"] == "included"]; exc = [d for d in details if d["status"] != "included"]
    partial = [r for r in dup_rows if r.get("suggested_status") == "Partially overlapping" and not (r.get("final_decision") or "").strip()]
    L = [f"# Document validation report\nGenerated {now_iso()}\n",
         f"- Registry rows: **{len(details)}**  |  eligible for core ingestion: **{len(inc)}**  |  excluded: **{len(exc)}**",
         f"- Reference-only files present (never ingested): **{len(ref_files)}**\n",
         "## Included documents\n| ID | Format | Words | Extraction status | Missing key metadata | Unknown (non-critical) |\n|---|---|---|---|---|---|"]
    for d in inc:
        L.append(f"| {d['id']} | {d['fmt']} | {d['words']} | {d['extraction']} | {', '.join(d['missing']) or '-'} | {', '.join(d['unknown_nice']) or '-'} |")
    L += ["\n## Excluded documents\n| ID | Reason |\n|---|---|"] + [f"| {d['id']} | {d['reason']} |" for d in exc]
    L += ["\n## Problems needing your attention"]
    probs = []
    probs += [f"- **{d['id']}**: original file not found on disk" for d in details if not d["orig_exists"]]
    probs += [f"- **{d['id']}**: extraction `{d['extraction']}` -> manual correction/OCR needed" for d in details if not d["extraction"].upper().startswith("OK") and not d["id"].endswith("_PAGE")]
    probs += [f"- **{d['id']}**: missing required metadata: {', '.join(d['missing'])}" for d in inc if d["missing"]]
    reasons = {d["id"]: d["reason"] for d in details}
    probs += [f"- Unresolved duplicate pair: {a_} vs {b_} (second one auto-excluded until you record a decision)"
              for a_, b_ in unresolved if reasons.get(b_, "").startswith("unresolved duplicate")]
    probs += [f"- Partially overlapping pair with no decision (both still included): {r['doc_a']} vs {r['doc_b']} (containment {r.get('shingle_containment')})" for r in partial]
    L += probs or ["- None found."]
    L += ["\n## Reference-only files", *([f"- {p}" for p in ref_files] or ["- (none)"])]
    L += ["\n## Metadata completeness note\nPublication/last-updated dates and licences are auto-detected by collect_kb.py and often `Unknown`. "
          "They are carried into chunk metadata as null when unknown - nothing is invented."]
    (root / "reports").mkdir(exist_ok=True)
    (root / "reports" / "document_validation_report.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    log.info("Included %d, excluded %d. Report: reports/document_validation_report.md ; manifest: metadata/ingestion_manifest.csv", len(inc), len(exc))
    for d in exc:
        log.info("  excluded %-14s %s", d["id"], d["reason"])
    if probs:
        log.warning("%d issue(s) listed in the report - review before chunking.", len(probs))


if __name__ == "__main__":
    main()
