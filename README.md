# legal-pdf-triage

A Claude skill for working with large legal documents without burning your context window. It handles Nexis Uni case exports, annotated statutes, casebooks, and long opinions.

**What it does**

- **Indexes on disk.** A 47-page annotated statute that would cost ~49,500 tokens to read whole becomes a one-screen map: statute text, history, and every Notes-to-Decisions topic with its page and annotation count. A 270-page casebook (~235,000 tokens) indexes in under 700.
- **Pulls only what you ask for.** Commands return one opinion section (outcome, dissent…), one statute topic, a page range, or page-cited search hits.
- **Builds Bluebook citations.** It extracts every case and statute cite, removes duplicates, attaches subsequent history, and reformats to Bluebook. It drops LEXIS parallels, converts court parentheticals, outputs F. App'x, and flags unreported decisions that need a docket number.
- **Runs a validity check (Shepardizing substitute).** Using the free CourtListener connector, it verifies that each case exists and catches fabricated or garbled cites, traces direct history, and reads how later opinions treat the case. Each case gets a red, yellow, or green signal.

**What it doesn't do:** replace Shepard's or KeyCite. The validity check is labeled as such. Run a real citator on anything you rely on in submitted or filed work. The skill supports research and is not legal advice.

## Install

- **Claude (web/desktop):** download `legal-pdf-triage.zip` and upload it as a custom skill from the Skills section of Settings.
- **Claude Code:** copy the `legal-pdf-triage/` folder into `~/.claude/skills/`.
- **Claude API:** upload it as a custom skill (see Anthropic's Agent Skills docs).

Requirements: Python 3 and poppler (`pdftotext`). The validity check needs the CourtListener connector enabled.

## Commands

```
python3 scripts/lpt.py index  <file.pdf|file.txt>
python3 scripts/lpt.py search <dir> "<regex>" [--doc N]
python3 scripts/lpt.py pages  <dir> 12-15
python3 scripts/lpt.py section <dir> <doc#> outcome|opinion|dissent|text|history|...
python3 scripts/lpt.py topic  <dir> <doc#> <n>
python3 scripts/lpt.py cites  <dir> [doc#] [--topic n] --bluebook
python3 scripts/lpt.py stats  <dir>
```

## Known limits

Parsing is tuned to Nexis Uni's export layout. Westlaw or court-website PDFs index fine but get fewer section labels. Case-name and citation parsing are heuristic. Bluebook output is a strong first draft, so check T6 abbreviations and pin cites before submitting.

## License

MIT
