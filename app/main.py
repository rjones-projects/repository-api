"""
GitHub File API — fetch files and commit changes to GitHub repos.
"""

import base64
import json
import os
import yaml
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, Query, Depends
from fastapi.responses import Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from ghapi.all import GhApi
from fastcore.net import HTTP4xxClientError
from pydantic import BaseModel, Field

app = FastAPI(
    title="Repo API",
    description="Fetch files from GitHub repositories and commit changes",
    version="2.0.0",
)

security = HTTPBearer(auto_error=False)


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


# ── Helpers ──────────────────────────────────────────────────────────────────

def get_github_client(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> GhApi:
    """Resolve GitHub token from the Authorization: Bearer header, or the GITHUB_TOKEN env var."""
    resolved_token = None
    if credentials:
        resolved_token = credentials.credentials
    elif os.getenv("GITHUB_TOKEN"):
        resolved_token = os.getenv("GITHUB_TOKEN")
    return GhApi(token=resolved_token)


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
    if hasattr(exc, "response") and exc.response is not None:
        return exc.response.status_code
    return 0


def _github_error(exc: HTTP4xxClientError, default_status: int = 404) -> HTTPException:
    status = _http_status(exc) or default_status
    msg = str(exc)
    try:
        msg = json.loads(msg).get("message", msg)
    except Exception:
        pass
    return HTTPException(status_code=status, detail=msg)


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

def _ensure_repo(gh: GhApi, owner: str, repo: str, private: bool) -> str:
    """Return the default branch of the repo, creating it if it doesn't exist."""
    try:
        return gh.repos.get(owner=owner, repo=repo).default_branch
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

    return r.default_branch


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
    The repo is created automatically if it does not exist.

    - `folder`: source prefix stripped from each file path key
    - `destination`: target directory in the repo where files are placed
    - `branch`: created from the default branch if it doesn't exist
    """
    if not request.files:
        raise HTTPException(status_code=422, detail="No files provided")

    default_branch = _ensure_repo(gh, owner, repo, request.private)
    branch = request.branch

    try:
        ref_obj = gh.git.get_ref(owner=owner, repo=repo, ref=f"heads/{branch}")
        base_sha = ref_obj.object.sha
    except HTTP4xxClientError as exc:
        if _http_status(exc) != 404:
            raise _github_error(exc)
        default_ref = gh.git.get_ref(owner=owner, repo=repo, ref=f"heads/{default_branch}")
        base_sha = default_ref.object.sha
        gh.git.create_ref(owner=owner, repo=repo, ref=f"refs/heads/{branch}", sha=base_sha)

    base_tree_sha = gh.git.get_commit(owner=owner, repo=repo, commit_sha=base_sha).tree.sha

    tree_entries = []
    committed_paths = []
    folder_prefix = request.folder.rstrip("/") + "/" if request.folder else ""

    for file_path, content in request.files.items():
        rel = file_path[len(folder_prefix):] if folder_prefix and file_path.startswith(folder_prefix) else file_path
        dest = f"{request.destination.rstrip('/')}/{rel}" if request.destination else rel
        dest = dest.lstrip("/")

        blob = gh.git.create_blob(owner=owner, repo=repo, content=content, encoding="utf-8")
        tree_entries.append({"path": dest, "mode": "100644", "type": "blob", "sha": blob.sha})
        committed_paths.append(dest)

    new_tree = gh.git.create_tree(owner=owner, repo=repo, tree=tree_entries, base_tree=base_tree_sha)
    new_commit = gh.git.create_commit(
        owner=owner, repo=repo,
        message=request.message,
        tree=new_tree.sha,
        parents=[base_sha],
    )
    gh.git.update_ref(owner=owner, repo=repo, ref=f"heads/{branch}", sha=new_commit.sha)

    return CommitResponse(
        repo=f"{owner}/{repo}",
        branch=branch,
        commit_sha=new_commit.sha,
        files_committed=committed_paths,
    )
