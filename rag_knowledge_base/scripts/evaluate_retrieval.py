#!/usr/bin/env python3
"""Step 8+9: retrieval evaluation and chunk-size comparison.

Sub-commands
  draft-questions   write evaluation/retrieval_test_questions.json (questions only; expected_document_ids EMPTY, reviewed=false)
  suggest           retrieve top-10 per question -> evaluation/review_candidates.csv  (to help YOU judge relevance by reading the text)
  evaluate          score questions you have reviewed (reviewed=true, expected_document_ids filled) -> retrieval_results.csv + report
  compare-chunking  configs A/B/C in throw-away in-memory Chroma collections -> evaluation/chunking_comparison.csv

Ground truth is document-level: a retrieved chunk counts as relevant if its document_id is in expected_document_ids.
Expected ids must be judged by you after reading the sources - this script never invents them.
"""
import argparse, sys
from collections import Counter, defaultdict
from pathlib import Path
from kb_common import *

CATEGORIES = {
 "Anxiety": [("What is anxiety?", ["anxiety definition"]), ("What are common symptoms of anxiety?", ["anxiety symptoms"]),
             ("What is generalized anxiety disorder?", ["generalized anxiety", "worry"]), ("How does social anxiety affect daily activities?", ["social anxiety"]),
             ("What is a panic attack?", ["panic"]), ("What is a phobia and how is it different from ordinary fear?", ["phobias"]),
             ("What is the difference between normal anxiety and an anxiety disorder?", ["anxiety definition"]),
             ("What causes anxiety disorders or increases the risk of them?", ["triggers", "risk factors"]),
             ("Why do people with anxiety avoid certain situations?", ["avoidance", "phobias"])],
 "Stress": [("What is stress?", ["stress definition"]), ("How does stress affect the body?", ["stress effects"]),
            ("What are common causes of stress?", ["stress triggers"]), ("How can examination pressure contribute to stress?", ["academic stress"]),
            ("How does prolonged stress affect everyday life?", ["chronic stress"]), ("What is the difference between stress and anxiety?", ["stress vs anxiety"]),
            ("What are signs that stress is becoming a problem?", ["stress effects", "help-seeking"]),
            ("How can work pressure lead to stress?", ["work stress"])],
 "Coping": [("What are some grounding techniques?", ["grounding"]), ("How can someone manage excessive worrying?", ["worry", "coping"]),
            ("What are practical ways to relax?", ["relaxation"]), ("How can sleep and daily routines affect well-being?", ["sleep", "routines"]),
            ("Does exercise help with stress or anxiety?", ["exercise", "lifestyle"]), ("How can talking to friends or family help when stressed?", ["social support"]),
            ("What are healthy ways to cope with stress?", ["coping"]), ("How can slow breathing help in a stressful moment?", ["breathing", "grounding"])],
 "Help-seeking": [("When should someone consider seeking professional help?", ["help-seeking"]),
                  ("How can someone prepare for a mental-health appointment?", ["help-seeking", "appointment"]),
                  ("What kinds of professionals can help with anxiety?", ["help-seeking"]),
                  ("What treatments are generally available for anxiety disorders?", ["treatment overview"])],
 "Populations": [("What might academic stress look like in young people?", ["academic stress", "young people"]),
                 ("What information is available for children experiencing stress?", ["children"]),
                 ("How can adults support a stressed child or teenager?", ["children", "social support"])],
}


def q_path(root): return root / "evaluation" / "retrieval_test_questions.json"


def load_questions(root):
    qs = read_json(q_path(root))
    if not qs:
        raise SystemExit("Run draft-questions first.")
    return qs


def cmd_draft(a, root, log):
    p = q_path(root)
    if p.exists() and not a.force:
        raise SystemExit(f"{p} exists; use --force to overwrite (your reviewed judgments would be lost!)")
    qs, n = [], 1
    for cat, items in CATEGORIES.items():
        for q, topics in items:
            qs.append(dict(question_id=f"Q{n:02d}", category=cat, question=q, expected_document_ids=[], expected_topics=topics,
                           notes="TODO: read the candidates, then list the documents that truly answer this question and set reviewed=true", reviewed=False))
            n += 1
    write_json(p, qs); log.info("Wrote %d draft questions to %s", len(qs), p)


def cmd_suggest(a, root, log):
    sys.path.insert(0, str(root)); from retrieval import retrieve
    rows = []
    for q in load_questions(root):
        resp = retrieve(q["question"], top_k=a.top_k, root=root)
        for r in resp["results"]:
            rows.append(dict(question_id=q["question_id"], question=q["question"], rank=r["rank"], document_id=r["document_id"], chunk_id=r["chunk_id"],
                             similarity=round(r["similarity"], 3), section=r["section_heading"], text_preview=r["text"][:400].replace("\n", " "),
                             relevant_YES_NO=""))
    out = root / "evaluation" / "review_candidates.csv"
    write_csv(out, list(rows[0].keys()), rows)
    log.info("Wrote %s. Read each candidate, then fill expected_document_ids in retrieval_test_questions.json yourself.", out)
    log.info("Tip: also use `grep -il <keyword> cleaned_documents/*.txt` to find documents the retriever may have missed.")


# ---------------------------------------------------------------- metrics
def metrics_for(retrieved_doc_ids, expected, k):
    top = retrieved_doc_ids[:k]; exp = set(expected)
    rel_flags = [d in exp for d in top]
    precision = sum(rel_flags) / k if k else 0.0
    recall = len({d for d in top if d in exp}) / len(exp) if exp else 0.0
    hit = 1.0 if any(rel_flags) else 0.0
    rr = next((1.0 / (i + 1) for i, f in enumerate(rel_flags) if f), 0.0)
    return dict(precision=precision, recall=recall, hit=hit, rr=rr)


def aggregate(per_q):
    n = len(per_q)
    return {k: (sum(m[k] for m in per_q) / n if n else float("nan")) for k in ("precision", "recall", "hit", "rr")}


def cmd_evaluate(a, root, log):
    sys.path.insert(0, str(root)); from retrieval import retrieve
    qs = load_questions(root); scored = [q for q in qs if q.get("reviewed") and q["expected_document_ids"]]
    skipped = [q["question_id"] for q in qs if q not in scored]
    if skipped:
        log.warning("%d question(s) not scored (reviewed=false or no expected ids): %s", len(skipped), skipped)
    if not scored:
        raise SystemExit("No reviewed questions. Fill expected_document_ids and set reviewed=true.")
    k = a.k; rows, per_q, irrelevant = [], {}, Counter()
    for q in scored:
        resp = retrieve(q["question"], top_k=k, root=root)
        docs = [r["document_id"] for r in resp["results"]]
        per_q[q["question_id"]] = m = metrics_for(docs, q["expected_document_ids"], k)
        for r in resp["results"]:
            rel = r["document_id"] in q["expected_document_ids"]
            if not rel:
                irrelevant[(r["chunk_id"], r["title"], r["section_heading"])] += 1
            rows.append(dict(question_id=q["question_id"], category=q["category"], question=q["question"], rank=r["rank"], chunk_id=r["chunk_id"],
                             document_id=r["document_id"], section_heading=r["section_heading"], similarity=round(r["similarity"], 4),
                             relevant=rel, expected_document_ids=";".join(q["expected_document_ids"])))
    write_csv(root / "evaluation" / "retrieval_results.csv", list(rows[0].keys()), rows)
    overall = aggregate(list(per_q.values()))
    by_cat = defaultdict(list)
    for q in scored:
        by_cat[q["category"]].append(per_q[q["question_id"]])
    poor = [(q, per_q[q["question_id"]]) for q in scored if per_q[q["question_id"]]["hit"] == 0 or per_q[q["question_id"]]["precision"] < 0.4]
    L = [f"# Retrieval evaluation report\nGenerated {now_iso()}  |  K = {k}  |  questions scored: {len(scored)} of {len(qs)}\n",
         "## Overall", f"| Precision@{k} | Recall@{k} | MRR | Hit rate@{k} |\n|---|---|---|---|",
         f"| {overall['precision']:.3f} | {overall['recall']:.3f} | {overall['rr']:.3f} | {overall['hit']:.3f} |",
         "\n## By category\n| Category | n | P@K | R@K | MRR | Hit@K |\n|---|---|---|---|---|---|"]
    for c, ms in by_cat.items():
        g = aggregate(ms); L.append(f"| {c} | {len(ms)} | {g['precision']:.3f} | {g['recall']:.3f} | {g['rr']:.3f} | {g['hit']:.3f} |")
    L += ["\n## Questions with poor retrieval (no hit, or precision < 0.4)"]
    L += [f"- **{q['question_id']}** {q['question']} (hit={m['hit']:.0f}, P@K={m['precision']:.2f}, expected: {', '.join(q['expected_document_ids'])})" for q, m in poor] or ["- None"]
    L += ["\n## Most frequently retrieved irrelevant chunks"]
    L += [f"- {n}x `{cid}` {t} / {s}" for (cid, t, s), n in irrelevant.most_common(10)] or ["- None"]
    L += ["\n## Missing source coverage / questions not scored",
          *([f"- {x}" for x in skipped] or ["- All questions scored."]),
          "- Questions with hit=0 above may indicate that no collected document covers the topic (e.g. academic/work stress, India-specific resources) - check coverage_gap_report.md.",
          "\n## What the metrics mean (and their limits)",
          f"- **Precision@{k}**: share of the top-{k} chunks that come from an expected document. Penalises irrelevant filler; with document-level truth it cannot tell a relevant document's *useful* chunk from an unrelated chunk of the same document.",
          f"- **Recall@{k}**: share of expected documents that appear in the top-{k}. Capped by K and by how many expected documents you listed (if you listed 4 but K=3, recall cannot reach 1).",
          "- **MRR**: mean of 1/rank of the first relevant chunk; rewards putting a good source first. Ignores everything after the first hit.",
          f"- **Hit rate@{k}**: share of questions with at least one relevant chunk in the top-{k}. Lenient.",
          "- Limitations: small hand-labelled set; document-level (not passage-level) judgments; duplicated content across documents can create false 'irrelevant' labels; "
          "**good retrieval scores do not mean generated answers will be safe, accurate or appropriate** - that needs separate evaluation once an LLM is added.",
          "\n## Recommendations (fill in after reading the results)\n- Inspect poor questions: is the answer split across chunks (try larger/overlapping chunks) or absent from the corpus (add a source)?\n"
          "- Consider hybrid keyword+semantic search or a re-ranker if relevant chunks appear at ranks 4-10.\n- Choose any similarity threshold only from the similarity distributions in retrieval_results.csv."]
    (root / "evaluation" / "retrieval_evaluation_report.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    log.info("Overall P@%d=%.3f R@%d=%.3f MRR=%.3f Hit@%d=%.3f", k, overall["precision"], k, overall["recall"], overall["rr"], k, overall["hit"])


# ---------------------------------------------------------------- chunk-size comparison
CONFIGS = {"A_300_50": (300, 50), "B_500_100": (500, 100), "C_700_100": (700, 100)}


def cmd_compare(a, root, log):
    import chromadb, numpy as np
    from chunk_documents import build_chunk_records
    qs = [q for q in load_questions(root) if q.get("reviewed") and q["expected_document_ids"]]
    if not qs:
        raise SystemExit("No reviewed questions to score.")
    cfg = read_json(root / "metadata" / "embedding_config.json")
    if not cfg:
        raise SystemExit("embedding_config.json missing (needed so the comparison uses the same model/settings)")
    docs = load_included_documents(root, log); count = get_token_counter(cfg["model_name"], log)
    emb = Embedder(cfg["model_name"], cfg.get("query_prefix", ""), cfg.get("doc_prefix", ""), cfg.get("normalized", True))
    max_seq = getattr(emb.model, "max_seq_length", None)
    qv = emb.encode_queries([q["question"] for q in qs]); client = chromadb.EphemeralClient(); rows = []
    for name, (tgt, ov) in CONFIGS.items():
        recs = build_chunk_records(docs, count, tgt, ov, min(100, tgt // 3), int(tgt * 1.4))
        vecs = emb.encode_docs([embed_text_for(r) for r in recs], show_progress=False)
        cname = f"cmp_{name.lower()}"
        try: client.delete_collection(cname)
        except Exception: pass
        col = client.create_collection(cname, metadata={"hnsw:space": "cosine"}, embedding_function=None)
        col.add(ids=[r["chunk_id"] for r in recs], embeddings=[v.tolist() for v in vecs], documents=[r["text"] for r in recs],
                metadatas=[{"document_id": r["document_id"]} for r in recs])
        per_q = []
        for q, v in zip(qs, qv):
            res = col.query(query_embeddings=[v.tolist()], n_results=a.k, include=["metadatas"])
            per_q.append(metrics_for([m["document_id"] for m in res["metadatas"][0]], q["expected_document_ids"], a.k))
        g = aggregate(per_q); toks = [r["metadata"]["token_count"] for r in recs]
        rows.append(dict(config=name, target_tokens=tgt, overlap_tokens=ov, n_chunks=len(recs), mean_chunk_tokens=round(sum(toks) / len(toks), 1),
                         max_chunk_tokens=max(toks), chunks_over_model_max_seq=(sum(t > max_seq for t in toks) if max_seq else "n/a"),
                         **{f"precision@{a.k}": round(g["precision"], 3), f"recall@{a.k}": round(g["recall"], 3), "mrr": round(g["rr"], 3), f"hit_rate@{a.k}": round(g["hit"], 3)},
                         n_questions=len(qs), context_sufficiency_manual="", notes="Fill context_sufficiency_manual after reading retrieved chunks"))
        log.info("%s", rows[-1])
    write_csv(root / "evaluation" / "chunking_comparison.csv", list(rows[0].keys()), rows)
    log.info("Wrote evaluation/chunking_comparison.csv. NOTE: chunks longer than the model max_seq_length are truncated when embedded, "
             "which penalises the larger configs for MiniLM (256 tokens). Pick by results, not by size.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["draft-questions", "suggest", "evaluate", "compare-chunking"])
    ap.add_argument("--root"); ap.add_argument("--k", type=int, default=5); ap.add_argument("--top-k", type=int, default=10); ap.add_argument("--force", action="store_true")
    a = ap.parse_args(); root = get_root(a.root); (root / "evaluation").mkdir(exist_ok=True)
    log = setup_logging("eval", root / "reports" / "pipeline.log")
    {"draft-questions": cmd_draft, "suggest": cmd_suggest, "evaluate": cmd_evaluate, "compare-chunking": cmd_compare}[a.command](a, root, log)


if __name__ == "__main__":
    main()
