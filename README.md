# Repo API

A lightweight FastAPI service for reading files from and committing files to GitHub repositories. Used by IDP

## Setup

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env       # add your GH_TOKEN
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

Interactive docs: http://localhost:8080/docs

## Authentication

The service resolves the GitHub Personal Access Token (PAT) **server-side**, per
request, from Google Secret Manager. For a request to `/repos/{owner}/...` it
reads the secret named `{owner}_token` (e.g. `octocat` → secret `octocat_token`)
from project `idp-poc-495014` (override with the `SECRET_PROJECT` env var).

If no secret exists for that owner, GitHub calls are made **unauthenticated**
(subject to lower rate limits and no private-repo access).

> The token is **never** accepted from the client — no `Authorization` header and
> no query parameter. This keeps PATs out of access logs, browser history, and
> proxy logs, and centralizes credential management in Secret Manager.

Scopes needed on each PAT: `repo` for private repos, `public_repo` for public only.

> **Local dev:** the Secret Manager client uses Application Default Credentials —
> run `gcloud auth application-default login` first, or the lookup returns nothing
> and calls fall back to unauthenticated.

## Endpoints

### Read

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/repos/{owner}/{repo}/file` | Fetch a single file as YAML |
| `GET` | `/repos/{owner}/{repo}/files` | Fetch multiple files in one request |
| `GET` | `/repos/{owner}/{repo}/tree` | List directory contents |
| `GET` | `/repos/{owner}/{repo}/info` | Repository metadata |

#### `GET /repos/{owner}/{repo}/file`

Query params:
- `path` — file path within the repo (required)
- `ref` — branch, tag, or commit SHA (default: `HEAD`)
- `raw` — return content only, no metadata wrapper (default: `false`)

```bash
curl "http://localhost:8080/repos/octocat/Hello-World/file?path=README.md"
```

#### `GET /repos/{owner}/{repo}/tree`

Query params:
- `path` — directory path, empty string for root (default: `""`)
- `ref` — branch, tag, or commit SHA (default: `HEAD`)

#### `GET /repos/{owner}/{repo}/files`

Query params:
- `paths` — repeat for each file: `?paths=a.tf&paths=b.tf`
- `ref` — branch, tag, or commit SHA (default: `HEAD`)

### Commit

#### `POST /repos/{owner}/{repo}/commit`

Commits one or more files in a single Git commit.

- **Existing repo:** files are committed straight to `branch` (created from the
  default branch if it doesn't exist).
- **New repo:** the repository is **created automatically** with a `main` branch,
  then the files — plus a `terraform-plan` GitHub Actions workflow — are pushed to a
  `first-commit` branch and a **PR is opened into `main`**. The workflow runs
  `terraform plan` on the PR and posts the result as a comment.

Request body:

```json
{
  "message": "add terraform modules",
  "files": {
    "main.tf": "terraform {\n  ...\n}",
    "variables.tf": "variable \"project_id\" {\n  ...\n}"
  },
  "folder": "src",
  "destination": "infra/modules",
  "branch": "main",
  "private": false
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `message` | string | required | Commit message |
| `files` | object | required | Map of file path → content |
| `folder` | string | `""` | Source prefix stripped from file path keys |
| `destination` | string | `""` | Target folder in the repo |
| `branch` | string | `"main"` | Branch to commit to; forked from default branch if absent |
| `private` | bool | `false` | Repo visibility when auto-creating |

**Path mapping:** a file keyed `"src/main.tf"` with `folder="src"` and `destination="infra"` is committed as `infra/main.tf`.

Response (existing repo):

```json
{
  "repo": "owner/my-repo",
  "branch": "main",
  "commit_sha": "abc123...",
  "files_committed": ["infra/main.tf", "infra/variables.tf"],
  "created_repo": false,
  "pull_request_url": null,
  "workflow_path": null
}
```

Response (newly created repo):

```json
{
  "repo": "owner/IDP-demo-xyz",
  "branch": "first-commit",
  "commit_sha": "abc123...",
  "files_committed": [".github/workflows/terraform-plan.yml", "infra/main.tf"],
  "created_repo": true,
  "pull_request_url": "https://github.com/owner/IDP-demo-xyz/pull/1",
  "workflow_path": ".github/workflows/terraform-plan.yml",
  "modules_secret_set": true
}
```

> The plan-on-PR workflow runs because the branch push and PR are made with the
> owner's **PAT** (a push using the built-in `GITHUB_TOKEN` would not trigger it).
> The PAT therefore needs `repo` scope, and the repo/org must allow Actions to
> have `pull-requests: write` for the plan comment to post.

> **Private Terraform modules:** `terraform init` clones module sources from
> github.com. The built-in `GITHUB_TOKEN` can't read *other* private repos, so on
> repo creation the owner's PAT is injected as a `GH_MODULES_TOKEN` Actions secret
> (`modules_secret_set: true`). The workflow's git-auth step uses that secret to
> authenticate module clones (falling back to `github.token` if it's absent).
> Setting the secret needs a PAT with `repo` scope and admin on the repo.

## Docker

```bash
docker build -t repo-api .
# Mount ADC so the container can read Secret Manager locally
docker run -p 8080:8080 \
  -e SECRET_PROJECT=idp-poc-495014 \
  -e GOOGLE_APPLICATION_CREDENTIALS=/adc.json \
  -v $HOME/.config/gcloud/application_default_credentials.json:/adc.json:ro \
  repo-api
```

## Environment variables

| Variable | Description |
|----------|-------------|
| `SECRET_PROJECT` | GCP project holding the per-owner `{owner}_token` secrets (default: `idp-poc-495014`) |
| `TOKEN_CACHE_TTL` | Seconds a resolved token is cached in memory before re-reading Secret Manager (default: `300`; `0` disables) |

#added github variables for 
CATALOG_OWNER=rjones-projects
CATALOG_REPO=repo-api

# Create a service account
gcloud iam service-accounts create github-actions  --project=vf-gned-ngdi-alpha-ing

# Grant required roles
gcloud projects add-iam-policy-binding vf-gned-ngdi-alpha-ing --member="serviceAccount:github-actions@vf-gned-ngdi-alpha-ing.iam.gserviceaccount.com" --role="roles/artifactregistry.writer"
gcloud projects add-iam-policy-binding vf-gned-ngdi-alpha-ing --member="serviceAccount:github-actions@vf-gned-ngdi-alpha-ing.iam.gserviceaccount.com" --role="roles/run.developer"
#add IAM permissions
gcloud iam service-accounts add-iam-policy-binding  479677124022-compute@developer.gserviceaccount.com --project=vf-gned-ngdi-alpha-ing  --role="roles/iam.serviceAccountUser"  --member="serviceAccount:github-actions@vf-gned-ngdi-alpha-ing.iam.gserviceaccount.com"
gcloud projects add-iam-policy-binding vf-gned-ngdi-alpha-ing --member="serviceAccount:github-actions@vf-gned-ngdi-alpha-ing.iam.gserviceaccount.com" --role="roles/run.admin"

# Create WIF pool + provider (swap in your GitHub org/repo)
gcloud iam workload-identity-pools create github-pool --project=idp-poc-495014 --location=global
gcloud iam workload-identity-pools providers update-oidc github-provider --project=idp-poc-495014 --location=global --workload-identity-pool=github-pool --issuer-uri="https://token.actions.githubusercontent.com"  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" --attribute-condition="assertion.repository=='rjones-projects/repo-api'"

# Allow the pool to impersonate the SA
gcloud iam service-accounts add-iam-policy-binding github-actions@idp-poc-495014.iam.gserviceaccount.com --project=idp-poc-495014 --role="roles/iam.workloadIdentityUser" --member="principalSet://iam.googleapis.com/projects/$(gcloud projects describe idp-poc-495014 --format='value(projectNumber)')/locations/global/workloadIdentityPools/github-pool/attribute.repository/rjones-projects/repo-api"

#check the policy binding
gcloud iam service-accounts get-iam-policy github-actions@idp-poc-495014.iam.gserviceaccount.com 

#create secrets
 Settings → Secrets and variables → Actions → New repository secret

#get the secret - WIF_PROVIDER - added to repo secrets - could be vars - todo: fix pipeline
gcloud iam workload-identity-pools providers describe github-provider --project=idp-poc-495014 --location=global --workload-identity-pool=github-pool --format="value(name)"

#secret - WIF_SERVICE_ACCOUNT
github-actions@idp-poc-495014.iam.gserviceaccount.com

#added github variables for 
CATALOG_OWNER=rjones-projects
CATALOG_REPO=catalog
CATALOG_FILE=catalog.yaml

# ── Per-owner GitHub PATs via Secret Manager ──────────────────────────────────
# At request time the service reads the secret "<owner>_token" from idp-poc-495014,
# where <owner> is the {owner} in /repos/{owner}/... . Create one secret per GitHub
# owner/org you want authenticated access to (example owner: octocat).

# Create the secret and add the PAT as the first version (reads from stdin)
gcloud secrets create octocat_token --project=idp-poc-495014 --replication-policy=automatic
printf '%s' 'ghp_yourTokenHere' | gcloud secrets versions add octocat_token --project=idp-poc-495014 --data-file=-

# Grant the Cloud Run runtime service account read access. Project-level grant lets
# it read every <owner>_token without re-binding for each new owner:
gcloud projects add-iam-policy-binding idp-poc-495014 --role="roles/secretmanager.secretAccessor" --member="serviceAccount:$(gcloud projects describe idp-poc-495014 --format='value(projectNumber)')-compute@developer.gserviceaccount.com"

# (Tighter alternative — grant per secret instead of project-wide:)
# gcloud secrets add-iam-policy-binding octocat_token --project=idp-poc-495014 --role="roles/secretmanager.secretAccessor" --member="serviceAccount:$(gcloud projects describe idp-poc-495014 --format='value(projectNumber)')-compute@developer.gserviceaccount.com"

# If a prior deploy left GH_TOKEN as a literal env var on the service, clear it once
gcloud run services update repo-api --project=idp-poc-495014 --region=europe-west2 --remove-env-vars=GH_TOKEN

# Rotate a token by adding a new version (picked up within TOKEN_CACHE_TTL, default 5m)
printf '%s' 'ghp_newTokenHere' | gcloud secrets versions add octocat_token --project=idp-poc-495014 --data-file=-



docker build -t repo-api .
docker run -p 8085:8080 repo-api 

docker tag repo-api europe-west2-docker.pkg.dev/idp-poc-495014/repo-api/repo-api:latest

docker push europe-west2-docker.pkg.dev/idp-poc-495014/repo-api/repo-api:latest

docker tag repo-api europe-west2-docker.pkg.dev/vf-gned-ngdi-alpha-ing/repo-api/repo-api:latest

docker push europe-west2-docker.pkg.dev/vf-gned-ngdi-alpha-ing/repo-api/repo-api:latest

