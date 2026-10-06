---
name: legal-pdf-triage
description: Index large legal PDFs (Nexis Uni case exports, annotated statutes, casebooks, long opinions) on disk so only the needed pages enter context; extract Bluebook citations and run a CourtListener validity check as a Shepardizing substitute. Use when the user uploads or references a long legal document, asks for its citations, or asks whether a case is still good law.
---

# Legal PDF Triage

Large legal documents cost tens or hundreds of thousands of tokens if read whole. This skill keeps the full text on disk, pulls only the slices a question needs, turns citations into Bluebook form, and checks whether the cases are still good law.

## Rule
Never read a long legal PDF or its extracted text directly into context. If the file is over ~20 pages (or ~60,000 characters for text), run the index first and work from it.

## Setup
The tool is `scripts/lpt.py` in this skill's folder. Copy it to the working directory, or run it in place. It needs Python 3 and poppler (`pdftotext`, `pdfinfo`). If poppler is missing, install it (`apt-get install -y poppler-utils` on Debian/Ubuntu, `brew install poppler` on macOS).

## 1. Index (always first)
`python3 lpt.py index <file.pdf|file.txt>` writes `<file>.lpt/` (per-page text, `meta.json`, `index.md`) and prints the index. Read nothing else yet. The document type is detected automatically:
- **Cases** (Nexis case exports, one or many per file, split on "End of Document"): title, court, decision date, reporter cite, and the page where each section starts (overview, procedural, outcome, coreterms, headnotes, opinion, concur, dissent).
- **Annotated statutes** (Nexis annotated codes): section text, history, amendments, Notes to Decisions, unpublished decisions, AG opinions, research references, plus a numbered **topic map** of every Notes-to-Decisions topic with its page and annotation count.
- **Casebooks / long single documents**: a list of case headings (a caption followed by a reporter citation).
- If the index warns of scanned pages, the file needs OCR first. Say so instead of guessing at the content.

## 2. Pull only what the question needs
- `python3 lpt.py section <dir> <doc#> <name>`. Case section names: overview, procedural, outcome, coreterms, headnotes, opinion, concur, dissent. Statute section names: text, history, amendments, decisions, unpublished, agopinions, research.
- `python3 lpt.py topic <dir> <doc#> <n>` returns the annotations for one statute topic, numbered.
- `python3 lpt.py search <dir> "<regex>" [--doc N] [--ctx 160] [--max 15]` returns page-cited snippets.
- `python3 lpt.py pages <dir> 41-43` returns specific pages once search has found them.
- Output is capped at 12,000 characters (`--max-chars`). Escalate in order: index → search/topic → section/pages. Read a full opinion only when briefing that case, and read it section by section.

## 3. Citations (Bluebook)
`python3 lpt.py cites <dir> [doc#] [--topic n] [--bluebook] [--max N]` extracts case and statute citations, removes duplicates (with counts and first page), and attaches subsequent history. With `--bluebook` it reformats them into Bluebook (21st ed.) citation-sentence form:
- Drops the LEXIS parallel when a reporter cite exists. Unreported decisions keep the LEXIS cite and are flagged, because R. 18.3.1 also requires a docket number and exact date.
- Converts court parentheticals ("Fla. 1st DCA" → "Fla. Dist. Ct. App."; strips the district-state tag Nexis adds after a circuit), uses the year only for U.S. Reports, and converts Fed. Appx. → F. App'x.
- Keeps subsequent history (cert. denied, aff'd, rev'd, vacated…) and marks those cases `<-- HISTORY`. They go straight to step 4.
- Abbreviates common T6 words in case names. Converts U.S.C.S. → U.S.C. (R. 12.2). Takes the Fla. Stat. code year from the export; "(YEAR)" means the year still has to be supplied.
- Jurisdiction note: some courts require local citation forms. Florida courts, for example, use "(Fla. 1st DCA 2004)" under Fla. R. App. P. 9.800. Default to Bluebook unless the user's assignment or court requires local style.
- Limits: the output is mechanical. Before submission, check case-name abbreviations (T6/R. 10.2), pin cites, signals, and short forms against the Bluebook itself. Nexis sometimes shortens party names, so use the verified name from step 4.

## 4. Shepardize (validity check)
True Shepard's (LexisNexis) and KeyCite (Westlaw) are proprietary citators, and nothing here reproduces their editorial flags. Run this check, label it **"CourtListener validity check, not Shepard's"**, and tell the user to run Shepard's or KeyCite on any case they will rely on in submitted or filed work. Requires the CourtListener connector.
1. **Existence.** Pass the Bluebook list to `analyze_citations` (text parameter; up to 250 unique cites per batch; continue with `resume_citation_analysis`). Report NOT FOUND, AMBIGUOUS (resolve by case name), and any case-name-mismatch WARNING, which signals a possible fabricated or garbled cite. Record each cluster_id and the "cited by" count.
2. **Direct history.** Start with the history already in the source (`<-- HISTORY`). Then use `search` with `q: caseName:"<first party>"`, filed after the decision date, to catch appeals and remands that a keyword search misses.
3. **Negative citing references.** Use `search` with `q: cites:<cluster_id> AND (overruled OR abrogated OR "receded from" OR disapproved OR quashed OR superseded OR reversed OR vacated OR "declined to follow" OR criticized OR "no longer good law")`, with `fields` limited to caseName, court, dateFiled, citation, cluster_id. Then run `search_document` (up to 10 opinion_ids; query = the case's first party name) to read how each opinion actually treats the case. A keyword hit is a lead, not a verdict.
4. **Signal.** Assign one based only on text actually read:
   - **Red:** overruled, abrogated, receded from (a court overruling its own precedent), quashed or disapproved by a higher court, or superseded by statute on the point relied on.
   - **Yellow:** reversed or vacated in part or "on other grounds", criticized, declined to follow, or limited.
   - **Green:** verified, no negative treatment found. That is evidence, not a guarantee.
5. **Report** a table: Case (Bluebook) | Verified | Direct history | Negative refs found (with cite) | Signal | Point cited for. Note coverage gaps, since CourtListener misses some unpublished and state trial-level opinions.

## 5. Cite only what was read
Every quotation, holding, and pin cite must come from text actually pulled with these commands, with its page noted. Nexis "Case Summary," "Overview," headnotes, and statute annotations are LexisNexis editorial content, not the court's words. Never quote them as the opinion or cite them as authority. Confirm a holding in the opinion text itself before relying on it.

## Notes
- PDF page numbers are physical pages, not reporter pagination. For pin cites, use the star-page markers in the opinion text (e.g., `*345`).
- Text inputs without page breaks are split into ~3,500-character chunks, and the index says so.
- Case-heading detection and citation parsing are heuristic. Treat the output as a map and spot-check anything that matters.
- `python3 lpt.py stats <dir>` compares the cost of reading the full file with the cost of the index.
- Index folders and cites JSON are working files. Don't hand them to the user unless asked.
- This skill supports research. It does not provide legal advice.
