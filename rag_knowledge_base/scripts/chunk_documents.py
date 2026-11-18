#!/usr/bin/env python3
"""Step 3+4: structure-aware chunking of every document marked `included` in ingestion_manifest.csv.

Outputs: chunks/chunks.jsonl, chunks/chunks.csv, chunks/chunk_statistics.json, metadata/chunking_config.json
"""
import argparse, statistics
from collections import Counter
from kb_common import *
from chunking import chunk_document

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
NO_PDF_HEADINGS = False


def build_chunk_records(docs, count, target, overlap, min_tokens, max_tokens, log=None):
    """Chunk all documents. Used by chunk_documents.py and by the chunk-size comparison."""
    records = []
    for d in docs:
        r, did = d["registry"], d["registry"]["document_id"]
        chunks, is_pdf = chunk_document(d["body"], count, target, overlap, min_tokens, max_tokens, pdf_headings=not NO_PDF_HEADINGS)
        n = len(chunks)
        for i, c in enumerate(chunks, 1):
            heads = c.headings or ([c.path[-1]] if c.path else [])
            records.append({
                "chunk_id": f"{did}_CHUNK_{i:03d}", "document_id": did, "text": c.text,
                "metadata": {
                    "document_title": r["title"], "organization": r["organization"], "source_url": r["source_url"],
                    "topic": null_if_unknown(r.get("primary_topic")), "subtopic": null_if_unknown(r.get("secondary_topics")),
                    "section_heading": " / ".join(dict.fromkeys(heads)) or None,
                    "heading_path": " > ".join(c.path) or None,
                    "page_number": c.page_start, "page_end": c.page_end,
                    "chunk_index": i, "total_chunks": n, "collection_type": "core",
                    "language": null_if_unknown(r.get("language")), "publication_date": null_if_unknown(r.get("publication_date")),
                    "last_updated_date": null_if_unknown(r.get("last_updated_date")), "file_format": r.get("file_format"),
                    "source_file": r["original_file_path"], "token_count": c.tokens, "overlap_tokens": c.overlap_tokens,
                    "heading_detection": "heuristic (PDF)" if is_pdf else "markdown headings",
                }})
        if log:
            log.info("%-14s %3d chunks", did, n)
    return records


def statistics_for(records, min_tokens, max_tokens, target, model_max_seq=None):
    toks = [r["metadata"]["token_count"] for r in records]; chars = [len(r["text"]) for r in records]
    per_doc = Counter(r["document_id"] for r in records)
    by_doc = {}
    for r in records:
        by_doc.setdefault(r["document_id"], []).append(r["metadata"]["token_count"])
    buckets = Counter()
    for t in toks:
        buckets[f"{(t // 100) * 100}-{(t // 100) * 100 + 99}"] += 1
    unusual = {d: dict(n_chunks=len(v), min=min(v), max=max(v), mean=round(statistics.mean(v), 1),
                       n_small=sum(x < min_tokens for x in v), n_large=sum(x > max_tokens for x in v))
               for d, v in by_doc.items() if any(x < min_tokens or x > max_tokens for x in v)}
    out = dict(n_documents=len(per_doc), n_chunks=len(records), chunks_per_document=dict(per_doc),
               tokens=dict(mean=round(statistics.mean(toks), 1), median=statistics.median(toks), min=min(toks), max=max(toks)),
               chars=dict(mean=round(statistics.mean(chars), 1), min=min(chars), max=max(chars)),
               token_distribution={k: buckets[k] for k in sorted(buckets, key=lambda s: int(s.split("-")[0]))},
               documents_with_small_or_large_chunks=unusual, target_tokens=target)
    if model_max_seq:
        out["chunks_over_model_max_seq"] = sum(t > model_max_seq for t in toks)
        out["model_max_seq_tokens"] = model_max_seq
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root"); ap.add_argument("--target", type=int, default=500); ap.add_argument("--overlap", type=int, default=100)
    ap.add_argument("--min", type=int, default=100, dest="min_tokens"); ap.add_argument("--max", type=int, default=700, dest="max_tokens")
    ap.add_argument("--tokenizer-model", default=DEFAULT_MODEL, help="tokenizer used to count tokens (use the embedding model)")
    ap.add_argument("--model-max-seq", type=int, default=None, help="embedding model max sequence length, for the over-limit warning")
    ap.add_argument("--no-pdf-headings", action="store_true", help="do not guess headings in PDFs (use for illustrated PDFs like WHO_003)")
    ap.add_argument("--out-dir", default="chunks"); ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    global NO_PDF_HEADINGS; NO_PDF_HEADINGS = a.no_pdf_headings
    root = get_root(a.root); log = setup_logging("chunk", root / "reports" / "pipeline.log")
    out = root / a.out_dir; out.mkdir(parents=True, exist_ok=True)
    for f in ("chunks.jsonl", "chunks.csv", "chunk_statistics.json"):
        guard_overwrite(out / f, a.force, log)
    count = get_token_counter(a.tokenizer_model, log)
    docs = load_included_documents(root, log)
    log.info("Chunking %d documents (target=%d overlap=%d min=%d max=%d, token counter=%s)", len(docs), a.target, a.overlap, a.min_tokens, a.max_tokens, count.kind)
    recs = build_chunk_records(docs, count, a.target, a.overlap, a.min_tokens, a.max_tokens, log)
    if not recs:
        raise SystemExit("No chunks produced.")
    with open(out / "chunks.jsonl", "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    flat = [{"chunk_id": r["chunk_id"], "document_id": r["document_id"], "text": r["text"], **r["metadata"]} for r in recs]
    write_csv(out / "chunks.csv", list(flat[0].keys()), flat)
    stats = statistics_for(recs, a.min_tokens, a.max_tokens, a.target, a.model_max_seq)
    stats["token_counter"] = count.kind
    write_json(out / "chunk_statistics.json", stats)
    write_json(root / "metadata" / "chunking_config.json",
               dict(target_tokens=a.target, overlap_tokens=a.overlap, min_tokens=a.min_tokens, max_tokens=a.max_tokens,
                    token_counter=count.kind, strategy="structure-aware (headings > paragraphs/lists/tables > sentences > tokens)", generated=now_iso()))
    log.info("%d chunks written. tokens mean=%s min=%s max=%s", len(recs), stats["tokens"]["mean"], stats["tokens"]["min"], stats["tokens"]["max"])
    if stats["documents_with_small_or_large_chunks"]:
        log.warning("Documents with chunks outside [%d,%d]: %s", a.min_tokens, a.max_tokens, list(stats["documents_with_small_or_large_chunks"]))
    if count.kind.startswith("approx"):
        log.warning("Token counts are APPROXIMATE (tokenizer not loaded). Install transformers + internet access for exact counts.")


if __name__ == "__main__":
    main()