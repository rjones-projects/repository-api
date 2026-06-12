"""
GitHub File API — fetch files and commit changes to GitHub repos.
"""

import base64
import json
import os
import time
import yaml
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, Query, Depends
from fastapi.responses import Response
from ghapi.all import GhApi
from fastcore.net import HTTP4xxClientError
from google.cloud import secretmanager
from google.api_core.exceptions import GoogleAPIError
from pydantic import BaseModel, Field

app = FastAPI(
    title="Repo API",
    description="Fetch files from GitHub repositories and commit changes",
    version="2.1.0",
)

# GCP project that stores the per-owner GitHub PAT secrets (named "<owner>_token").
SECRET_PROJECT = os.getenv("SECRET_PROJECT", "idp-poc-495014")

# How long (seconds) a resolved token is cached in memory before it is re-read
# from Secret Manager. Bounds how long a rotation (or a newly added secret) takes
# to take effect. Set TOKEN_CACHE_TTL=0 to disable caching.
TOKEN_CACHE_TTL = int(os.getenv("TOKEN_CACHE_TTL", "300"))

_secret_client: Optional[secretmanager.SecretManagerServiceClient] = None

# owner -> (token_or_None, expires_at_monotonic). None values are cached too, so a
# missing secret doesn't trigger a Secret Manager call on every request.
_token_cache: dict[str, tuple[Optional[str], float]] = {}


def _secret_manager() -> secretmanager.SecretManagerServiceClient:
    global _secret_client
    if _secret_client is None:
        _secret_client = secretmanager.SecretManagerServiceClient()
    return _secret_client


# ── Models ───────────────────────────────────────────────────────────────────

class CommitRequest(BaseModel):
    message: str = Field(..., description="Commit message")
    files: dict[str, str] = Field(..., description="Mapping of file path to content")
    folder: str = Field("", description="Source prefix stripped from file paths before committing")
    destination: str = Field("", description="Target folder path in the repo")
    branch: str = Field("main", description="Branch to commit to (created from default branch if absent)")
    private: bool = Field(False, description="Make the repo private when auto-creating it")


class CommitResponse(BaseModel):
    repo: str
    branch: str
    commit_sha: str
    files_committed: list[str]
    created_repo: bool = False
    pull_request_url: Optional[str] = None
    workflow_path: Optional[str] = None


# ── Helpers ──────────────────────────────────────────────────────────────────

def _fetch_owner_token(owner: str) -> Optional[str]:
    """Read the GitHub PAT for an owner from Secret Manager (secret '<owner>_token').

    Returns None when the owner has no secret, so callers fall back to
    unauthenticated GitHub access.
    """
    name = f"projects/{SECRET_PROJECT}/secrets/{owner}_token/versions/latest"
    try:
        response = _secret_manager().access_secret_version(name=name)
    except GoogleAPIError:
        return None
    return response.payload.data.decode("utf-8").strip()


def _resolve_owner_token(owner: str) -> Optional[str]:
    """Return the owner's token, served from an in-memory TTL cache when fresh."""
    if TOKEN_CACHE_TTL <= 0:
        return _fetch_owner_token(owner)

    now = time.monotonic()
    cached = _token_cache.get(owner)
    if cached is not None and cached[1] > now:
        return cached[0]

    token = _fetch_owner_token(owner)
    _token_cache[owner] = (token, now + TOKEN_CACHE_TTL)
    return token


def get_github_client(owner: str) -> GhApi:
    """Resolve the GitHub token from the Secret Manager secret named '<owner>_token'.

    `owner` is bound to the {owner} path parameter of each route. When no secret
    exists for the owner, GitHub calls are made unauthenticated.
    """
    return GhApi(token=_resolve_owner_token(owner))


def decode_content(content_bytes: bytes, path: str) -> object:
    text = content_bytes.decode("utf-8", errors="replace")
    if path.endswith(".json"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    if path.endswith((".yaml", ".yml")):
        try:
            docs = [d for d in yaml.safe_load_all(text) if d is not None]
            return docs[0] if len(docs) == 1 else docs
        except yaml.YAMLError:
            pass
    return text


def to_yaml_response(data: dict) -> Response:
    yaml_str = yaml.dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return Response(content=yaml_str, media_type="text/yaml; charset=utf-8")


def _http_status(exc: HTTP4xxClientError) -> int:
    # fastcore's HTTP errors subclass urllib.error.HTTPError, which exposes the
    # status as `.code`. Fall back to other common attributes just in case.
    for attr in ("code", "status", "status_code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    if response is not None:
        return getattr(response, "status_code", 0) or 0
    return 0


def _error_message(exc: HTTP4xxClientError) -> str:
    """Extract GitHub's JSON error message from a fastcore HTTP exception.

    fastcore appends the raw response body to the exception text after an
    '====Error Body====' marker, so we parse that to surface GitHub's own
    "message" (plus any field-level "errors") instead of a generic string.
    """
    text = str(exc)
    body = text.split("====Error Body====", 1)[-1].strip()
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        # No parseable body — return the status line without the body marker noise.
        return text.split("====Error Body====", 1)[0].strip() or text

    if not isinstance(data, dict):
        return text
    message = data.get("message") or text
    errors = data.get("errors")
    if isinstance(errors, list) and errors:
        details = "; ".join(
            e.get("message") or " ".join(filter(None, (e.get("field"), e.get("code"))))
            for e in errors if isinstance(e, dict)
        ).strip("; ")
        if details:
            message = f"{message}: {details}"
    return message


def _github_error(exc: HTTP4xxClientError, default_status: int = 404) -> HTTPException:
    status = _http_status(exc) or default_status
    return HTTPException(status_code=status, detail=_error_message(exc))


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def root():
    return {"message": "Repo API — visit /docs for usage"}


@app.get("/health")
def health():
    return {"status": "ok"}


# ── Read endpoints ───────────────────────────────────────────────────────────

@app.get(
    "/repos/{owner}/{repo}/file",
    summary="Fetch a single file as YAML",
    response_class=Response,
    responses={200: {"content": {"text/yaml": {}}}, 404: {"description": "File or repo not found"}},
)
def get_file(
    owner: str,
    repo: str,
    path: str = Query(..., description="Path to the file within the repo"),
    ref: str = Query("HEAD", description="Branch, tag, or commit SHA"),
    raw: bool = Query(False, description="Return raw content without metadata wrapper"),
    gh: GhApi = Depends(get_github_client),
):
    try:
        fc = gh.repos.get_content(owner=owner, repo=repo, path=path, ref=ref)
    except HTTP4xxClientError as exc:
        raise _github_error(exc)

    if isinstance(fc, list):
        raise HTTPException(status_code=400, detail="Path points to a directory — use /tree endpoint instead")

    raw_bytes = base64.b64decode(fc.content)
    parsed = decode_content(raw_bytes, path)

    if raw:
        return to_yaml_response(parsed if isinstance(parsed, dict) else {"content": parsed})

    return to_yaml_response({
        "repo": f"{owner}/{repo}",
        "path": fc.path,
        "branch": ref,
        "sha": fc.sha,
        "size": fc.size,
        "html_url": fc.html_url,
        "content": parsed,
    })


@app.get(
    "/repos/{owner}/{repo}/tree",
    summary="List directory contents as YAML",
    response_class=Response,
    responses={200: {"content": {"text/yaml": {}}}},
)
def get_tree(
    owner: str,
    repo: str,
    path: str = Query("", description="Directory path (empty = repo root)"),
    ref: str = Query("HEAD", description="Branch, tag, or commit SHA"),
    gh: GhApi = Depends(get_github_client),
):
    try:
        contents = gh.repos.get_content(owner=owner, repo=repo, path=path, ref=ref)
    except HTTP4xxClientError as exc:
        raise _github_error(exc)

    if not isinstance(contents, list):
        contents = [contents]

    items = sorted(
        [{"name": c.name, "path": c.path, "type": c.type, "size": c.size, "sha": c.sha, "html_url": c.html_url} for c in contents],
        key=lambda x: (x["type"] != "dir", x["name"]),
    )

    return to_yaml_response({"repo": f"{owner}/{repo}", "path": path or "/", "ref": ref, "count": len(items), "entries": items})


@app.get(
    "/repos/{owner}/{repo}/files",
    summary="Fetch multiple files at once as YAML",
    response_class=Response,
    responses={200: {"content": {"text/yaml": {}}}},
)
def get_multiple_files(
    owner: str,
    repo: str,
    paths: list[str] = Query(..., description="One or more file paths (repeat ?paths= for each)"),
    ref: str = Query("HEAD", description="Branch, tag, or commit SHA"),
    gh: GhApi = Depends(get_github_client),
):
    results = {}
    for file_path in paths:
        try:
            fc = gh.repos.get_content(owner=owner, repo=repo, path=file_path, ref=ref)
            if isinstance(fc, list):
                results[file_path] = {"error": "path is a directory"}
                continue
            raw_bytes = base64.b64decode(fc.content)
            results[file_path] = {"sha": fc.sha, "size": fc.size, "content": decode_content(raw_bytes, file_path)}
        except HTTP4xxClientError as exc:
            results[file_path] = {"error": str(exc)}

    return to_yaml_response({"repo": f"{owner}/{repo}", "ref": ref, "files": results})


@app.get(
    "/repos/{owner}/{repo}/info",
    summary="Repository metadata as YAML",
    response_class=Response,
    responses={200: {"content": {"text/yaml": {}}}},
)
def get_repo_info(owner: str, repo: str, gh: GhApi = Depends(get_github_client)):
    try:
        r = gh.repos.get(owner=owner, repo=repo)
    except HTTP4xxClientError as exc:
        raise _github_error(exc)

    try:
        topics = gh.repos.get_all_topics(owner=owner, repo=repo).names
    except Exception:
        topics = []

    return to_yaml_response({
        "name": r.name,
        "full_name": r.full_name,
        "description": r.description,
        "default_branch": r.default_branch,
        "private": r.private,
        "language": r.language,
        "stars": r.stargazers_count,
        "forks": r.forks_count,
        "open_issues": r.open_issues_count,
        "topics": topics,
        "created_at": str(r.created_at),
        "updated_at": str(r.updated_at),
        "clone_url": r.clone_url,
        "html_url": r.html_url,
    })


# ── Commit endpoint ──────────────────────────────────────────────────────────

# Branch a freshly created repo is bootstrapped on (PR'd into the default branch).
FIRST_COMMIT_BRANCH = "first-commit"

# GitHub Actions workflow committed into new repos. Runs `terraform plan` on each
# PR and posts the result as a comment. __TF_DIR__ is replaced with the directory
# the Terraform lives in. The plan output is passed to github-script via env (not
# string-interpolated into the script) to avoid breakage/injection on special chars.
_TERRAFORM_PLAN_WORKFLOW = """name: Terraform Plan

on:
  pull_request:

permissions:
  contents: read
  pull-requests: write

jobs:
  plan:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: __TF_DIR__
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Setup Terraform
        uses: hashicorp/setup-terraform@v3

      - name: Terraform Init
        id: init
        run: terraform init -backend=false -input=false
        continue-on-error: true

      - name: Terraform Validate
        id: validate
        run: terraform validate -no-color
        continue-on-error: true

      - name: Terraform Plan
        id: plan
        run: terraform plan -no-color -input=false
        continue-on-error: true

      - name: Comment plan on PR
        if: always()
        uses: actions/github-script@v7
        env:
          PLAN_STDOUT: ${{ steps.plan.outputs.stdout }}
          PLAN_STDERR: ${{ steps.plan.outputs.stderr }}
          PLAN_OUTCOME: ${{ steps.plan.outcome }}
          INIT_OUTCOME: ${{ steps.init.outcome }}
          VALIDATE_OUTCOME: ${{ steps.validate.outcome }}
        with:
          script: |
            const plan = (process.env.PLAN_STDOUT || process.env.PLAN_STDERR || 'No plan output.').slice(0, 60000);
            const body = [
              '#### Terraform Plan `' + process.env.PLAN_OUTCOME + '`',
              '- init: `' + process.env.INIT_OUTCOME + '`  validate: `' + process.env.VALIDATE_OUTCOME + '`',
              '',
              '<details><summary>Show Plan</summary>',
              '',
              '```terraform',
              plan,
              '```',
              '',
              '</details>',
            ].join('\\n');
            await github.rest.issues.createComment({
              owner: context.repo.owner,
              repo: context.repo.repo,
              issue_number: context.issue.number,
              body,
            });
"""


def _ensure_repo(gh: GhApi, owner: str, repo: str, private: bool) -> tuple[str, bool]:
    """Return (default_branch, created) for the repo, creating it if it doesn't exist."""
    try:
        return gh.repos.get(owner=owner, repo=repo).default_branch, False
    except HTTP4xxClientError as exc:
        if _http_status(exc) != 404:
            raise _github_error(exc)

    try:
        me = gh.users.get_authenticated()
        if me.login == owner:
            r = gh.repos.create_for_authenticated_user(name=repo, private=private, auto_init=True)
        else:
            r = gh.repos.create_in_org(org=owner, name=repo, private=private, auto_init=True)
    except HTTP4xxClientError as exc:
        raise _github_error(exc, default_status=422)

    return r.default_branch, True


def _map_paths(files: dict[str, str], folder: str, destination: str) -> dict[str, str]:
    """Apply the folder-strip / destination-prefix mapping to commit file paths."""
    folder_prefix = folder.rstrip("/") + "/" if folder else ""
    mapped: dict[str, str] = {}
    for file_path, content in files.items():
        rel = file_path[len(folder_prefix):] if folder_prefix and file_path.startswith(folder_prefix) else file_path
        dest = f"{destination.rstrip('/')}/{rel}" if destination else rel
        mapped[dest.lstrip("/")] = content
    return mapped


def _commit_tree(gh: GhApi, owner: str, repo: str, base_sha: str, files: dict[str, str], message: str) -> str:
    """Create a single commit on top of base_sha containing files; return its SHA."""
    base_tree_sha = gh.git.get_commit(owner=owner, repo=repo, commit_sha=base_sha).tree.sha
    tree_entries = []
    for path, content in files.items():
        blob = gh.git.create_blob(owner=owner, repo=repo, content=content, encoding="utf-8")
        tree_entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob.sha})
    new_tree = gh.git.create_tree(owner=owner, repo=repo, tree=tree_entries, base_tree=base_tree_sha)
    new_commit = gh.git.create_commit(owner=owner, repo=repo, message=message, tree=new_tree.sha, parents=[base_sha])
    return new_commit.sha


def _branch_sha(gh: GhApi, owner: str, repo: str, branch: str, attempts: int = 8, delay: float = 0.5) -> str:
    """Return a branch's head SHA, retrying while a freshly created repo settles."""
    last_exc = None
    for _ in range(attempts):
        try:
            return gh.git.get_ref(owner=owner, repo=repo, ref=f"heads/{branch}").object.sha
        except HTTP4xxClientError as exc:
            last_exc = exc
            if _http_status(exc) not in (404, 409):
                raise _github_error(exc)
            time.sleep(delay)
    raise _github_error(last_exc) if last_exc else HTTPException(status_code=500, detail="branch not ready")


def _bootstrap_new_repo(
    gh: GhApi, owner: str, repo: str, default_branch: str, files: dict[str, str], request: "CommitRequest"
) -> CommitResponse:
    """New-repo flow: push files + a terraform-plan workflow to a first-commit branch
    and open a PR into the default branch (which triggers the plan-on-PR action)."""
    base_sha = _branch_sha(gh, owner, repo, default_branch)

    try:
        gh.git.create_ref(owner=owner, repo=repo, ref=f"refs/heads/{FIRST_COMMIT_BRANCH}", sha=base_sha)
    except HTTP4xxClientError as exc:
        if _http_status(exc) != 422:  # 422 = ref already exists
            raise _github_error(exc)

    tf_dir = request.destination.strip("/") or "."
    workflow_path = ".github/workflows/terraform-plan.yml"
    payload = dict(files)
    payload[workflow_path] = _TERRAFORM_PLAN_WORKFLOW.replace("__TF_DIR__", tf_dir)

    commit_sha = _commit_tree(gh, owner, repo, base_sha, payload, request.message)
    gh.git.update_ref(owner=owner, repo=repo, ref=f"heads/{FIRST_COMMIT_BRANCH}", sha=commit_sha)

    try:
        pr = gh.pulls.create(
            owner=owner,
            repo=repo,
            title="First commit",
            head=FIRST_COMMIT_BRANCH,
            base=default_branch,
            body=(
                "Automated first commit.\n\n"
                "The **Terraform Plan** workflow runs `terraform plan` on this PR and "
                "posts the result as a comment."
            ),
        )
    except HTTP4xxClientError as exc:
        raise _github_error(exc, default_status=422)

    return CommitResponse(
        repo=f"{owner}/{repo}",
        branch=FIRST_COMMIT_BRANCH,
        commit_sha=commit_sha,
        files_committed=sorted(payload),
        created_repo=True,
        pull_request_url=pr.html_url,
        workflow_path=workflow_path,
    )


@app.post(
    "/repos/{owner}/{repo}/commit",
    response_model=CommitResponse,
    summary="Commit files to a GitHub repository",
    responses={
        200: {"description": "Files committed successfully"},
        422: {"description": "No files provided or repo creation failed"},
    },
)
def commit_files(
    owner: str,
    repo: str,
    request: CommitRequest,
    gh: GhApi = Depends(get_github_client),
):
    """
    Commit one or more files to a GitHub repo in a single commit.

    If the repo **does not exist** it is created with a `main` branch, the files
    (plus a Terraform-plan GitHub Actions workflow) are pushed to a `first-commit`
    branch, and a PR is opened into `main` — the workflow then runs `terraform plan`
    and comments the result on the PR.

    For an **existing** repo the files are committed straight to `branch`.

    - `folder`: source prefix stripped from each file path key
    - `destination`: target directory in the repo where files are placed
    - `branch`: created from the default branch if it doesn't exist (existing repos only)
    """
    if not request.files:
        raise HTTPException(status_code=422, detail="No files provided")

    default_branch, created = _ensure_repo(gh, owner, repo, request.private)
    files = _map_paths(request.files, request.folder, request.destination)

    if created:
        return _bootstrap_new_repo(gh, owner, repo, default_branch, files, request)

    branch = request.branch
    try:
        base_sha = gh.git.get_ref(owner=owner, repo=repo, ref=f"heads/{branch}").object.sha
    except HTTP4xxClientError as exc:
        if _http_status(exc) != 404:
            raise _github_error(exc)
        base_sha = gh.git.get_ref(owner=owner, repo=repo, ref=f"heads/{default_branch}").object.sha
        gh.git.create_ref(owner=owner, repo=repo, ref=f"refs/heads/{branch}", sha=base_sha)

    commit_sha = _commit_tree(gh, owner, repo, base_sha, files, request.message)
    gh.git.update_ref(owner=owner, repo=repo, ref=f"heads/{branch}", sha=commit_sha)

    return CommitResponse(
        repo=f"{owner}/{repo}",
        branch=branch,
        commit_sha=commit_sha,
        files_committed=sorted(files),
    )
