"""Structure-aware chunker.

Pipeline per document:
  cleaned text -> blocks (heading / paragraph / list / table, with heading path + PDF page)
               -> glue intro sentences ending in ':' to the list/table that follows
               -> pack units into chunks *within a section only* (sections are never mixed, except that
                  undersized sibling sections under the same parent may be merged to avoid tiny chunks)
               -> oversized units are split by sentence / list item / table row, then by tokens as last resort
               -> overlap (last sentences of previous chunk, <= overlap tokens) only between chunks of the same section
"""
import re
from dataclasses import dataclass, field

PAGE_RX = re.compile(r"^=== PAGE (\d+) ===\s*$")
HEAD_RX = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
LIST_RX = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
TABLE_RX = re.compile(r"^\s*\|")
SENT_RX = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")


@dataclass
class Unit:
    kind: str            # paragraph | list | table
    text: str
    path: tuple          # heading path
    page: int = None
    tokens: int = 0


@dataclass
class Chunk:
    text: str
    path: tuple
    page_start: int = None
    page_end: int = None
    tokens: int = 0
    overlap_tokens: int = 0
    headings: list = field(default_factory=list)


# ------------------------------------------------------------------ parsing
def _looks_like_pdf_heading(line, prev_line):
    w = line.split()
    if not (1 <= len(w) <= 9) or re.search(r"[.,;:?!]$", line) or not line[0].isupper() or re.match(r"^\d+$", line):
        return False
    return prev_line is None or prev_line == "" or bool(re.search(r"[.!?:]$", prev_line))


def parse_blocks(body: str, pdf_headings=True):
    lines = body.splitlines()
    is_pdf = any(PAGE_RX.match(l) for l in lines)
    stack, page, blocks, para, lst, tbl = {}, None, [], [], [], []

    def path():
        return tuple(stack[k] for k in sorted(stack))

    def flush():
        nonlocal para, lst, tbl
        if para:
            blocks.append(Unit("paragraph", " ".join(s.strip() for s in para), path(), page)); para = []
        if lst:
            blocks.append(Unit("list", "\n".join(lst), path(), page)); lst = []
        if tbl:
            blocks.append(Unit("table", "\n".join(tbl), path(), page)); tbl = []

    prev = None
    for raw in lines:
        line = raw.rstrip(); s = line.strip()
        m = PAGE_RX.match(s)
        if m:
            flush(); page = int(m.group(1)); prev = ""; continue
        h = HEAD_RX.match(s)
        if h:
            flush(); lvl = len(h.group(1))
            for k in [k for k in stack if k >= lvl]:
                del stack[k]
            stack[lvl] = h.group(2).strip(); prev = ""; continue
        if not s:
            flush(); prev = ""; continue
        if is_pdf and pdf_headings and not LIST_RX.match(s) and _looks_like_pdf_heading(s, prev):
            flush(); lvl = 2
            for k in [k for k in stack if k >= lvl]:
                del stack[k]
            stack[lvl] = s; prev = ""; continue
        if TABLE_RX.match(s):
            if para or lst: flush()
            tbl.append(s)
        elif LIST_RX.match(line):
            if para or tbl: flush()
            lst.append(s)
        elif lst and raw.startswith((" ", "\t")):       # wrapped list item
            lst[-1] += " " + s
        else:
            if lst or tbl: flush()
            para.append(s)
        prev = s
    flush()
    return blocks, is_pdf


# ------------------------------------------------------------------ splitting oversized units
def _split_sentences(text):
    return [s for s in SENT_RX.split(text) if s.strip()]


def _hard_split(text, count, limit):
    words, out, cur = text.split(), [], []
    for w in words:
        cur.append(w)
        if count(" ".join(cur)) >= limit:
            out.append(" ".join(cur)); cur = []
    if cur:
        out.append(" ".join(cur))
    return out


def _pieces(unit: Unit, count, limit):
    """Split a unit into pieces <= limit tokens, at the most natural boundaries available."""
    if unit.kind == "list" or unit.kind == "table":
        parts = unit.text.split("\n")
        header = parts[:2] if unit.kind == "table" and len(parts) > 2 else []
        body = parts[len(header):]
    else:
        header, body = [], _split_sentences(unit.text)
    join = " " if unit.kind == "paragraph" else "\n"
    out, cur = [], []
    pre = join.join(header)

    def emit(items):
        if items:   # repeat the table header row on continuation pieces so each piece stays readable
            out.append("\n".join(([pre] if pre and out else []) + items) if join == "\n"
                       else join.join(items))

    for item in body:
        if count(item) > limit:                           # single sentence/item too long -> token split
            emit(cur); cur = []
            out.extend(_hard_split(item, count, limit)); continue
        if cur and count(join.join(cur + [item])) > limit:
            emit(cur); cur = [item]
        else:
            cur.append(item)
    emit(cur)
    return out


def _glue_intro(units):
    """Keep a lead-in sentence (ending ':') together with the list/table that follows."""
    out, i = [], 0
    while i < len(units):
        u = units[i]
        if (u.kind == "paragraph" and u.text.rstrip().endswith(":") and i + 1 < len(units)
                and units[i + 1].kind in ("list", "table") and units[i + 1].path == u.path):
            n = units[i + 1]
            out.append(Unit("list", u.text + "\n" + n.text, u.path, u.page)); i += 2
        else:
            out.append(u); i += 1
    return out


def _tail_overlap(text, count, overlap):
    if overlap <= 0:
        return "", 0
    pieces = [p for line in text.split("\n") for p in (_split_sentences(line) or [line]) if p.strip()]
    keep, tok = [], 0
    for p in reversed(pieces):
        t = count(p)
        if tok + t > overlap:
            break
        keep.insert(0, p); tok += t
    return " ".join(keep), tok


# ------------------------------------------------------------------ chunking
def chunk_document(body, count, target=500, overlap=100, min_tokens=100, max_tokens=700, pdf_headings=True):
    blocks, is_pdf = parse_blocks(body, pdf_headings)
    for b in blocks:
        b.tokens = count(b.text)
    # group into sections (consecutive blocks with the same heading path)
    sections, cur = [], []
    for b in blocks:
        if cur and cur[0].path != b.path:
            sections.append(cur); cur = []
        cur.append(b)
    if cur:
        sections.append(cur)

    chunks = []
    for sec in sections:
        units = _glue_intro(sec)
        expanded = []
        for u in units:
            u.tokens = count(u.text)
            if u.tokens > max_tokens:
                for p in _pieces(u, count, target):
                    expanded.append(Unit(u.kind, p, u.path, u.page, count(p)))
            else:
                expanded.append(u)
        sec_chunks, cur_units, cur_tok, ov_text, ov_tok = [], [], 0, "", 0

        def flush():
            nonlocal cur_units, cur_tok, ov_text, ov_tok
            if not cur_units:
                return
            body_text = "\n\n".join(x.text for x in cur_units)
            full = (ov_text + "\n\n" if ov_text else "") + body_text
            pages = [x.page for x in cur_units if x.page is not None]
            sec_chunks.append(Chunk(full, cur_units[0].path, min(pages) if pages else None, max(pages) if pages else None,
                                    count(full), ov_tok, [cur_units[0].path[-1]] if cur_units[0].path else []))
            ov_text, ov_tok = _tail_overlap(body_text, count, overlap)
            cur_units, cur_tok = [], ov_tok

        cur_tok = 0
        for u in expanded:
            limit = max_tokens if (cur_tok - ov_tok) < min_tokens else target
            if cur_units and cur_tok + u.tokens > limit:
                flush()
            cur_units.append(u); cur_tok += u.tokens
        flush()
        chunks.extend(sec_chunks)

    return _merge_small(chunks, count, min_tokens, max_tokens), is_pdf


def _merge_small(chunks, count, min_tokens, max_tokens):
    """Merge undersized chunks with a *related* neighbour: a sibling (same parent heading) or an ancestor/descendant
    section of the same article. Unrelated sections are never combined. Heading lines of absorbed sections are kept in the text."""
    def parent(c): return c.path[:-1] if c.path else ()
    def is_anc(a, d): return len(d.path) > len(a.path) and d.path[:len(a.path)] == a.path
    def related(a, b): return parent(a) == parent(b) or is_anc(a, b) or is_anc(b, a)

    def absorb(base, other):
        add = other.text if other.path == base.path else (f"{other.path[-1]}\n{other.text}" if other.path else other.text)
        base.text += "\n\n" + add; base.tokens = count(base.text)
        if other.page_end is not None:
            base.page_end = other.page_end
        if base.page_start is None:
            base.page_start = other.page_start
        if other.path != base.path and other.path:
            base.headings = (base.headings or ([base.path[-1]] if base.path else [])) + [other.path[-1]]

    changed = True
    while changed:
        changed, out, i = False, [], 0
        while i < len(chunks):
            c = chunks[i]
            small = (c.tokens - c.overlap_tokens) < min_tokens
            if small and out and related(out[-1], c) and out[-1].tokens + c.tokens <= max_tokens:
                absorb(out[-1], c); changed = True
            elif small and i + 1 < len(chunks) and related(c, chunks[i + 1]) and c.tokens + chunks[i + 1].tokens <= max_tokens:
                absorb(c, chunks[i + 1]); out.append(c); i += 1; changed = True
            else:
                out.append(c)
            i += 1
        chunks = out
    return chunks