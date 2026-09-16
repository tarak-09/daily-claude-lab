# daily-claude-lab

An autonomous engineering lab. Once a day, a GitHub Action decides whether to
build a new software project, improve one it built earlier, or do nothing — and
then does it, tests it, documents it, and pushes it.

The goal is a portfolio of **10–15 genuinely useful, technically interesting
projects that get better over time**. It is explicitly *not* a repo generator.
A day where the lab decides nothing is worth building is a successful day.

Every project it creates is tagged with the GitHub topic
[`daily-claude-project`](https://github.com/search?q=user%3Atarak-09+topic%3Adaily-claude-project&type=repositories).
That topic is the only state this system keeps — there is no database.

## How a run works

```
digest portfolio  →  DECIDE (claude #1)  →  BUILD (claude #2)  →  gates → push → ledger
   gh + jq            writes decision.json    writes report.json    bash, deterministic
```

1. **Digest** — `gh` and `jq` assemble a factual picture of the portfolio: what
   each project is, its language, its topics, and its last 8 commit subjects.
   No model involved.
2. **Decide** — Claude reads that digest, investigates candidates further with
   `gh`, and writes a single JSON decision: `create`, `improve`, or `skip`. A
   dice roll suggests an action, but the model can override it on quality
   grounds. This phase is read-only.
3. **Build** — Claude implements that one decision in a scratch checkout, runs
   the tests, and reports back what it actually verified.
4. **Gates** — bash checks the report and the diff, then commits and pushes.
   The model gets no vote in this phase.

### What the gates enforce

- **Tests must pass.** A self-reported `tests_passed: false` aborts the run
  before any commit. Broken code never reaches a repo.
- **Nothing meaningful changed → no commit.** Not a failure; it becomes a skip.
- **No build artifacts or credential-shaped files.** `node_modules/`,
  `__pycache__/`, `dist/`, `.env`, `*.pem` and friends are rejected outright.
- **No secrets in the diff.** The staged content is scanned for GitHub,
  Anthropic and AWS key shapes and private-key headers.
- **Only portfolio repos get written.** An `improve` target must appear in the
  topic listing, and this lab repo is explicitly excluded.
- **New repos are created last**, after the files exist and the gates pass — so
  a failed run never leaves an empty repo behind.

## Tuning it

Everything worth changing lives at the top of
[`.github/workflows/daily-profile-project.yml`](.github/workflows/daily-profile-project.yml).

| Knob | Default | What it does |
| --- | --- | --- |
| `cron` | `0 9 * * *` | When it runs. UTC only. |
| `CREATE_PERCENT` | `35` | Chance a run is *suggested* to create, under the cap. |
| `PORTFOLIO_MIN` | `10` | Below this, the create chance gets +25 points. |
| `PORTFOLIO_MAX` | `15` | At this count, `create` is disallowed outright. |
| `TOPIC` | `daily-claude-project` | Defines the portfolio. Changing it orphans every existing project. |
| `timeout-minutes` | `60` | Raise if builds get truncated. |
| `--model opus` | — | On both `claude -p` calls. Switch to `sonnet` for cheaper runs. |

The two prompts — one in *Decide what to do today*, one each in *Build the new
project* / *Improve the existing project* — are where taste lives. Edit those to
change what kinds of projects the lab favours.

## Running it manually

```bash
# Dry run: all four phases, nothing pushed, no repo created.
gh workflow run daily-profile-project.yml -f dry_run=true

# Force a path
gh workflow run daily-profile-project.yml -f mode=create
gh workflow run daily-profile-project.yml -f mode=improve

gh run watch
```

Each run writes a summary table to the run page: action, repository, purpose,
feature, test command and result, files changed, commit, and the decide phase's
rationale. It is assembled from git facts rather than model prose, so it cannot
claim a success that did not happen.

## Secrets

Both are repository secrets on this repo
(Settings → Secrets and variables → Actions):

| Name | What it is |
| --- | --- |
| `GH_PROFILE_TOKEN` | A GitHub PAT for the profile. Classic token with the `repo` scope. The built-in `GITHUB_TOKEN` cannot be used — it is scoped to this repo alone and can neither create repos nor push to others. |
| `ANTHROPIC_API_KEY` | API key from console.anthropic.com. The Claude Code CLI reads it from the environment. |

## The ledger

[`RUNS.md`](RUNS.md) gets one row per run. It serves a second purpose: GitHub
disables scheduled workflows in any repository with 60 days of no commits, and
this repo only ever pushes to *other* repos. The ledger commit keeps the
schedule alive.
