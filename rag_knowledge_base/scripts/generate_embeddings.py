#!/usr/bin/env python3
"""Step 5: embed chunks (kept separate from chunking and from ChromaDB).

Embeddings are cached in embeddings/embedding_cache.npz keyed by a hash of (model, prefix, embedded text), so unchanged
chunks are never re-embedded. Writes metadata/embedding_config.json.
"""
import argparse
import numpy as np
from kb_common import *

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


def load_chunks(root, chunks_dir="chunks"):
    p = root / chunks_dir / "chunks.jsonl"
    if not p.exists():
        raise SystemExit(f"{p} not found - run chunk_documents.py first")
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def load_cache(path):
    if not Path(path).exists():
        return {}
    z = np.load(path, allow_pickle=False)
    return dict(zip(z["hashes"].tolist(), z["vectors"]))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root"); ap.add_argument("--model", default=DEFAULT_MODEL); ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--query-prefix", default="", help='e.g. "Represent this sentence for searching relevant passages: " for BGE models')
    ap.add_argument("--doc-prefix", default=""); ap.add_argument("--no-normalize", action="store_true")
    ap.add_argument("--chunks-dir", default="chunks"); ap.add_argument("--force", action="store_true", help="ignore cache and re-embed everything")
    a = ap.parse_args()
    root = get_root(a.root); log = setup_logging("embed", root / "reports" / "pipeline.log")
    chunks = load_chunks(root, a.chunks_dir)
    cache_path = root / "embeddings" / "embedding_cache.npz"; (root / "embeddings").mkdir(exist_ok=True)
    cfg_old = read_json(root / "metadata" / "embedding_config.json", {})
    normalize = not a.no_normalize

    log.info("Loading model %s ...", a.model)
    emb = Embedder(a.model, a.query_prefix, a.doc_prefix, normalize)
    dim = emb.model.get_sentence_embedding_dimension(); max_seq = getattr(emb.model, "max_seq_length", None)
    cache = {} if a.force else load_cache(cache_path)
    hashes = [embed_hash(c, a.model, a.doc_prefix) for c in chunks]
    todo = [i for i, h in enumerate(hashes) if h not in cache]
    log.info("%d chunks, %d cached, %d to embed (dim=%s, max_seq_length=%s)", len(chunks), len(chunks) - len(todo), len(todo), dim, max_seq)

    if todo:
        vecs = emb.encode_docs([embed_text_for(chunks[i]) for i in todo], batch_size=a.batch_size)
        for i, v in zip(todo, vecs):
            cache[hashes[i]] = v.astype(np.float32)
    keep = {h: cache[h] for h in hashes}                       # drop stale entries
    np.savez_compressed(cache_path, hashes=np.array(list(keep), dtype=str), vectors=np.stack(list(keep.values())))

    # truncation check: the model silently ignores tokens beyond max_seq_length
    truncated = None
    try:
        count = get_token_counter(a.model, log)
        if not count.kind.startswith("approx") and max_seq:
            truncated = sum(count(embed_text_for(c)) > max_seq for c in chunks)
            if truncated:
                log.warning("%d/%d chunks are longer than the model's max_seq_length (%d tokens): text beyond that is NOT embedded. "
                            "Use smaller chunks or a longer-context model (e.g. BAAI/bge-small-en-v1.5, 512).", truncated, len(chunks), max_seq)
    except Exception as e:  # noqa
        log.warning("Truncation check skipped: %s", e)

    revision = None
    try:
        revision = emb.model[0].auto_model.config._commit_hash
    except Exception:
        pass
    cfg = dict(model_name=a.model, model_revision=revision or "Unknown", embedding_dimension=int(dim), normalized=normalize,
               similarity_metric="cosine (vectors L2-normalised)" if normalize else "unnormalised",
               batch_size=a.batch_size, max_seq_length=max_seq, chunks_exceeding_max_seq=truncated,
               query_prefix=a.query_prefix, doc_prefix=a.doc_prefix,
               embedded_text_template="{document_title} - {section_heading}\\n{chunk_text}",
               n_chunks=len(chunks), n_newly_embedded=len(todo), date_generated=now_iso(),
               collection_name=cfg_old.get("collection_name", "anxiety_stress_knowledge_base"))
    write_json(root / "metadata" / "embedding_config.json", cfg)
    log.info("Embeddings cached at %s ; config written to metadata/embedding_config.json", cache_path.relative_to(root))


if __name__ == "__main__":
    main()
