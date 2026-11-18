#!/usr/bin/env python3
"""Semantic retrieval over the ChromaDB knowledge base. No LLM / answer generation here.

    from retrieval import retrieve, format_context
    resp = retrieve("What is a panic attack?", top_k=5)
    for r in resp["results"]: print(r["similarity"], r["title"], r["section_heading"], r["source_url"])

CLI:  python retrieval.py "How can I manage excessive worrying?" --top-k 5 [--topic "Stress"] [--max-distance 0.6]

Distances are cosine distances (0 = identical, larger = less similar); similarity = 1 - distance.
No default threshold is applied - choose one only after looking at evaluation results.
"""
import argparse, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
_STATE = {}


def _load(root=ROOT):
    key = str(root)
    if key not in _STATE:
        import chromadb
        cfg = json.loads((Path(root) / "metadata" / "embedding_config.json").read_text(encoding="utf-8"))
        from sentence_transformers import SentenceTransformer
        client = chromadb.PersistentClient(path=str(Path(root) / "vector_db"))
        col = client.get_collection(cfg.get("collection_name", "anxiety_stress_knowledge_base"), embedding_function=None)
        _STATE[key] = dict(cfg=cfg, col=col, model=SentenceTransformer(cfg["model_name"]))
    return _STATE[key]


def embed_query(query, root=ROOT):
    s = _load(root); cfg = s["cfg"]
    return s["model"].encode([cfg.get("query_prefix", "") + query], normalize_embeddings=cfg.get("normalized", True),
                             convert_to_numpy=True)[0].tolist()


def retrieve(query, top_k=5, topic=None, organization=None, document_id=None, max_distance=None, root=ROOT, query_embedding=None):
    """Return {"query", "status", "results": [...]}.
    status: "ok" | "no_results" (empty DB / filters match nothing) | "below_threshold" (hits exist but all beyond max_distance)."""
    if not query or not query.strip():
        return dict(query=query, status="no_results", results=[], note="Empty query")
    s = _load(root); col = s["col"]
    filters = [{k: v} for k, v in (("topic", topic), ("organization", organization), ("document_id", document_id)) if v]
    where = None if not filters else (filters[0] if len(filters) == 1 else {"$and": filters})
    emb = query_embedding if query_embedding is not None else embed_query(query, root)
    res = col.query(query_embeddings=[emb], n_results=top_k, where=where, include=["documents", "metadatas", "distances"])
    out = []
    for rank, (cid, doc, md, dist) in enumerate(zip(res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0]), 1):
        md = md or {}
        out.append(dict(rank=rank, chunk_id=cid, document_id=md.get("document_id"), text=doc, distance=float(dist), similarity=1.0 - float(dist),
                        title=md.get("document_title"), organization=md.get("organization"), source_url=md.get("source_url"),
                        section_heading=md.get("section_heading"), page_number=md.get("page_number"), topic=md.get("topic"),
                        publication_date=md.get("publication_date"), chunk_index=md.get("chunk_index"), total_chunks=md.get("total_chunks")))
    status = "ok" if out else "no_results"
    if max_distance is not None and out:
        out = [r for r in out if r["distance"] <= max_distance]
        status = "ok" if out else "below_threshold"
    return dict(query=query, status=status, results=out)


def list_topics(root=ROOT):
    got = _load(root)["col"].get(include=["metadatas"], limit=_load(root)["col"].count())
    return sorted({m.get("topic") for m in got["metadatas"] if m.get("topic")})


def format_context(resp, max_chars=None):
    """Plain-text context block with source labels, ready to paste into a future LLM prompt."""
    parts = []
    for r in resp["results"]:
        where = f", p.{r['page_number']}" if r.get("page_number") else ""
        sec = f" - {r['section_heading']}" if r.get("section_heading") else ""
        parts.append(f"[{r['rank']}] {r['title']} ({r['organization']}){sec}{where}\nSource: {r['source_url']}\n{r['text']}")
    txt = "\n\n".join(parts)
    return txt[:max_chars] if max_chars else txt


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query"); ap.add_argument("--top-k", type=int, default=5); ap.add_argument("--topic"); ap.add_argument("--organization")
    ap.add_argument("--max-distance", type=float); ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    resp = retrieve(a.query, a.top_k, a.topic, a.organization, max_distance=a.max_distance)
    if a.json:
        print(json.dumps(resp, indent=2, ensure_ascii=False)); sys.exit()
    print(f"status: {resp['status']}")
    for r in resp["results"]:
        pg = f" p.{r['page_number']}" if r["page_number"] else ""
        print(f"\n#{r['rank']}  sim={r['similarity']:.3f} dist={r['distance']:.3f}  {r['chunk_id']}\n   {r['title']} | {r['section_heading']}{pg}\n   {r['source_url']}\n   {r['text'][:300].replace(chr(10), ' ')}...")
