#!/usr/bin/env python3
"""legal-pdf-triage (lpt): index a large legal PDF on disk so only the
needed slices ever enter the conversation.

Commands
  index   <pdf> [--out DIR]           extract text per page, split Nexis exports
                                      into documents, write a compact index.md
  search  <DIR> <regex> [--ctx N] [--max N] [--doc N]
                                      page-cited hits with short snippets
  pages   <DIR> <range> [--max-chars N]   e.g. 12  or  12-15
  section <DIR> <doc#> <name> [--max-chars N]
                                      e.g. 3 overview | holding | headnotes | opinion | dissent
  topic   <DIR> <doc#> <n>            annotated statutes: one Notes-to-Decisions topic
  cites   <DIR> [doc#] [--topic n] [--bluebook] [--max N]
                                      unique case + statute citations, Bluebook form
  stats   <DIR>                       rough token cost of the whole file vs. index
"""
import argparse, json, os, re, subprocess, sys
from pathlib import Path

# ---------- patterns ----------
END_DOC = re.compile(r"^\s*End of Document\s*$", re.M)
CITE = re.compile(
    r"\b\d{1,4}\s+(?:U\.S\.|S\.\s?Ct\.|L\.\s?Ed\.(?:\s?2d)?|F\.(?:\s?(?:2d|3d|4th))?|"
    r"F\.\s?Supp\.(?:\s?(?:2d|3d))?|[A-Z][a-z]*\.?\s?(?:2d|3d)|So\.\s?(?:2d|3d)?|"
    r"N\.E\.(?:2d|3d)?|N\.W\.(?:2d)?|S\.E\.(?:2d)?|S\.W\.(?:2d|3d)?|P\.(?:2d|3d)?|"
    r"A\.(?:2d|3d)?|N\.Y\.(?:2d|3d)?|[A-Z][a-z]{1,4}\.(?:\s?App\.)?(?:\s?(?:2d|3d))?)\s+\d{1,5}\b"
)
LEXIS = re.compile(r"\b\d{4}\s+[A-Z][A-Za-z.\s]{1,20}LEXIS\s+\d+\b")
DATE = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+\d{1,2},\s+\d{4}\b"
)
COURT = re.compile(
    r"^(?:Supreme Court of [^\n]{2,60}|United States Court of Appeals[^\n]{0,60}|"
    r"United States District Court[^\n]{0,80}|Court of Appeals of [^\n]{2,60}|"
    r"Court of Appeal[^\n]{0,60}|Supreme Court of the United States)\s*$",
    re.M | re.I,
)
# Section headings seen in Nexis Uni case exports and general opinions
SECTIONS = {
    "overview":   r"(?:Case Summary|Overview)",
    "outcome":    r"Outcome",
    "procedural": r"Procedural Posture",
    "holding":    r"(?:Outcome|Holdings?)",
    "headnotes":  r"(?:LexisNexis.{0,3} Headnotes|Headnotes)",
    "coreterms":  r"Core Terms",
    "counsel":    r"Counsel",
    "judges":     r"Judges",
    "opinion":    r"(?:Opinion(?: by:)?|OPINION)",
    "concur":     r"(?:Concur by:|Concurrence|CONCUR)",
    "dissent":    r"(?:Dissent by:|Dissent|DISSENT)",
}
SEC_RE = {k: re.compile(r"^\s*" + v + r"\b.*$", re.M) for k, v in SECTIONS.items()}
ORDER = ["overview", "procedural", "outcome", "coreterms", "headnotes",
         "counsel", "judges", "opinion", "concur", "dissent"]


# Annotated-statute exports (Nexis "Annotated Statutes")
STAT_SECTIONS = {
    "text":        r"§\s*[\d.]+[\w.\-]*\.?\s",
    "history":     r"History",
    "amendments":  r"Amendment Notes",
    "decisions":   r"Notes to Decisions",
    "unpublished": r"Notes to Unpublished Decisions",
    "agopinions":  r"(?:Opinion Notes|OPINIONS OF ATTORNEY GENERAL)",
    "research":    r"(?:Research References(?: & Practice Aids)?|RESEARCH REFERENCES)",
}
STAT_RE = {k: re.compile(r"^\s*" + v + r".*$", re.M) for k, v in STAT_SECTIONS.items()}
STAT_ORDER = list(STAT_SECTIONS)
PAGE_HDR = re.compile(r"^\s*Page \d+ of \d+\s*$", re.M)


def strip_headers(pages):
    """Drop 'Page N of M' lines and the running title repeated atop each page."""
    pages = [PAGE_HDR.sub("", p) for p in pages]
    firsts = [next((l.strip() for l in p.splitlines() if l.strip()), "") for p in pages]
    if len(pages) > 3:
        top = max(set(firsts), key=firsts.count)
        if top and firsts.count(top) > len(pages) * 0.6:
            pages = [p.replace(top, "", 1) if f == top else p for p, f in zip(pages, firsts)]
    return pages


def is_statute(text):
    return bool(re.search(r"^\s*§\s*[\d.]+", text, re.M) and
                re.search(r"^\s*History\s*$", text, re.M) and
                re.search(r"^\s*(?:Annotations|Notes to Decisions)\s*$", text, re.M))


def norm_with_pages(pages, s, e):
    """Whitespace-collapsed text for pages s..e plus offset->page lookup."""
    parts, starts, pos = [], [], 0
    for p in range(s, e + 1):
        t = re.sub(r"\s+", " ", pages[p - 1]).strip() + " "
        starts.append((pos, p)); parts.append(t); pos += len(t)
    full = "".join(parts)
    def page_at(off):
        pg = starts[0][1]
        for st, p in starts:
            if st <= off: pg = p
            else: break
        return pg
    return full, page_at


def statute_topics(pages, s, e):
    """Topic headings under Notes to Decisions: TOC list, then body dividers."""
    text = "".join(pages[s - 1:e])
    m = STAT_RE["decisions"].search(text)
    if not m:
        return [], None
    lines = text[m.end():].splitlines()
    topics, cur = [], None
    for ln in lines:
        st = ln.strip()
        if not st:
            if cur: topics.append(cur); cur = None
            continue
        if cur and cur.endswith(":"):
            cur += " " + st  # heading wrapped right after a colon
            continue
        if re.match(r"^[A-Z][A-Za-z&,'/()0-9\- ]+:\s", st) and len(st) < 140:
            if cur: topics.append(cur)
            cur = st
            if topics and cur == topics[0]:
                break  # TOC repeats -> body has begun
        elif cur and len(st) < 60 and not st.endswith("."):
            cur += " " + st  # wrapped heading line
        else:
            if cur: topics.append(cur); cur = None
            if topics: break
    topics = list(dict.fromkeys(re.sub(r"\s+", " ", t) for t in topics))
    full, page_at = norm_with_pages(pages, s, e)
    toc_at = full.find("Notes to Decisions")
    # body starts at the second appearance of the first topic
    body = full.find(topics[0], full.find(topics[0], toc_at) + 1) if topics else -1
    stop = min([x for x in (full.find("Notes to Unpublished Decisions", body),
                            full.find("Opinion Notes", body),
                            full.find("Research References", body)) if x > 0] or [len(full)])
    out, pos = [], body
    for t in topics:
        i = full.find(t, pos)
        if i < 0 or i > stop:
            out.append(dict(topic=t, page=None, start=None, n=0)); continue
        out.append(dict(topic=t, page=page_at(i), start=i, n=0)); pos = i + len(t)
    located = [o for o in out if o["start"] is not None]
    for a, b in zip(located, located[1:] + [None]):
        end = b["start"] if b else stop
        a["end"] = end
        a["n"] = len(LEXIS.findall(full[a["start"]:end]))
    return out, full


def toks(s):  # rough: ~4 chars per token
    return max(1, len(s) // 4)


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


# ---------- index ----------
def cmd_index(a):
    pdf = Path(a.pdf).resolve()
    out = Path(a.out or (pdf.parent / (pdf.stem + ".lpt"))).resolve()
    (out / "pages").mkdir(parents=True, exist_ok=True)

    unit = "p."
    if pdf.suffix.lower() == ".pdf":
        info = run(["pdfinfo", str(pdf)])
        npages = int(re.search(r"Pages:\s+(\d+)", info).group(1))
        # one pdftotext pass, split on form feed -> exact page mapping
        raw = run(["pdftotext", "-layout", str(pdf), "-"])
        pages = raw.split("\f")[:npages]
    else:  # .txt/.md export: use form feeds if present, else ~3500-char chunks
        raw = pdf.read_text(errors="replace")
        if "\f" in raw:
            pages = raw.split("\f")
        else:
            unit, pages, buf = "chunk ", [], ""
            for line in raw.splitlines(keepends=True):
                buf += line
                if len(buf) >= 3500:
                    pages.append(buf); buf = ""
            if buf:
                pages.append(buf)
        npages = len(pages)
    pages = strip_headers(pages)
    empty = 0
    for i, t in enumerate(pages, 1):
        if len(t.strip()) < 30:
            empty += 1
        (out / "pages" / f"{i:04d}.txt").write_text(t)

    # split into documents on Nexis "End of Document"; fall back to one doc
    docs, start = [], 1
    for i, t in enumerate(pages, 1):
        if END_DOC.search(t):
            docs.append((start, i)); start = i + 1
    if start <= len(pages) and (not docs or start <= len(pages)):
        tail = "".join(pages[start - 1:]).strip()
        if tail or not docs:
            docs.append((start, len(pages)))

    meta = []
    for n, (s, e) in enumerate(docs, 1):
        text = "".join(pages[s - 1:e])
        head = text[:6000]
        lines = [l.strip() for l in head.splitlines() if l.strip()]
        title = next((l for l in lines if re.search(r"\bv\.?\s", l) and len(l) < 160), lines[0][:150] if lines else "")
        cites = list(dict.fromkeys(CITE.findall(head) + LEXIS.findall(head)))[:4]
        court = (COURT.search(head).group(0).strip() if COURT.search(head) else "")
        date = (DATE.search(head).group(0) if DATE.search(head) else "")
        kind = "statute" if is_statute(text) else "case"
        regs, order = (STAT_RE, STAT_ORDER) if kind == "statute" else (SEC_RE, ORDER)
        secs = {}
        for k in order:
            for p in range(s, e + 1):
                if regs[k].search(pages[p - 1]):
                    secs[k] = p; break
        topics = []
        if kind == "statute":
            sec_line = re.search(r"^\s*(§\s*[\d.]+[^\n]{0,120})$", text, re.M)
            title = sec_line.group(1).strip() if sec_line else title
            cur = re.search(r"Current through[^*\n]*", text)
            date = cur.group(0).strip() if cur else ""
            court, cites = "", []
            topics, _ = statute_topics(pages, s, e)
            topics = [{k: v for k, v in t.items() if k in ("topic", "page", "n")} for t in topics]
        meta.append(dict(doc=n, kind=kind, pages=[s, e], title=title, cites=cites, court=court,
                         date=date, words=len(text.split()), sections=secs, topics=topics))

    # case captions anywhere in the file (casebooks, briefs, multi-case exports)
    CAP = re.compile(r"^\s{0,8}([A-Z][\w.'&,\- ]{1,70}\s+v\.\s+[A-Z][\w.'&,\- ]{1,70})\s*$", re.M)
    cases, seen = [], set()
    for i, t in enumerate(pages, 1):
        for mt in CAP.finditer(t):
            name = re.sub(r"\s+", " ", mt.group(1)).strip()
            win = t[mt.end(): mt.end() + 400]
            c = CITE.search(win) or LEXIS.search(win)
            key = name.lower()
            first = name.split()[0]
            prose = first in {"See","Also","The","In","Supreme","Court","Compare","Cf.","But","Accord","And","Of","Under","As","Unlike","Following","Weirum","Use"} or len(name.split(" v.")[0].split()) > 9
            if c and key not in seen and not prose:  # caption followed by a citation = case heading
                seen.add(key); cases.append(dict(page=i, name=name, cite=c.group(0)))

    (out / "meta.json").write_text(json.dumps(
        dict(pdf=str(pdf), npages=npages, unit=unit, empty_pages=empty, docs=meta,
             cases=cases), indent=1))

    # compact index.md (this is the only thing read up front)
    L = [f"# Index: {pdf.name}",
         f"{npages} pages, {len(meta)} document(s), ~{toks(raw):,} tokens if read whole."]
    if empty > npages * 0.3:
        L.append(f"WARNING: {empty} pages have little or no text — likely scanned; OCR needed.")
    L.append("")
    for m in meta:
        L.append(f"[{m['doc']}] pp.{m['pages'][0]}-{m['pages'][1]} | {m['title']}")
        extra = " | ".join(x for x in [m["court"], m["date"], "; ".join(m["cites"])] if x)
        if extra:
            L.append(f"    {extra}")
        if m["sections"]:
            L.append("    sections: " + ", ".join(f"{k}@p{v}" for k, v in m["sections"].items()))
        L.append(f"    ~{m['words']:,} words  [{m.get('kind', 'case')}]")
        if m.get("topics"):
            tot = sum(t["n"] for t in m["topics"])
            L.append(f"    Notes to Decisions: {len(m['topics'])} topics, ~{tot} annotations"
                     f"  (pull one: topic <dir> {m['doc']} <#>)")
            for i, t in enumerate(m["topics"], 1):
                where = f"p.{t['page']}" if t["page"] else "toc only"
                L.append(f"      {i:>2}. {t['topic']}  [{where}, {t['n']}]")
    if len(meta) == 1 and cases and meta[0].get("kind") != "statute":
        L.append(f"\n## Case headings found ({len(cases)})")
        for c in cases[:200]:
            L.append(f"{unit}{c['page']}  {c['name']} — {c['cite']}")
        if len(cases) > 200:
            L.append(f"... {len(cases) - 200} more in meta.json")
    if unit != "p.":
        L.insert(2, "Text input: locations are ~3,500-char chunks, not printed page numbers.")
    (out / "index.md").write_text("\n".join(L) + "\n")
    print(f"indexed -> {out}")
    print("\n".join(L[:60]))
    if len(L) > 60:
        print(f"... ({len(L) - 60} more lines in index.md)")


def load(d):
    d = Path(d)
    m = json.loads((d / "meta.json").read_text())
    pages = [p.read_text() for p in sorted((d / "pages").glob("*.txt"))]
    return m, pages


def clean(t):
    t = re.sub(r"[ \t]{2,}", " ", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def cap(t, n):
    return t if len(t) <= n else t[:n] + f"\n[...truncated at {n} chars; {len(t) - n} more]"


# ---------- search ----------
def cmd_search(a):
    m, pages = load(a.dir)
    rx = re.compile(a.regex, re.I)
    rng = range(1, len(pages) + 1)
    if a.doc:
        s, e = m["docs"][a.doc - 1]["pages"]; rng = range(s, e + 1)
    def doc_of(p):
        for d in m["docs"]:
            if d["pages"][0] <= p <= d["pages"][1]:
                return d["doc"]
    hits = 0
    for p in rng:
        t = re.sub(r"\s+", " ", pages[p - 1])
        last_end = -1
        for mt in rx.finditer(t):
            if mt.start() < last_end:
                continue  # inside previous snippet window
            last_end = mt.end() + a.ctx
            hits += 1
            if hits <= a.max:
                s0, e0 = max(0, mt.start() - a.ctx), min(len(t), mt.end() + a.ctx)
                print(f"[doc {doc_of(p)} {m.get('unit','p.')}{p}] ...{t[s0:e0]}...")
    print(f"-- {hits} hit(s){' (showing ' + str(a.max) + ')' if hits > a.max else ''}")


# ---------- pages ----------
def cmd_pages(a):
    _, pages = load(a.dir)
    s, _, e = a.range.partition("-")
    s, e = int(s), int(e or s)
    out = "\n".join(f"=== p.{p} ===\n{clean(pages[p - 1])}" for p in range(s, e + 1))
    print(cap(out, a.max_chars))


# ---------- section ----------
def cmd_section(a):
    m, pages = load(a.dir)
    d = m["docs"][a.doc - 1]
    s, e = d["pages"]
    text = "".join(pages[s - 1:e])
    k = a.name.lower()
    stat = d.get("kind") == "statute"
    regs, order = (STAT_RE, STAT_ORDER) if stat else (SEC_RE, ORDER)
    if k not in regs:
        sys.exit(f"unknown section; choose from {', '.join(order)}")
    mt = regs[k].search(text)
    if stat:
        if not mt:
            sys.exit(f"'{k}' not found in doc {a.doc}; use search instead")
        nxt = [x.start() for kk in order[order.index(k) + 1:]
               for x in [regs[kk].search(text, mt.end())] if x]
        end = min(nxt) if nxt else len(text)
        print(f"[doc {a.doc} '{d['title'][:80]}' — {k}, starts ~p.{d['sections'].get(k, '?')}]")
        print(cap(clean(text[mt.start():end]), a.max_chars))
        return
    if not mt:
        sys.exit(f"'{k}' not found in doc {a.doc}; use search instead")
    # end at the next known heading that starts after this one
    nxt = [x.start() for kk in ORDER if kk != k
           for x in [SEC_RE[kk].search(text, mt.end())] if x]
    end = min(nxt) if nxt and k not in ("opinion", "concur", "dissent") else len(text)
    if k == "opinion":  # stop at concurrence/dissent if present
        stops = [x.start() for kk in ("concur", "dissent")
                 for x in [SEC_RE[kk].search(text, mt.end())] if x]
        end = min(stops) if stops else len(text)
    page = s + text[:mt.start()].count("\f")
    print(f"[doc {a.doc} '{d['title'][:80]}' — {k}, starts ~p.{d['sections'].get(k, page)}]")
    print(cap(clean(text[mt.start():end]), a.max_chars))


def cmd_topic(a):
    m, pages = load(a.dir)
    d = m["docs"][a.doc - 1]
    s, e = d["pages"]
    topics, full = statute_topics(pages, s, e)
    if not topics:
        sys.exit("no Notes to Decisions topics in this document")
    t = topics[a.n - 1]
    if t["start"] is None:
        sys.exit(f"topic {a.n} appears only in the table of contents (no annotations)")
    body = full[t["start"] + len(t["topic"]): t["end"]].strip()
    # split into annotations: each ends with a LEXIS cite + parenthetical
    anns = re.split(r"(?<=\))\s+(?=[“\"A-Z])", body)
    anns = [x for x in anns if LEXIS.search(x)] or [body]
    out = [f"[doc {a.doc} topic {a.n}: {t['topic']} — starts p.{t['page']}, {len(anns)} annotation(s)]"]
    for i, x in enumerate(anns, 1):
        out.append(f"({i}) {x.strip()}")
    print(cap("\n\n".join(out), a.max_chars))


# ---------- citations ----------
# Nexis annotation tail: "Name, 884 So. 2d 241, 2004 Fla. App. LEXIS 11706 (Fla. 1st DCA 2004)."
TAIL = re.compile(
    r"(?P<rep>\d{1,4} (?:U\.S\.|S\. ?Ct\.|L\. ?Ed\. ?2d|F\. ?(?:2d|3d|4th)|F\. Supp\. ?(?:2d|3d)?|"
    r"F\. App'x|So\. ?(?:2d|3d)?|Fla\. L\. Weekly(?: Supp\.| Fed\. [A-Z])?|B\.R\.|"
    r"[A-Z][\w.]{0,10}(?: ?[23]d)?) \d{1,5})?"
    r"(?:, )?(?P<lexis>\d{4} [A-Z][\w.' ]{0,25}?LEXIS \d+)?"
    r"(?:, at \*?\d+)?\s?\((?P<court>[^()]{0,60}?(?:\d{4}|\.))\)")
HIST = re.compile(r"(cert\. denied|cert\. granted|aff'd(?: in part)?|rev'd(?: in part)?|vacated|"
                  r"dismissed|reh'g denied|review denied|modified|quashed|approved|disapproved|"
                  r"overruled|abrogated|superseded|remanded|appeal dismissed)", re.I)
ABBR_BREAK = {"Bus.", "Enters.", "Comm'n", "Cmty.", "Dev.", "Envtl.", "Fla.", "Hous.", "Invs.",
              "Med.", "Ret.", "Transp.", "Am.", "Inc.", "Co.", "Corp.", "Sys.", "Bros.", "Mfg.", "Ins.", "Fin.", "Sav.",
              "Ltd.", "Fed.", "Nat.", "Gen.", "Mut.", "Assoc.", "Ctr.", "Univ.", "Dist.",
              "Elec.", "Tel.", "Pharm.", "Prods.", "Servs.", "Indus.", "Mgmt.", "Grp.", "Hosp."}
T6 = [("Department", "Dep't"), ("International", "Int'l"), ("Association", "Ass'n"),
      ("Company", "Co."), ("Corporation", "Corp."), ("Incorporated", "Inc."),
      ("National", "Nat'l"), ("Insurance", "Ins."), ("Services", "Servs."),
      ("Systems", "Sys."), ("Manufacturing", "Mfg."), ("Brothers", "Bros."),
      ("Government", "Gov't"), ("Federal", "Fed."), ("America", "Am."), ("American", "Am."),
      ("Hospital", "Hosp."), ("University", "Univ."), ("Management", "Mgmt."),
      ("Financial", "Fin."), ("Mutual", "Mut."), ("Products", "Prods."), ("Industries", "Indus.")]
STATCITE = re.compile(r"(?:Fla\. Stat\. §§? ?[\d.]+(?:\([\w]+\))*|\d{1,2} U\.S\.C\.S?\. §§? ?\d+[\w-]*(?:\([\w]+\))*)")


def case_name_before(text, end):
    seg = text[max(0, end - 260):end].rstrip(" ,")
    if " v. " not in seg and "In re " not in seg and "Ex parte " not in seg:
        return None
    cut = 0
    for m in re.finditer(r"([^\s]+)\s+(?=[A-Z“\"])", seg):
        tok = m.group(1)
        if re.search(r"[.;:)”]$", tok) and tok not in ABBR_BREAK and not re.fullmatch(r"[A-Z]\.", tok):
            if " v. " in seg[m.end():] or "In re " in seg[m.end():] or "Ex parte " in seg[m.end():]:
                cut = m.end()
    name = seg[cut:].strip(" ,")
    return name if 3 < len(name) < 160 else None


def bluebook(c):
    name = c["name"]
    for w, ab in T6:
        name = re.sub(rf"\b{w}\b", ab, name)
    court = bb_court(c["court"])
    court = re.sub(r"\bU\.S\. (\d{4})$", r"\1", court) if c.get("rep", "").find(" U.S. ") > 0 else court
    if c.get("rep"):
        pin = c["rep"].replace("Fed. Appx.", "F. App'x")
        if " U.S. " in pin:   # SCOTUS: year only
            court = re.sub(r"^.*?(\d{4})$", r"\1", court)
        hist = bb_history(c.get("history", ""))
        return f"{name}, {pin} ({court}){', ' + hist if hist else ''}."
    hist = bb_history(c.get("history", ""))
    return (f"{name}, {c['lexis']} ({court}){', ' + hist if hist else ''} "
            f"[unreported: Bluebook R. 18.3.1 wants docket no. + exact date].")


def bb_court(court):
    court = re.sub(r"Fla\. (\d+(?:st|nd|rd|th)) DCA", "Fla. Dist. Ct. App.", court)
    return re.sub(r"(\d+(?:st|nd|rd|th) Cir\.) [A-Z][a-z]+\.(?: [A-Z][a-z]+\.)?", r"\1", court)


def bb_history(h):
    if not h:
        return ""
    h = re.sub(r",(?=\S)", ", ", h)
    h = h.replace("Fed. Appx.", "F. App'x")
    # drop a LEXIS parallel only when it follows a reporter cite
    h = re.sub(r"(\d+ [A-Z][\w.' ]{1,20} \d+), \d{4} [A-Z][\w.' ]{0,25}?LEXIS \d+", r"\1", h)
    if " U.S. " in h:  # cert history: U.S. cite only
        h = re.sub(r", \d+ (?:S\. ?Ct\.|L\. ?Ed\. ?2d) \d+", "", h)
    h = re.sub(r"\(U\.S\. (\d{4})\)", r"(\1)", h)
    h = re.sub(r"\(([^()]*)\)", lambda m: "(" + bb_court(m.group(1)) + ")", h)
    return h.strip(" ,")


def extract_cites(text):
    flat = re.sub(r"\s+", " ", text)
    flat = re.sub(r"(\d) (?=[23]d\b)", r"\1 ", flat)
    out, prev_end = [], -1
    for m in TAIL.finditer(flat):
        if not (m.group("rep") or m.group("lexis")):
            continue
        start = m.start("rep") if m.group("rep") else m.start("lexis")
        gap = flat[prev_end:start] if prev_end >= 0 else ""
        cite_txt = flat[start:m.end()]
        # subsequent history ("..., cert. denied, 531 U.S. 818 (2000)") belongs to the prior case
        if out and 0 <= start - prev_end <= 140 and HIST.search(gap):
            out[-1]["history"] = (out[-1].get("history", "") + gap.strip(" ") + cite_txt).strip()
            prev_end = m.end(); continue
        name = case_name_before(flat, start)
        prev_end = m.end()
        if not name or re.search(r"\d+ (?:So\.|F\.|U\.S\.)|LEXIS", name):
            continue
        court = m.group("court").strip()
        if not re.search(r"\d{4}$", court):  # "(Fla.)" -> take year from LEXIS cite
            yr = re.match(r"\d{4}", m.group("lexis") or "")
            court = court.rstrip(".") + "." + (" " + yr.group(0) if yr else "")
        out.append(dict(name=name, rep=(m.group("rep") or "").strip(),
                        lexis=(m.group("lexis") or "").strip(), court=court, pos=start))
    return out, flat


def cmd_cites(a):
    m, pages = load(a.dir)
    d = m["docs"][a.doc - 1]
    s, e = d["pages"]
    if a.topic:
        topics, full = statute_topics(pages, s, e)
        t = topics[a.topic - 1]
        text = full[t["start"]:t["end"]]
        page_at = lambda off: t["page"]
    else:
        full, page_at = norm_with_pages(pages, s, e)
        text = full
    cites, _ = extract_cites(text)
    seen, rows = {}, []
    for c in cites:
        key = (c["rep"] or c["lexis"]).lower()
        if key in seen:
            seen[key]["count"] += 1; continue
        c["count"], c["page"] = 1, page_at(c["pos"])
        c["bluebook"] = bluebook(c)
        seen[key] = c; rows.append(c)
    stats = sorted(set(re.sub(r"\s+", " ", x).rstrip(".") for x in STATCITE.findall(text)))
    Path(a.dir, f"cites_doc{a.doc}{'_t' + str(a.topic) if a.topic else ''}.json").write_text(
        json.dumps(dict(cases=rows, statutes=stats), indent=1))
    print(f"[doc {a.doc}{' topic ' + str(a.topic) if a.topic else ''}: {len(rows)} unique case cites "
          f"({sum(r['count'] for r in rows)} total), {len(stats)} statute cites]")
    for r in rows[:a.max]:
        flag = "  <-- HISTORY" if HIST.search(r.get("history", "")) else ""
        line = (r["bluebook"] + flag) if a.bluebook else f"{r['name']}, {r['rep'] or r['lexis']} ({r['court']})"
        print(f"p.{r['page']}{' x' + str(r['count']) if r['count'] > 1 else ''}  {line}")
    if len(rows) > a.max:
        print(f"... {len(rows) - a.max} more in the cites json")
    if stats:
        if a.bluebook:  # R. 12: official code (U.S.C., not U.S.C.S.) + year of code
            yr = re.search(r"(\d{4}) (?:Regular )?Session", d.get("date", ""))
            fy = f" ({yr.group(1)})" if yr else " (YEAR)"
            bb = []
            for x in stats:
                x = x.replace("U.S.C.S.", "U.S.C.")
                x = re.sub(r"§§ (\S+)$", r"§ \1", x)
                bb.append(x + (fy if x.startswith("Fla.") else " (YEAR)"))
            stats = sorted(set(bb))
        print("statutes: " + "; ".join(stats[:40]))


def cmd_stats(a):
    m, pages = load(a.dir)
    whole = sum(len(p) for p in pages)
    idx = (Path(a.dir) / "index.md").read_text()
    print(f"whole file ~{whole // 4:,} tokens; index.md ~{toks(idx):,} tokens "
          f"({100 * len(idx) / max(whole, 1):.2f}% of full read)")


def main():
    import signal
    if hasattr(signal, "SIGPIPE"):  # quiet exit when piped to head/less
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    ap = argparse.ArgumentParser(prog="lpt")
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("index"); p.add_argument("pdf"); p.add_argument("--out")
    p = sp.add_parser("search"); p.add_argument("dir"); p.add_argument("regex")
    p.add_argument("--ctx", type=int, default=160); p.add_argument("--max", type=int, default=15)
    p.add_argument("--doc", type=int)
    p = sp.add_parser("pages"); p.add_argument("dir"); p.add_argument("range")
    p.add_argument("--max-chars", type=int, default=12000)
    p = sp.add_parser("section"); p.add_argument("dir"); p.add_argument("doc", type=int)
    p.add_argument("name"); p.add_argument("--max-chars", type=int, default=12000)
    p = sp.add_parser("topic"); p.add_argument("dir"); p.add_argument("doc", type=int)
    p.add_argument("n", type=int); p.add_argument("--max-chars", type=int, default=12000)
    p = sp.add_parser("cites"); p.add_argument("dir"); p.add_argument("doc", type=int, nargs="?", default=1)
    p.add_argument("--topic", type=int); p.add_argument("--bluebook", action="store_true")
    p.add_argument("--max", type=int, default=40)
    p = sp.add_parser("stats"); p.add_argument("dir")
    a = ap.parse_args()
    {"index": cmd_index, "search": cmd_search, "pages": cmd_pages,
     "section": cmd_section, "topic": cmd_topic, "cites": cmd_cites, "stats": cmd_stats}[a.cmd](a)


if __name__ == "__main__":
    main()
