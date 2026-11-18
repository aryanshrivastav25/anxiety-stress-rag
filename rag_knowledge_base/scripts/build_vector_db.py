#!/usr/bin/env python3
"""Step 6: load chunks + cached embeddings into a persistent ChromaDB collection (vector_db/).

* Stable IDs = chunk_id. Re-running is idempotent: only new/changed chunks are upserted.
* --prune removes chunks of *re-chunked* or removed documents that no longer exist in chunks.jsonl.
* --rebuild deletes the collection and rebuilds it from scratch.
* Hard guard: only chunks with collection_type == "core" whose document is `included` in the manifest are inserted.
"""
import argparse
import chromadb
import numpy as np
from kb_common import *
from generate_embeddings import load_chunks, load_cache

BATCH = 1000


def clean_meta(md: dict) -> dict:
    """Chroma metadata must be str/int/float/bool; drop nulls (absence == unknown)."""
    return {k: v for k, v in md.items() if v is not None and isinstance(v, (str, int, float, bool))}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root"); ap.add_argument("--chunks-dir", default="chunks")
    ap.add_argument("--rebuild", action="store_true"); ap.add_argument("--prune", action="store_true")
    a = ap.parse_args()
    root = get_root(a.root); log = setup_logging("vectordb", root / "reports" / "pipeline.log")
    cfg = read_json(root / "metadata" / "embedding_config.json")
    if not cfg:
        raise SystemExit("metadata/embedding_config.json missing - run generate_embeddings.py first")
    chunks = load_chunks(root, a.chunks_dir); cache = load_cache(root / "embeddings" / "embedding_cache.npz")
    included = {m["document_id"] for m in read_csv(root / "metadata" / "ingestion_manifest.csv") if m["inclusion_status"] == "included"}

    ids, vecs, docs, metas = [], [], [], []
    for c in chunks:
        if c["metadata"].get("collection_type") != "core" or c["document_id"] not in included \
                or "reference_only" in str(c["metadata"].get("source_file", "")).replace("\\", "/"):
            raise SystemExit(f"REFUSING to insert {c['chunk_id']}: not a core/included document (reference-only or excluded).")
        h = embed_hash(c, cfg["model_name"], cfg.get("doc_prefix", ""))
        if h not in cache:
            raise SystemExit(f"No cached embedding for {c['chunk_id']} - re-run generate_embeddings.py")
        md = clean_meta({**c["metadata"], "document_id": c["document_id"], "chunk_id": c["chunk_id"], "embed_hash": h})
        ids.append(c["chunk_id"]); vecs.append(cache[h]); docs.append(c["text"]); metas.append(md)
    if len(set(ids)) != len(ids):
        raise SystemExit("Duplicate chunk_ids in chunks.jsonl")
    if vecs and len(vecs[0]) != cfg["embedding_dimension"]:
        raise SystemExit("Embedding dimension mismatch with embedding_config.json")

    client = chromadb.PersistentClient(path=str(root / "vector_db"))
    name = cfg.get("collection_name", "anxiety_stress_knowledge_base")
    if a.rebuild:
        try:
            client.delete_collection(name); log.info("Deleted existing collection %s", name)
        except Exception:
            pass
    col = client.get_or_create_collection(name, metadata={"hnsw:space": "cosine", "embedding_model": cfg["model_name"]}, embedding_function=None)
    existing_model = (col.metadata or {}).get("embedding_model")
    if existing_model and existing_model != cfg["model_name"] and col.count():
        raise SystemExit(f"Collection was built with {existing_model}, config says {cfg['model_name']}. Use --rebuild.")

    have = {}
    if col.count():
        got = col.get(include=["metadatas"], limit=col.count())
        have = {i: (m or {}).get("embed_hash") for i, m in zip(got["ids"], got["metadatas"])}
    todo = [i for i, cid in enumerate(ids) if have.get(cid) != metas[i]["embed_hash"]]
    stale = [cid for cid in have if cid not in set(ids)]
    for s in range(0, len(todo), BATCH):
        sel = todo[s:s + BATCH]
        col.upsert(ids=[ids[i] for i in sel], embeddings=[np.asarray(vecs[i]).tolist() for i in sel],
                   documents=[docs[i] for i in sel], metadatas=[metas[i] for i in sel])
    if stale and a.prune:
        for s in range(0, len(stale), BATCH):
            col.delete(ids=stale[s:s + BATCH])
        log.info("Pruned %d stale chunks", len(stale))
    elif stale:
        log.warning("%d chunks in the DB are no longer in chunks.jsonl (e.g. %s). Use --prune to remove them.", len(stale), stale[:3])
    log.info("Collection '%s': upserted %d, unchanged %d, total now %d (expected %d)", name, len(todo), len(ids) - len(todo), col.count(), len(ids))
    if a.prune or not stale:
        assert col.count() == len(ids), "Count mismatch after build"


if __name__ == "__main__":
    main()
