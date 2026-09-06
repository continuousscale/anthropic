# csfiler

An AI file renaming and filing agent for **Continuous Scale LLC**, operating on
Google Drive.

Clients and vendors send documents named `IMG_4471.pdf`, `scan (2).pdf` and
`final proposal v2 FINAL.docx`. Someone has to open each one, work out what it
is, rename it to the convention, and put it in the right folder. `csfiler` reads
each file, identifies it, and does both — renaming *and* filing — with a preview
step before anything moves and a one-command undo afterwards.

```
IMG_4471.pdf                     →  2026-08-14_ThirdHorizon_SOW_signed.pdf
                                    05 Clients & Revenue/Clients/ThirdHorizon/1 Agreement

scan (2).pdf                     →  2026-08-31_Mercury_Statement_x4471.pdf
                                    04 Money & Banking/Accounts/Mercury/2 Statements/2026

Document(3).pdf                  →  2026-03-15_IRS_Return.pdf
                                    03 Tax/Tax Years/2025/1 Filed Returns
```

Note the third one: filed in March 2026, but it is the **2025** return, so it
files under 2025.

---

## How this differs from the tool it is modelled on

This was built after [Financial Cents' AI File Renaming
agent](https://financial-cents.com/artificial-intelligence/ai-file-renaming/),
which reads a client-uploaded document, detects its date, type, account and
client, and applies the firm's naming convention either automatically or after
approval. That is the core idea, and it is a good one. This rebuild keeps it and
adds the following.

| | Financial Cents | csfiler |
|---|---|---|
| **Scope** | Renames the file | Renames **and files** it to an exact folder |
| **Where files live** | Inside their practice-management portal | Any Google Drive folder, including shared drives |
| **Approval** | A global setting: "Just Do It" or "Always Ask" | Per file, by calibrated confidence — only genuinely uncertain files reach a human |
| **Undo** | — | Every action journalled with prior name and folder; `csfiler undo <batch>` restores exactly |
| **Collisions** | — | Versions to `_v2` rather than overwriting; never overwrites anything |
| **Duplicates** | — | Content-hashed, so the same document is not filed twice |
| **Privacy** | — | Account numbers reduced to `x4471`; SSNs and EINs can never reach a filename |
| **Re-runs** | — | Already-conforming files are skipped without an API call |
| **Learning** | — | Corrections made in review are replayed into later runs |
| **Domain** | Generic accounting firm | The actual BizBox taxonomy, clients, banks and retention rules |

The last row is the one that matters most. `csfiler` knows that Third Horizon,
PLTW, MessageGears and GoodSkin are clients with a seven-tab folder each; that a
registration belongs in `02 Compliance` while every return after it belongs in
`03 Tax`; that meeting recordings are not records and are never filed; and that
a document still naming **Andover Consulting, LLC** is correct as filed and
belongs on the name-change register rather than being quietly "corrected".

---

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

### Anthropic credentials

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

### Google Drive access

1. In Google Cloud Console, create a project and enable the **Drive API**.
2. Create an **OAuth client ID** of type *Desktop app*, download the JSON, and
   save it as `credentials.json` in the repo root.
3. The first command that touches Drive opens a browser once and writes
   `token.json`, which later runs reuse.

For unattended runs, pass a service account instead and share the BizBox folder
with the service account's email address.

Check everything at once:

```bash
csfiler doctor
```

---

## Use

The four commands map onto the safety model. Nothing writes until `apply`.

```bash
# 1. Look. Reads every file, proposes a name and a destination, writes nothing.
csfiler scan "Inbox"

# 2. Decide. Opens a local review queue for anything it was unsure about.
csfiler review --batch 20260906-141233-a1b2c3

# 3. Act. Renames and moves. Prompts for confirmation first.
csfiler apply 20260906-141233-a1b2c3

# 4. Reverse, if you don't like the result.
csfiler undo 20260906-141233-a1b2c3
```

Useful flags:

```bash
csfiler scan "Inbox" --limit 20          # try it on twenty files first
csfiler scan "Inbox" --source local --root ./sandbox   # rehearse on a local copy
csfiler scan "Inbox" --effort high       # think harder on a folder of bad scans
csfiler scan "Inbox" --out proposals.json
csfiler batches                          # what has been run, and what is undoable
```

See it work without an API key or a Drive connection:

```bash
python scripts/demo.py
```

---

## How a file is decided

```
list  →  skip if already conforming  →  download  →  duplicate check
      →  extract  →  classify  →  name  →  route  →  disposition
```

The classifier returns **facts** — type, date, period, counterparty, account,
whether it is signed, its own confidence, and its reasoning. It never returns a
filename or a folder. Those are built by deterministic code from
`config/continuous_scale.yaml`, so a convention change is a config edit, and the
model cannot produce a name that breaks the convention.

Each file ends in one disposition:

| Disposition | Meaning |
|---|---|
| `auto` | Confidence ≥ 0.90 and nothing flagged. Applied without review. |
| `review` | A proposal exists but wants a human: unknown client, old entity name, a possible personal record, or middling confidence. |
| `unidentified` | Below 0.60. No proposal. The file is left alone. |
| `duplicate` | Byte-identical to something already filed. |
| `excluded` | A type the BizBox deliberately does not file, e.g. a meeting transcript. |
| `skip_conforming` | Already named correctly. No API call spent. |
| `error` | Unreadable or the classification failed. Reported, batch continues. |

Thresholds live under `confidence:` in the config.

---

## Safety

The things that make this safe to point at a real Drive:

- **`scan` cannot write.** It has no code path that renames, moves or creates
  anything. A first run is safe by construction, not by remembering a flag.
- **Nothing is ever overwritten.** A name that is taken becomes `_v2`. Both
  storage adapters refuse to clobber an existing file.
- **Rename and move are one operation**, so a file is never briefly sitting in
  its old folder under its new name.
- **Everything is journalled** to `.csfiler/journal.db` with the prior name and
  prior parent, which is what makes `undo` exact.
- **No account number, SSN or EIN can reach a filename.** Account numbers are
  reduced to their last four digits; the forbidden patterns are re-checked
  after the name is built, and a name that still matches one is rejected.
- **Protected paths are never written to** — `12 Archive` and
  `09 Legal Matters/Legal Holds`, since a legal hold overrides routine filing.
- **A missing fact blocks; it never guesses.** No date means no name, rather
  than today's date and a document misfiled into the wrong year.
- **One bad file cannot abort a batch.** A corrupt upload, an unreadable scan or
  an API failure is recorded against that file and the run continues.

---

## Configuring it

Everything domain-specific is in `config/continuous_scale.yaml`. It is found by
searching `./config/`, then `./`, then the installed package directory, then
`~/.config/csfiler/` — or pass `--config` explicitly. It contains:

- `naming` — the `{date}_{subject}_{descriptor}` template, banned words, length
- `privacy` — redaction style and the patterns forbidden in a filename
- `confidence` — the auto-apply and review thresholds
- `entities` — clients, banks, vendors, agencies, and their aliases
- `document_types` — the catalog the classifier chooses from, each with the
  descriptor it renders to and the folder rule that files it
- `excluded_types` — what is deliberately never filed
- `retention_policy` — reference text attached to proposals

**Adding a client** means adding it under `entities.clients` with its aliases.
Until then, documents naming it go to review with the reason attached rather
than being filed into a folder that does not exist.

**Adding a document type** means adding an entry to `document_types` with a
`path`. If the path needs a token the facts cannot supply, the file goes to
review instead of being half-filed.

---

## Cost

One API call per file, against `claude-opus-5` at `medium` effort. PDFs are
trimmed to their first 12 pages before being sent, since identification never
needs more. The type catalog and entity list are sent as a cached system
prompt, and `scan` reports the cache hit rate it actually achieved. Re-running
over a folder that is already filed costs nothing, because conforming files are
skipped before the API is reached.

Raise `--effort` for a folder of difficult scans; it is per-run.

---

## Development

```bash
pip install -e ".[dev]"
pytest          # 37 tests, no API key or network needed
```

The test suite drives the real pipeline, storage, naming, routing and journal
against a temporary directory with a faked classifier, including the full
apply-then-undo round trip.

## Layout

```
config/continuous_scale.yaml   the entire domain: convention, taxonomy, entities, rules
src/csfiler/
  models.py       DocumentFacts (the classifier's output contract), Proposal
  config.py       typed config + entity resolution
  extract.py      file bytes → content blocks (PDFs go to the model natively)
  classify.py     the Claude call
  naming.py       filename construction, redaction, versioning
  routing.py      facts → BizBox folder path
  storage.py      Google Drive and local adapters
  journal.py      SQLite audit log, undo, duplicates, corrections
  pipeline.py     scan / apply / undo
  review.py       the review queue web UI
  cli.py          the commands
scripts/demo.py   a full offline run
```
