"""Shared helpers for the RAG preparation scripts (paths, CSV/JSON IO, registry loading, tokenizer, embedder)."""
import csv, hashlib, json, logging, re, sys
from datetime import datetime
from pathlib import Path

UNKNOWN_VALUES = {"", "unknown", "not specified", "n/a", "none", "null", "nan", "pending"}


def get_root(arg=None) -> Path:
    """Project root = --root if given, else the folder above scripts/ (so no hardcoded paths)."""
    return Path(arg).resolve() if arg else Path(__file__).resolve().parents[1]


def setup_logging(name="kb", logfile: Path = None):
    handlers = [logging.StreamHandler(sys.stdout)]
    if logfile:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(logfile, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers, force=True)
    return logging.getLogger(name)


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_csv(p: Path):
    p = Path(p)
    if not p.exists() or p.stat().st_size == 0:
        return []
    with open(p, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(p: Path, fields, rows):
    p = Path(p); p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore"); w.writeheader(); w.writerows(rows)


def read_json(p: Path, default=None):
    p = Path(p)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def write_json(p: Path, obj):
    p = Path(p); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_unknown(v) -> bool:
    return v is None or str(v).strip().lower() in UNKNOWN_VALUES


def null_if_unknown(v):
    return None if is_unknown(v) else str(v).strip()


def guard_overwrite(path: Path, force: bool, log):
    """Never silently overwrite existing outputs."""
    if Path(path).exists() and not force:
        log.error("%s already exists. Re-run with --force to overwrite (a .bak copy is kept).", path)
        sys.exit(2)
    if Path(path).exists() and force:
        bak = Path(str(path) + ".bak")
        Path(path).replace(bak)
        log.info("Existing %s moved to %s", path.name, bak.name)


# ---------------------------------------------------------------- registry / documents
def resolve_original(root: Path, rel: str) -> Path:
    """original_file_path in source_registry.csv was written relative to wherever collect_kb.py ran."""
    p = Path(rel)
    cands = [p, root / p, root.parent / p]
    if p.parts and p.parts[0] == root.name:
        cands.append(root / Path(*p.parts[1:]))
    for c in cands:
        if c.exists():
            return c
    return root / p


def cleaned_path_for(root: Path, row) -> Path:
    return root / "cleaned_documents" / (Path(row["original_file_path"]).stem + ".txt")


def split_header(text: str):
    """collect_kb.py writes SOURCE_URL/TITLE/ORGANIZATION/DATE_ACCESSED lines, blank line, then the body."""
    header, lines = {}, text.splitlines()
    i = 0
    while i < len(lines) and re.match(r"^(SOURCE_URL|TITLE|ORGANIZATION|DATE_ACCESSED):", lines[i]):
        k, v = lines[i].split(":", 1); header[k] = v.strip(); i += 1
    return header, "\n".join(lines[i:]).lstrip("\n")


def load_included_documents(root: Path, log=None):
    """Documents the manifest marks as included, with their cleaned text and registry metadata."""
    manifest = read_csv(root / "metadata" / "ingestion_manifest.csv")
    if not manifest:
        raise SystemExit("metadata/ingestion_manifest.csv missing or empty - run scripts/validate_documents.py first.")
    reg = {r["document_id"]: r for r in read_csv(root / "metadata" / "source_registry.csv")}
    docs = []
    for m in manifest:
        if m["inclusion_status"] != "included":
            continue
        r = reg.get(m["document_id"])
        p = root / m["cleaned_text_path"]
        if r is None or not p.exists():
            raise SystemExit(f"Manifest lists {m['document_id']} but registry row or cleaned text is missing: {p}")
        header, body = split_header(p.read_text(encoding="utf-8", errors="replace"))
        docs.append(dict(registry=r, manifest=m, header=header, body=body, cleaned_path=p))
    return docs


# ---------------------------------------------------------------- tokenizer
_TOK = {}


def get_token_counter(model_name: str, log=None):
    """Token counter using the embedding model's own tokenizer (so chunk sizes match what the model sees).
    Falls back to ~1.3 tokens/word with a loud warning if the tokenizer cannot be loaded."""
    if model_name in _TOK:
        return _TOK[model_name]
    try:
        from transformers import AutoTokenizer
        tok = AutoTokenizer.from_pretrained(model_name); tok.model_max_length = 10 ** 9
        fn = lambda t: len(tok.encode(t, add_special_tokens=False, verbose=False))
        fn.kind = f"tokenizer:{model_name}"
    except Exception as e:  # noqa
        (log or logging.getLogger("kb")).warning("Tokenizer for %s unavailable (%s). Using words*1.3 approximation.", model_name, e)
        fn = lambda t: int(round(len(re.findall(r"\S+", t)) * 1.3))
        fn.kind = "approx_words_x1.3"
    _TOK[model_name] = fn
    return fn


# ---------------------------------------------------------------- embedder
def load_sentence_model(model_name: str):
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(model_name)


class Embedder:
    def __init__(self, model_name, query_prefix="", doc_prefix="", normalize=True, loader=None):
        self.model_name, self.query_prefix, self.doc_prefix, self.normalize = model_name, query_prefix, doc_prefix, normalize
        self.model = (loader or load_sentence_model)(model_name)

    def encode_docs(self, texts, batch_size=32, show_progress=True):
        return self.model.encode([self.doc_prefix + t for t in texts], batch_size=batch_size, normalize_embeddings=self.normalize,
                                 show_progress_bar=show_progress, convert_to_numpy=True)

    def encode_queries(self, texts, batch_size=32):
        return self.model.encode([self.query_prefix + t for t in texts], batch_size=batch_size, normalize_embeddings=self.normalize,
                                 show_progress_bar=False, convert_to_numpy=True)


def embed_text_for(chunk: dict) -> str:
    """Text actually embedded = document title + section heading + chunk text (plain concatenation, no LLM rewriting).
    The stored/retrieved chunk text stays the original text."""
    md = chunk["metadata"]
    head = md["document_title"] + (" - " + md["section_heading"] if md.get("section_heading") else "")
    return head + "\n" + chunk["text"]


def embed_hash(chunk: dict, model_name: str, doc_prefix: str = "") -> str:
    return sha256(f"{model_name}|{doc_prefix}|{embed_text_for(chunk)}")
