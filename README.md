# Repo API

A lightweight FastAPI service for reading files from and committing files to GitHub repositories. Used by IDP

## Setup

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env       # add your GITHUB_TOKEN
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

Interactive docs: http://localhost:8080/docs

## Authentication

Pass a GitHub Personal Access Token (PAT) via:

- `Authorization: Bearer <token>` header
- `?token=<token>` query parameter
- `GITHUB_TOKEN` environment variable (fallback)

Scopes needed: `repo` for private repos, `public_repo` for public only.

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

Commits one or more files in a single Git commit. **Creates the repository automatically** if it does not exist.

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

Response:

```json
{
  "repo": "owner/my-repo",
  "branch": "main",
  "commit_sha": "abc123...",
  "files_committed": ["infra/main.tf", "infra/variables.tf"]
}
```

## Docker

```bash
docker build -t repo-api .
docker run -p 8080:8080 -e GITHUB_TOKEN=<token> repo-api
```

## Environment variables

| Variable | Description |
|----------|-------------|
| `GITHUB_TOKEN` | GitHub Personal Access Token (used as fallback when no token is passed per-request) |

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



docker build -t repo-api .
docker run -p 8080:8080 repo-api 

docker tag repo-api europe-west2-docker.pkg.dev/idp-poc-495014/repo-api/repo-api:latest

docker push europe-west2-docker.pkg.dev/idp-poc-495014/repo-api/repo-api:latest

docker tag repo-api europe-west2-docker.pkg.dev/vf-gned-ngdi-alpha-ing/repo-api/repo-api:latest

docker push europe-west2-docker.pkg.dev/vf-gned-ngdi-alpha-ing/repo-api/repo-api:latest

