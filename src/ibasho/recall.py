"""`ibasho recall`: search the agent's own memory folder and print short, dated, ranked snippets.

    ibasho recall WORDS [--in FOLDER[,FOLDER]] [-n N] [--full]

WORDS are plain words ("what do I know about the lighthouse", "harbour market"); question words drop out, and
"quoted phrases" must appear as written. --in narrows the search to top-level folders of the memory folder (or
`top` for the files directly in it); the default is everything.

Every Markdown file is cut into chunks (a paragraph, a list item, or a line of a notes-style file) and scored by
term hits weighted by rarity, how many of the query words a chunk covers, hits in the file's title, headings or
name, and a mild lift for recent dates. Files named by date (YYYY-MM-DD.md, a journal) date their chunks; elsewhere a
chunk takes the latest date written in it or above it. At most two chunks per file are shown; "more in:" lists where
the rest matched.

Generalized from a recall tool a model wrote for its own memory. Stdlib only, so it costs nothing to run often.
"""

from __future__ import annotations

import math
import re
import textwrap
from collections import Counter
from datetime import date
from pathlib import Path

STOPWORDS = set("""
a an the and or of to in on at by for from with about into over
what whats who whom whose where when why how which
is are was were be been being am do does did done
i me my mine myself we us our you your it its that this these those there here
know knew remember recall tell find anything something any some all
have has had can could should would will
""".split())

PER_FILE = 2
SNIPPET_CHARS = 300
DATE_RE = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")
LINK_RE = re.compile(r"\]\(([^)\s]+\.md)\)")
WORD_RE = re.compile(r"\w+")


def words(text: str) -> list[str]:
    return [w for w in WORD_RE.findall(text.lower()) if len(w) > 1 or w.isdigit()]


def stem(w: str) -> str:
    if w.endswith("'s"):
        w = w[:-2]
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith(("ches", "shes", "sses", "xes")):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    if len(w) > 5 and w.endswith("ing"):
        return w[:-3]
    if len(w) > 4 and w.endswith("ed"):
        return w[:-2]
    return w


def scope_of(rel: str) -> str:
    return rel.split("/", 1)[0] if "/" in rel else "top"


class Chunk:
    def __init__(self, rel: str, line: int, title: str | None, heading: str | None, when: str | None, text: str,
                 first: bool = False, links: tuple[str, ...] = ()):
        self.rel, self.scope, self.line = rel, scope_of(rel), line
        self.title, self.heading, self.date, self.text = title, heading, when, text
        self.first = first   # a file's opening chunk is usually its summary, so it counts the title as its own
        self.counts = Counter(words(" ".join([text, title or ""]) if first else text))
        name = Path(rel).parent.name if Path(rel).name.lower() == "readme.md" else Path(rel).stem
        self.head_words = set(words(" ".join([title or "", heading or "", name.replace("-", " ")])))
        self.links = links
        # Lines that mostly point at other files ("Pictures: ...", "See ...") rank low.
        pointer = sum(len(x) for x in re.findall(r"\]\([^)]*\)|`[^`]*[/.][^`]*`|\S+\.(?:md|png|jpg)\b", text))
        self.pointer = pointer > 0.4 * len(text)


def blocks(lines: list[str], prose_lines: bool = False):
    """(first line number, lines) for each paragraph or list item; headings and rules on their own. With
    prose_lines, a line starting a new sentence after a finished one starts a block too, since notes often keep one
    fact per line without blank lines between."""
    cur, start = [], 0
    for i, line in enumerate(lines, 1):
        s = line.rstrip()
        if not s.strip():
            if cur:
                yield start, cur
            cur = []
            continue
        is_head = s.startswith("#") or s.strip() == "---"
        is_item = re.match(r"(- |\* |\d+\. )", s) is not None
        new_fact = prose_lines and cur and re.match(r"[A-Z0-9*`(]", s) and re.search(r"[.!?)]$", cur[-1])
        if is_head or (is_item and cur) or new_fact:
            if cur:
                yield start, cur
            cur = []
        if is_head:
            yield i, [s]
            continue
        if not cur:
            start = i
        cur.append(s)
    if cur:
        yield start, cur


def chunk_file(root: Path, path: Path) -> list[Chunk]:
    rel = path.relative_to(root).as_posix()
    text = path.read_text(encoding="utf-8", errors="replace")
    day = path.stem if DATE_RE.fullmatch(path.stem) else None   # a journal-style file
    title, heading, label, last_date = None, None, None, day
    out = []
    for line_no, blk in blocks(text.splitlines(), prose_lines=not day):
        first = blk[0]
        if first.strip() == "---":
            label = None
            continue
        m = re.match(r"(#+)\s+(.*)", first)
        if m and len(blk) == 1:
            if len(m.group(1)) == 1 and title is None:
                title = m.group(2).strip()
            else:
                heading = m.group(2).strip()
            continue
        body = " ".join(line.strip() for line in blk)
        if len(body.split()) < 3:
            continue
        if day:
            lead = re.match(r"\*\*(.+?)\*\*", body)   # a dated entry often opens with a bold label
            if lead:
                label = lead.group(1).strip().rstrip(".")
            when = day
        else:
            when = max((m.group(0) for m in DATE_RE.finditer(body)), default=None) or last_date
            last_date = when
        links = tuple((path.parent / l).resolve().relative_to(root.resolve()).as_posix()
                      for l in LINK_RE.findall(body) if _inside(path.parent / l, root))
        out.append(Chunk(rel, line_no, title, (label or heading) if day else heading, when, body,
                         first=not out and not day, links=links))
    return out


def _inside(p: Path, root: Path) -> bool:
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def load(root: Path, scopes: set[str]) -> list[Chunk]:
    chunks = []
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root).as_posix()
        if any(part.startswith(".") for part in Path(rel).parts):
            continue
        if "all" in scopes or scope_of(rel) in scopes:
            chunks.extend(chunk_file(root, path))
    return chunks


def parse_query(q: str) -> tuple[list[str], list[str]]:
    phrases = [p.strip().lower() for p in re.findall(r'"([^"]+)"', q) if p.strip()]
    raw = words(re.sub(r'"[^"]*"', " ", q))
    terms = [w for w in raw if w not in STOPWORDS] or raw
    for p in phrases:
        terms.extend(words(p))
    return list(dict.fromkeys(terms)), phrases


def term_matcher(term: str, vocab: set[str]) -> dict[str, float]:
    """Weights for every vocabulary word that counts as a hit for term."""
    st = stem(term)
    hits = {}
    for w in vocab:
        if w == term:
            hits[w] = 1.0
        elif stem(w) == st:
            hits[w] = 0.9
        elif len(term) >= 4 and w.startswith(term):
            hits[w] = 0.5
    return hits


def norm(text: str) -> str:
    return " ".join(words(text))


def score_chunks(chunks: list[Chunk], terms: list[str], phrases: list[str], today: date | None = None):
    vocab = set()
    for c in chunks:
        vocab.update(c.counts)
        vocab.update(c.head_words)
    matchers = {t: term_matcher(t, vocab) for t in terms}
    n = len(chunks)
    df = {t: sum(1 for c in chunks if any(w in c.counts for w in matchers[t])) for t in terms}
    idf = {t: math.log(1 + n / (1 + df[t])) for t in terms}
    phrase_norm = [norm(p) for p in phrases]
    whole = norm(" ".join(terms)) if len(terms) > 1 else None
    today = today or date.today()
    scored = []
    for c in chunks:
        s, covered = 0.0, 0
        for t in terms:
            m = matchers[t]
            tf = sum(wt * c.counts[w] for w, wt in m.items() if w in c.counts)
            in_head = any(w in c.head_words for w in m)
            if tf:
                s += idf[t] * (1 + math.log(1 + tf))
            if in_head:
                s += 0.8 * idf[t]
            if tf or in_head:
                covered += 1
        if not s:
            continue
        cn = norm(c.text) if (phrase_norm or whole) else ""
        if phrase_norm and not all(p in cn for p in phrase_norm):
            continue
        s *= (covered / len(terms)) ** 1.5
        if whole and whole in cn:
            s *= 1.5
        s /= (1 + sum(c.counts.values()) / 200) ** 0.3
        if c.pointer:
            s *= 0.3
        if c.first:
            s *= 1.2
        if c.date:
            try:
                age = (today - date.fromisoformat(c.date)).days
                s *= 1 + 0.25 * 0.5 ** (max(age, 0) / 14)
            except ValueError:
                pass
        scored.append((s, c))
    scored.sort(key=lambda x: -x[0])
    return scored, matchers


def snippet(c: Chunk, matchers: dict, width: int) -> str:
    sentences = [x for x in re.split(r"(?<=[.!?;:])\s+", c.text) if x]

    def hits(sent):
        ws = Counter(words(sent))
        return sum(wt * ws[w] for m in matchers.values() for w, wt in m.items() if w in ws)

    scores = [hits(x) for x in sentences]
    best = max(range(len(sentences)), key=lambda i: scores[i])
    lo = hi = best
    out = sentences[best]
    while True:
        grew = False
        for j in (hi + 1, lo - 1):
            if 0 <= j < len(sentences) and len(out) + len(sentences[j]) + 1 <= width:
                if j > hi:
                    out, hi = out + " " + sentences[j], j
                else:
                    out, lo = sentences[j] + " " + out, j
                grew = True
        if not grew:
            break
    cut_left, cut_right = lo > 0, hi < len(sentences) - 1
    if len(out) > width:
        low = out.lower()
        found = [p for m in matchers.values() for w in m if (p := low.find(w)) >= 0]
        a = max(0, min(found, default=0) - width // 3)
        cut_left, cut_right = cut_left or a > 0, cut_right or a + width < len(out)
        out = out[a:a + width]
    return ("... " if cut_left else "") + out.strip() + (" ..." if cut_right else "")


def label(c: Chunk) -> str:
    if DATE_RE.fullmatch(Path(c.rel).stem):
        return (f"{c.date} " + (c.heading or "")[:60]).strip()
    bits = [b for b in (c.title and c.title[:60], c.heading and c.heading != c.title and c.heading[:40]) if b]
    return (" / ".join(bits) + (f"  ({c.date})" if c.date else "")).strip()


def recall(root: Path, query: str, scopes: set[str] | None = None, n: int = 6, full: bool = False,
           today: date | None = None) -> list[str]:
    """The lines `ibasho recall` prints."""
    scopes = scopes or {"all"}
    terms, phrases = parse_query(query)
    if not terms:
        raise ValueError("nothing to search for")
    scored, matchers = score_chunks(load(root, scopes), terms, phrases, today)
    # An index file's line (places.md over places/) pointing at a file that matched too says less than the file.
    matched = {c.rel for _, c in scored}

    def redundant(c: Chunk) -> bool:
        folder = Path(c.rel).with_suffix("").as_posix() + "/"
        return any(l in matched and l.startswith(folder) for l in c.links)

    scored = [(s, c) for s, c in scored if not redundant(c)]
    files = Counter(c.rel for _, c in scored)
    shown, per_file = [], Counter()
    for s, c in scored:
        if len(shown) >= n:
            break
        own_file = all(any(w in c.head_words for w in matchers[t]) for t in terms)
        if per_file[c.rel] >= PER_FILE + own_file:
            continue
        per_file[c.rel] += 1
        shown.append(c)
    where = ", ".join(sorted(scopes))
    out = [f'recall "{" ".join(terms)}" in {where}: {len(scored)} matching chunks in {len(files)} files']
    width = 10_000 if full else SNIPPET_CHARS
    for i, c in enumerate(shown, 1):
        out.append(f"{i}. {c.rel}:{c.line}  {label(c)}".rstrip())
        text = c.text if full else snippet(c, matchers, width)
        out.append(textwrap.fill(text, 108, initial_indent="   ", subsequent_indent="   "))
    rest = [(rel, k - per_file[rel]) for rel, k in files.items() if k > per_file[rel]]
    if rest:
        more = ", ".join(r + (f" ({k})" if k > 1 else "") for r, k in rest[:8])
        out.append(f"more in: {more}" + (f" +{len(rest) - 8} files" if len(rest) > 8 else ""))
    return out


# ---------- the command ----------

def register(sub):
    p = sub.add_parser("recall", help="search my memory folder: ranked, dated snippets")
    p.add_argument("query", nargs="+")
    p.add_argument("--in", dest="scope", default="all",
                   help="top-level folders to search, comma-separated (`top` = the files directly in it)")
    p.add_argument("-n", type=int, default=6, help="snippets to show")
    p.add_argument("--full", action="store_true", help="whole chunks instead of snippets")
    p.add_argument("--dir", help="the folder to search (default: the keeper's life folder)")
    p.set_defaults(fn=run)


def run(ctx, a):
    from .cli.common import Fail
    if a.dir:
        root = ctx.agent_path(a.dir, "folder")
    else:
        from .config import ConfigError
        from .keeper import settings as ksettings
        try:
            root = ksettings.life_dir(ctx.cfg, ksettings.load(ctx.cfg))
        except ConfigError as e:
            raise Fail(str(e)) from None
    if not root.is_dir():
        raise Fail(f"no memory folder at {root}")
    scopes = {s.strip() for s in a.scope.split(",") if s.strip()}
    known = {"all", "top"} | {p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")}
    if bad := scopes - known:
        raise Fail(f"no folder {', '.join(sorted(bad))} in {root} (have: {', '.join(sorted(known))})")
    try:
        print("\n".join(recall(root, " ".join(a.query), scopes, a.n, a.full)))
    except ValueError as e:
        raise Fail(str(e)) from None
