"""Last-change dates for repository folders from the Azure DevOps Git REST API.

Uses `Items - List` with ``latestProcessedChange=true`` (API 7.1): Azure DevOps
computes the latest commit per folder on its side, so a whole repository takes
one call per top-level folder instead of walking commit history. Read-only.

Only a date and a short commit id are kept per item folder. Author names and
commit messages are deliberately not stored.

Auth: a bearer token from ``AZURE_DEVOPS_TOKEN``, else ``az account get-access-token``.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime

ADO_RESOURCE_ID = "499b84ac-1321-427f-aa17-267ca6975798"   # Azure DevOps first-party app
API_VERSION = "7.1"
SCHEMA_VERSION = 1
ITEM_SUFFIXES = (".Notebook", ".DataPipeline", ".SemanticModel", ".Report", ".Lakehouse",
                 ".Environment", ".VariableLibrary", ".MirroredDatabase", ".PaginatedReport")

# transport(url, headers) -> (status, response headers, body text)
Transport = Callable[[str, dict], tuple[int, dict, str]]


class DateFetchError(RuntimeError):
    pass


def default_transport(url: str, headers: dict) -> tuple[int, dict, str]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read().decode("utf-8", errors="replace")


def get_token() -> str:
    token = os.environ.get("AZURE_DEVOPS_TOKEN")
    if token:
        return token.strip()
    try:
        out = subprocess.run(
            ["az", "account", "get-access-token", "--resource", ADO_RESOURCE_ID,
             "--query", "accessToken", "-o", "tsv"],
            capture_output=True, text=True, timeout=60, shell=(os.name == "nt"))
    except (OSError, subprocess.SubprocessError) as exc:
        raise DateFetchError(f"no AZURE_DEVOPS_TOKEN and Azure CLI unavailable: {exc}") from exc
    if out.returncode != 0 or not out.stdout.strip():
        raise DateFetchError("no AZURE_DEVOPS_TOKEN and `az account get-access-token` failed; "
                             "run `az login` or set AZURE_DEVOPS_TOKEN")
    return out.stdout.strip()


class AdoClient:
    def __init__(self, organization: str, project: str, repo: str, branch: str = "main",
                 token: str | None = None, transport: Transport | None = None,
                 sleep: Callable[[float], None] = time.sleep, max_retries: int = 5) -> None:
        self.organization, self.project, self.repo, self.branch = organization, project, repo, branch
        self._token = token
        self.transport = transport or default_transport
        self.sleep, self.max_retries = sleep, max_retries
        self.calls = 0

    def _headers(self) -> dict:
        if self._token is None:
            self._token = get_token()
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}

    def items(self, scope_path: str, recursion: str) -> list[dict]:
        q = urllib.parse.urlencode({
            "scopePath": scope_path, "recursionLevel": recursion, "latestProcessedChange": "true",
            "versionDescriptor.version": self.branch, "versionDescriptor.versionType": "branch",
            "api-version": API_VERSION})
        url = (f"https://dev.azure.com/{urllib.parse.quote(self.organization)}/"
               f"{urllib.parse.quote(self.project)}/_apis/git/repositories/"
               f"{urllib.parse.quote(self.repo)}/items?{q}")
        for attempt in range(self.max_retries + 1):
            self.calls += 1
            status, headers, body = self.transport(url, self._headers())
            if status == 200:
                return json.loads(body).get("value", [])
            if status in (401, 403):
                raise DateFetchError(f"HTTP {status}: token rejected or no read access to the repository")
            if status == 429 or status >= 500:
                wait = float(headers.get("Retry-After", 2 ** attempt))
                self.sleep(min(wait, 60))
                continue
            raise DateFetchError(f"HTTP {status} for scope {scope_path}")
        raise DateFetchError(f"gave up after {self.max_retries} retries for scope {scope_path}")


def _is_item_folder(path: str) -> bool:
    return path.endswith(ITEM_SUFFIXES)


def _entry(item: dict) -> dict | None:
    change = item.get("latestProcessedChange") or {}
    date = (change.get("committer") or {}).get("date")
    commit = change.get("commitId")
    if not date or not commit:
        return None
    return {"lastChange": date, "commit": commit[:8]}


def fetch_dates(client: AdoClient, folders: list[str] | None = None) -> dict:
    """Return the dates document: {schemaVersion, source, branch, fetchedAt, items{path: {...}}}."""
    items: dict[str, dict] = {}
    root = client.items("/", "OneLevel")
    top = [i for i in root if i.get("isFolder") and i.get("path") not in ("/", None)]
    for node in top:
        path = node["path"]
        if folders and path.strip("/") not in folders:
            continue
        if _is_item_folder(path):                       # an item at the repository root
            e = _entry(node)
            if e:
                items[path.strip("/")] = e
            continue
        for child in client.items(path, "Full"):
            cpath = child.get("path", "")
            if child.get("isFolder") and _is_item_folder(cpath):
                e = _entry(child)
                if e:
                    items[cpath.strip("/")] = e
    return {"schemaVersion": SCHEMA_VERSION, "source": "azure-devops", "branch": client.branch,
            "fetchedAt": datetime.now(UTC).isoformat(timespec="seconds"),
            "apiCalls": client.calls, "items": items}


def save_dates(doc: dict, path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1)


def load_dates(path: str) -> dict[str, dict]:
    """Return {item folder path: {lastChange, commit}} from a dates document."""
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    if doc.get("schemaVersion") != SCHEMA_VERSION:
        raise DateFetchError(f"unsupported dates schemaVersion: {doc.get('schemaVersion')}")
    return doc["items"]
