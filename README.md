# daily-claude-lab

An autonomous engineering lab. Once a day, a GitHub Action decides whether to
build a new software project, improve one it built earlier, or do nothing — and
then does it, tests it, documents it, and pushes it.

The goal is a portfolio of **10–15 genuinely useful, technically interesting
projects that get better over time**. It is explicitly *not* a repo generator.
A day where the lab decides nothing is worth building is a successful day.

Every project it creates is tagged with the GitHub topic
[`daily-gemini-project`](https://github.com/search?q=user%3Atarak-09+topic%3Adaily-gemini-project&type=repositories).
That topic is the only state this system keeps — there is no database.

Reasoning and implementation run on **Gemini via Vertex AI**, billed to a Google
Cloud project. Google is the only vendor involved — there is no third-party AI
API key anywhere in this repo.

## How a run works

```
digest portfolio  →  DECIDE (gemini #1)  →  BUILD (gemini #2)  →  gates → push → ledger
   gh + jq            writes decision.json    writes report.json    bash, deterministic
```

1. **Digest** — `gh` and `jq` assemble a factual picture of the portfolio: what
   each project is, its language, its topics, and its last 8 commit subjects.
   No model involved.
2. **Decide** — Gemini reads that digest, investigates candidates further with
   `gh`, and writes a single JSON decision: `create`, `improve`, or `skip`. A
   dice roll suggests an action, but the model can override it on quality
   grounds. This phase is read-only.
3. **Build** — Gemini implements that one decision in a scratch checkout, runs
   the tests, and reports back what it actually verified.
4. **Gates** — bash checks the report and the diff, then commits and pushes.
   The model gets no vote in this phase.

### What the gates enforce

- **Tests must pass.** A self-reported `tests_passed: false` aborts the run
  before any commit. Broken code never reaches a repo.
- **Nothing meaningful changed → no commit.** Not a failure; it becomes a skip.
- **No build artifacts or credential-shaped files.** `node_modules/`,
  `__pycache__/`, `dist/`, `.env`, `*.pem` and friends are rejected outright.
- **No secrets in the diff.** The staged content is scanned for GitHub, Google
  (API key and service-account JSON) and AWS key shapes, and private-key headers.
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
| `TOPIC` | `daily-gemini-project` | Defines the portfolio. Changing it orphans every existing project. |
| `timeout-minutes` | `60` | Raise if builds get truncated. |
| `GEMINI_MODEL` | `pro` | Model for all three `gemini -p` calls. `flash` is cheaper and faster. |
| `GOOGLE_CLOUD_LOCATION` | `global` | Vertex location. `global` routes to nearest capacity; `us-central1` pins one region. |

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

All three are repository secrets on this repo
(Settings → Secrets and variables → Actions):

| Name | What it is |
| --- | --- |
| `GH_PROFILE_TOKEN` | A GitHub PAT for the profile. Classic token with the `repo` scope. The built-in `GITHUB_TOKEN` cannot be used — it is scoped to this repo alone and can neither create repos nor push to others. |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | Full resource name of the Workload Identity Federation provider, e.g. `projects/123456789/locations/global/workloadIdentityPools/github/providers/daily-claude-lab`. |
| `GCP_SERVICE_ACCOUNT` | Email of the service account the pool impersonates. Needs only `roles/aiplatform.user`. |

Inference runs on Gemini through Vertex AI and is billed to the GCP project, so
there is no AI-provider API key anywhere. Neither GCP secret is a credential —
they only name *which* identity to federate into. The actual credential is minted
per run from the workflow's OIDC token, which is why the job requests
`id-token: write`.

### Setting up Vertex access

One-time, in the GCP project you want billed:

```bash
PROJECT=your-project-id
REPO=tarak-09/daily-claude-lab

gcloud config set project "$PROJECT"
gcloud services enable aiplatform.googleapis.com iamcredentials.googleapis.com sts.googleapis.com

# The identity the workflow becomes. These resource names are arbitrary labels
# and have no effect on behaviour — they just have to match what the secrets say.
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
no approval wait — enabling `aiplatform.googleapis.com` on a project with billing
is enough. Confirm it before spending a workflow run on it:

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" \
  "https://aiplatform.googleapis.com/v1/projects/$PROJECT/locations/global/publishers/google/models/gemini-2.5-pro:generateContent" \
  -d '{"contents":[{"role":"user","parts":[{"text":"say ok"}]}],"generationConfig":{"maxOutputTokens":10}}'
```

`200` means the project can invoke the model.

The Gemini CLI chooses its auth backend from `~/.gemini/settings.json`, which the
*Point Gemini at Vertex AI* step writes on each run. Without it the CLI would try
to prompt for a backend and hang, since a runner has no TTY.

## The ledger

[`RUNS.md`](RUNS.md) gets one row per run. It serves a second purpose: GitHub
disables scheduled workflows in any repository with 60 days of no commits, and
this repo only ever pushes to *other* repos. The ledger commit keeps the
schedule alive.
