# daily-claude-lab

An autonomous engineering lab that makes the projects it already built better,
one reviewed pull request at a time. Once a day a GitHub Action picks one
portfolio repository, audits it, implements the single most valuable
improvement, verifies it, and opens a pull request for you to review. When you
comment on one of its pull requests, the next run answers: it pushes a fix, or
replies with its reasons.

It does **not** create repositories, never pushes to a default branch, and
commits only as you, so your account is the only contributor GitHub lists.

The portfolio is every repository you own that carries the topic
[`daily-gemini-project`](https://github.com/search?q=user%3Atarak-09+topic%3Adaily-gemini-project&type=repositories).
To retire a project from the lab, remove the topic or archive the repository.
Reasoning and implementation run on **Gemini via Vertex AI**, billed to your
Google Cloud project.

## How a run works

```
plan ──► work ──► publish ──► ledger
```

| job | holds | does |
| --- | --- | --- |
| **plan** | GitHub token. No model. | Reads every portfolio repository and picks one action: **revise** an open lab PR that has new comments from you or failing checks; otherwise **improve** the next repository in rotation; otherwise **skip**. ([`scripts/lab_state.py`](scripts/lab_state.py)) |
| **work** | Vertex AI credentials. **No GitHub token.** | Gemini audits the repository from a clean clone, chooses the change that matters most, implements it and runs the tests. The job then commits the result as you and re-runs the tests itself. |
| **publish** | GitHub token. No model, and it runs no project code. | Re-checks the commit ([`scripts/verify_change.sh`](scripts/verify_change.sh)), pushes a `lab/…` branch and opens the PR, or pushes to the PR branch and replies. |
| **ledger** | This repository only. | Appends one row to [`RUNS.md`](RUNS.md), committed as you. |

The split exists because the model runs with a shell (`--approval-mode=yolo`)
and installs packages from npm and PyPI. In the August 2025
[s1ngularity attack](https://www.wiz.io/blog/s1ngularity-supply-chain-attack),
a malicious npm package drove AI CLIs started with `--yolo`-style flags to hunt
for secrets, and over a thousand GitHub tokens leaked. GitHub-hosted runners
also give every step passwordless `sudo`, so nothing that runs inside a job can
be fenced off from the rest of that job. The only real boundary is a separate
VM. So the job that runs the model never holds the GitHub token, and the jobs
that hold the token never run the model or any project code. The publish job
takes nothing from the work job except a git bundle, and re-checks every
commit in it.

### What the lab works on

The audit prompt ([`prompts/audit.md`](prompts/audit.md)) defines what
"technically good" means and makes the model take the first real problem in
this order: a real bug, then no build or no CI, then committed artefacts, weak
tests, no type-check or lint baseline, documentation that misstates behaviour.
Features come only after all of that is solid. A survey on 2026-11-10 found
that none of the 15 repositories has CI, a lint or type-check configuration,
or a licence, two have committed runtime databases, and four have no
lockfile, so the early pull requests will mostly be foundation work.

Repositories take turns: the one considered least recently goes next, with
`RUNS.md` as the memory. Before, the lab kept improving the same few projects;
`secret-sweeper` had not been touched since the day it was created.

**Steering:** open an issue on a portfolio repository. Issues you file move
that repository a week up the queue, and the audit treats them as top
priority. The pull request closes the issue when it is merged.

### Your review is the gate

- At most one open lab PR per repository, and at most `MAX_OPEN_PRS` (3) in
  total. At the cap the lab only answers your comments, so unreviewed work
  never piles up.
- **Comment** on a lab PR, in the conversation or inline on the diff, and the
  next run addresses it. It pushes a commit, then replies with a comment
  marked `<!-- daily-lab -->`. A question gets an answer, not a code change.
  Only *your* comments count: the repositories are public and anyone can
  comment, but nobody else's words ever reach the model.
- **Failing checks** on a lab PR: the lab tries to fix them, at most
  `MAX_CI_FIX_ATTEMPTS` (2) times, then leaves the PR to you.
- **Close without merging** to reject. The audit sees the title and does not
  propose it again.
- **Merge** with *Squash and merge*, so each change lands as one commit on the
  default branch.
- **Where to find them:** a PR opened with your own token sends you no
  notification, so bookmark
  [your open lab PRs](https://github.com/pulls?q=is%3Aopen+is%3Apr+author%3A%40me+label%3Adaily-lab).
  Each run's summary page and `RUNS.md` also link the PR.
- Recommended for each portfolio repository: Settings → General →
  *Automatically delete head branches*.

### What publish refuses

Nothing is pushed unless all of these hold. They are checked against the
commit itself, not the model's report.

- Exactly one new commit, directly on the base, authored and committed by you,
  with no `Co-authored-by` trailer and no closing keyword (`fixes #3` in a
  commit message closes issue 3 once it reaches the default branch).
- The workflow's own run of the test command on that exact commit passed.
- No build output, dependency directories, caches, runtime databases or logs,
  `.env`, keys, or submodules added. Deleting such files is allowed: that is
  how committed artefacts get cleaned up.
- No credential-shaped string in an added line.
- At most `MAX_CHANGED_LINES` (800) changed lines, lockfiles excluded, and no
  file over 2 MiB.
- No `.github/workflows/` changes unless the token has the `workflow` scope.
- For a revision, the PR branch has not moved since the lab cloned it.

## Only you in the contributor list

How GitHub decides who appears:

- A repository's contributor list is built from the **authors of commits on
  the default branch**, matched to accounts by email. A commit whose email is
  not linked to any account shows up as an anonymous contributor
  ([GitHub docs](https://docs.github.com/en/repositories/viewing-activity-and-data-for-your-repository/viewing-a-projects-contributors)).
- **`Co-authored-by:` trailers credit the co-author.** That is how "Claude"
  shows up on repositories built with Claude Code
  ([community report](https://github.com/orgs/community/discussions/175200)).
- A **squash merge** credits every other commit author in the PR as a
  co-author ([GitHub changelog](https://github.blog/changelog/2019-12-19-improved-attribution-when-squashing-commits/)),
  so a single bot-authored commit inside a PR would bring the bot back.
- After a history rewrite, GitHub takes **about 24 hours** to refresh the
  list. If it is still wrong after that, GitHub Support can refresh it.

What the lab does about it:

- Every commit is authored and committed as you. By default that is your
  ID-based noreply address (`<id>+<login>@users.noreply.github.com`), which
  GitHub always links to your account and which survives a username change.
  To use a different address, set the repository variables `COMMIT_NAME` and
  `COMMIT_EMAIL`.
- Whatever the model does with git, the job collapses its work into one commit
  that it makes itself, with hooks disabled. Co-author lines are stripped from
  commit messages and from PR bodies (a squash merge can turn a PR body into
  the commit message). Then publish verifies the identity again.
- Everything the model writes is posted under your account, so it passes
  through a sanitizer first ([`lab_state.py sanitize`](scripts/lab_state.py)):
  `@mentions` are wrapped in code so no stranger gets a notification from you,
  and closing keywords are defused so a merge cannot close an unrelated issue.
  The only `Closes #N` is the one the workflow adds itself, and only for an
  issue you filed.
- The prompts forbid Dependabot, Renovate, and CI steps that commit, because
  each of those would add a bot to the contributor list.
- Ledger commits in this repository are made as you too.

### One-time cleanup (needed)

1. **Portfolio history.** All 15 portfolio repositories list
   `github-actions[bot]` as their only contributor, because the old lab
   committed as the bot. Run the **reattribute-history** workflow (Actions →
   reattribute-history → Run workflow). Leave `confirm` empty for a dry run,
   read the summary, then run it again with `confirm` = `REWRITE`. It rewrites
   authorship with [git-filter-repo](https://github.com/newren/git-filter-repo)
   and force-pushes. File contents and dates stay the same; commit SHAs change.
   It skips any repository with an open pull request. **Run it before the lab
   opens its first PR.** Until you do, the lab skips repositories whose history
   lists someone else (`REQUIRE_CLEAN_HISTORY`).
2. **This repository** has 56 ledger commits by `github-actions[bot]` and 6 of
   your own commits whose email is not linked to your account, so GitHub shows
   them as anonymous. The simplest fix for the 6 is to add and verify that
   address under GitHub → Settings → Emails, which links them immediately
   without a rewrite. To rewrite the bot commits here as well, run the workflow
   with `repos` = `daily-claude-lab`, then reset your local clone
   (`git fetch origin && git reset --hard origin/main`).
3. **Claude Code** adds `Co-Authored-By: Claude` to every commit it makes,
   unless told not to. Two early commits in this repository carry it, and the
   rewrite in step 2 removes them. To stop new ones, add this to
   `~/.claude/settings.json`:

   ```json
   { "attribution": { "commit": "", "pr": "", "sessionUrl": false } }
   ```

## Tuning

Everything worth changing is in the `env` block at the top of
[`.github/workflows/daily-profile-project.yml`](.github/workflows/daily-profile-project.yml).
Taste lives in [`prompts/`](prompts/).

| Knob | Default | What it does |
| --- | --- | --- |
| `cron` | `0 9 * * *` | When it runs. UTC only. |
| `MAX_OPEN_PRS` | `3` | Lab PRs open at once across the portfolio. |
| `MAX_CI_FIX_ATTEMPTS` | `2` | Attempts at fixing failing checks on one PR. |
| `ISSUE_BOOST_DAYS` | `7` | How far up the rotation an open owner issue moves a repo. |
| `MAX_CHANGED_LINES` | `800` | Hard cap per change, lockfiles excluded. |
| `REQUIRE_CLEAN_HISTORY` | `true` | Skip repos whose history lists anyone but you. |
| `TOPIC` | `daily-gemini-project` | Defines the portfolio. Changing it orphans every existing project. |
| `GEMINI_MODEL` | `pro` | `flash` is cheaper and faster. |
| `COMMIT_NAME` / `COMMIT_EMAIL` | your profile name / noreply | Repository *variables* (Settings → Secrets and variables → Actions → Variables). |

## Running it manually

```bash
# Normal run, now
gh workflow run daily-profile-project.yml

# Work on one repository (bypasses the open-PR cap, never the one-PR-per-repo rule)
gh workflow run daily-profile-project.yml -f repo=nano-vec

# Plan, build and verify, but publish nothing
gh workflow run daily-profile-project.yml -f dry_run=true

gh run watch
```

Each job writes a summary to the run page: the plan job shows the whole
portfolio (open lab PRs, last-considered dates, contributor status) and why it
chose today's action; the work job shows the audit decision, the build report,
and the tail of the workflow's own test run.

## Secrets

All are repository secrets on this repository (Settings → Secrets and
variables → Actions).

| Name | What it is |
| --- | --- |
| `GH_PROFILE_TOKEN` | A GitHub token for your account. See below. |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | Full resource name of the Workload Identity Federation provider, e.g. `projects/123456789/locations/global/workloadIdentityPools/github/providers/daily-claude-lab`. |
| `GCP_SERVICE_ACCOUNT` | Email of the service account the pool impersonates. Needs only `roles/aiplatform.user`. |

**`GH_PROFILE_TOKEN`** must be able to push branches, open pull requests,
comment, and create labels on the portfolio repositories. It also needs the
**workflow** permission: GitHub refuses any push that touches
`.github/workflows/` from a token without it, and adding CI is the first thing
most of these repositories need. Two options:

- **Fine-grained token (recommended).** Repository access: only the portfolio
  repositories, plus this one if you rewrite its history. Permissions:
  Contents, Pull requests, Issues and Workflows set to read and write;
  Metadata read. A leaked token then reaches only these repositories.
- **Classic token.** Scopes `repo` and `workflow`. This reaches every
  repository you own, including private ones.

The plan job reads the token's scopes and tells the model whether it may touch
workflow files. With a fine-grained token it cannot tell, and assumes it may.

Inference runs on Gemini through Vertex AI and is billed to the GCP project,
so there is no AI-provider API key anywhere. Neither GCP secret is a
credential: they only name *which* identity to federate into. The credential
is minted per run from the work job's OIDC token, which is why that job
requests `id-token: write`.

### Setting up Vertex access

One-time, in the GCP project you want billed:

```bash
PROJECT=your-project-id
REPO=tarak-09/daily-claude-lab

gcloud config set project "$PROJECT"
gcloud services enable aiplatform.googleapis.com iamcredentials.googleapis.com sts.googleapis.com

# The identity the workflow becomes. These resource names are arbitrary labels
# and have no effect on behaviour; they just have to match what the secrets say.
gcloud iam service-accounts create claude-lab --display-name="daily-claude-lab"
SA="claude-lab@$PROJECT.iam.gserviceaccount.com"
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:$SA" --role="roles/aiplatform.user"

# Trust GitHub's OIDC issuer, scoped to this repo only.
gcloud iam workload-identity-pools create github --location=global
gcloud iam workload-identity-pools providers create-oidc daily-claude-lab \
  --location=global --workload-identity-pool=github \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository=='$REPO'"

PROVIDER="$(gcloud iam workload-identity-pools providers describe daily-claude-lab \
  --location=global --workload-identity-pool=github --format='value(name)')"
NUM="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"

# Let the pool impersonate the service account, for this repo only.
gcloud iam service-accounts add-iam-policy-binding "$SA" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/$NUM/locations/global/workloadIdentityPools/github/attribute.repository/$REPO"

gh secret set GCP_WORKLOAD_IDENTITY_PROVIDER --body "$PROVIDER"
gh secret set GCP_SERVICE_ACCOUNT --body "$SA"
```

Gemini models are first-party on Vertex, so there is no Model Garden request and
no approval wait: enabling `aiplatform.googleapis.com` on a project with
billing is enough. Confirm it before spending a workflow run on it:

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" \
  "https://aiplatform.googleapis.com/v1/projects/$PROJECT/locations/global/publishers/google/models/gemini-2.5-pro:generateContent" \
  -d '{"contents":[{"role":"user","parts":[{"text":"say ok"}]}],"generationConfig":{"maxOutputTokens":10}}'
```

`200` means the project can invoke the model.

## The ledger

[`RUNS.md`](RUNS.md) gets one row per run. It also keeps the schedule alive:
GitHub disables scheduled workflows in a public repository after 60 days
without activity, and this repository's only other writes go to other
repositories.

## Developing the lab

```bash
python -m unittest discover -s tests -v   # planner and publish-gate tests; needs git and bash
shellcheck scripts/*.sh
actionlint
```

[`lab-ci`](.github/workflows/lab-ci.yml) runs the same checks on every push
that touches the lab's code.
