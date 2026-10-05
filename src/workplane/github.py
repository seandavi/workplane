"""Read-only GitHub GraphQL client that yields normalized issues and PRs."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.github.com/graphql"

_ISSUE_FIELDS = """
fragment IssueFields on Issue {
  id number title url state stateReason createdAt updatedAt closedAt
  author { login __typename }
  repository { nameWithOwner isPrivate isFork isArchived }
  labels(first: 20) { nodes { name } }
  assignees(first: 10) { nodes { login } }
  comments { totalCount }
}
"""
_PR_FIELDS = """
fragment PrFields on PullRequest {
  id number title url state createdAt updatedAt closedAt isDraft
  author { login __typename }
  repository { nameWithOwner isPrivate isFork isArchived }
  labels(first: 20) { nodes { name } }
  assignees(first: 10) { nodes { login } }
  comments { totalCount }
  reviewRequests(first: 10) {
    nodes { requestedReviewer { ... on User { login } ... on Team { slug } } }
  }
}
"""

_OWNER_REPOS = """
query($login: String!, $after: String, $isFork: Boolean) {
  repositoryOwner(login: $login) {
    __typename
    repositories(first: 100, after: $after, ownerAffiliations: [OWNER], isFork: $isFork) {
      pageInfo { hasNextPage endCursor }
      nodes {
        nameWithOwner isPrivate isArchived
        issues(states: OPEN) { totalCount }
        pullRequests(states: OPEN) { totalCount }
      }
    }
  }
}
"""

_REPO = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    nameWithOwner isPrivate isArchived
    issues(states: OPEN) { totalCount }
    pullRequests(states: OPEN) { totalCount }
  }
}
"""

_OPEN_ISSUES = (
    """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    items: issues(first: 50, after: $after, states: OPEN) {
      pageInfo { hasNextPage endCursor }
      nodes { ...IssueFields }
    }
  }
}
"""
    + _ISSUE_FIELDS
)

_OPEN_PRS = (
    """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    items: pullRequests(first: 50, after: $after, states: OPEN) {
      pageInfo { hasNextPage endCursor }
      nodes { ...PrFields }
    }
  }
}
"""
    + _PR_FIELDS
)

_SEARCH = (
    """
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 50, after: $after) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes { __typename ...IssueFields ...PrFields }
  }
}
"""
    + _ISSUE_FIELDS
    + _PR_FIELDS
)

_NODES = (
    """
query($ids: [ID!]!) {
  nodes(ids: $ids) { __typename id ...IssueFields ...PrFields }
}
"""
    + _ISSUE_FIELDS
    + _PR_FIELDS
)


@dataclass(frozen=True, slots=True)
class RepoInfo:
    full_name: str
    is_private: bool
    open_count: int


@dataclass(frozen=True, slots=True)
class GhItem:
    node_id: str
    repo: str
    repo_private: bool
    repo_fork: bool
    repo_archived: bool
    number: int
    kind: str  # "issue" | "pr"
    title: str
    state: str  # "open" | "closed" | "merged"
    state_reason: str | None  # COMPLETED / NOT_PLANNED / DUPLICATE / UNMERGED
    author: str | None
    labels: list[str]
    assignees: list[str]
    review_requests: list[str]
    comments_count: int
    is_draft: bool
    url: str
    created_at: dt.datetime
    updated_at: dt.datetime
    closed_at: dt.datetime | None


def _login(author: dict[str, Any] | None) -> str | None:
    """GraphQL drops the ``[bot]`` suffix that REST and the web UI show; put it back."""
    if not author:
        return None
    login = author["login"]
    return f"{login}[bot]" if author.get("__typename") == "Bot" else login


class GitHubError(Exception):
    pass


def _ts(value: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def parse_item(node: dict[str, Any]) -> GhItem:
    is_pr = node.get("__typename") == "PullRequest" or "isDraft" in node
    state = node["state"].lower()
    reason = node.get("stateReason")
    if is_pr and state == "closed":
        reason = "UNMERGED"
    repo = node["repository"]
    reviewers = []
    for rr in (node.get("reviewRequests") or {}).get("nodes", []):
        who = rr.get("requestedReviewer") or {}
        if who.get("login") or who.get("slug"):
            reviewers.append(who.get("login") or who["slug"])
    return GhItem(
        node_id=node["id"],
        repo=repo["nameWithOwner"],
        repo_private=repo["isPrivate"],
        repo_fork=repo.get("isFork", False),
        repo_archived=repo.get("isArchived", False),
        number=node["number"],
        kind="pr" if is_pr else "issue",
        title=node["title"],
        state=state,
        state_reason=reason,
        author=_login(node.get("author")),
        labels=[n["name"] for n in node["labels"]["nodes"]],
        assignees=[n["login"] for n in node["assignees"]["nodes"]],
        review_requests=reviewers,
        comments_count=node["comments"]["totalCount"],
        is_draft=bool(node.get("isDraft", False)),
        url=node["url"],
        created_at=_ts(node["createdAt"]),  # type: ignore[arg-type]
        updated_at=_ts(node["updatedAt"]),  # type: ignore[arg-type]
        closed_at=_ts(node.get("closedAt")),
    )


class GitHub:
    def __init__(self, token: str, *, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(60.0),
            headers={"Authorization": f"bearer {token}", "User-Agent": "workplane"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def query(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(4):
            resp = await self._client.post(GRAPHQL_URL, json={"query": query, "variables": variables})
            if resp.status_code in (502, 503, 504) or (
                resp.status_code == 403 and "rate limit" in resp.text.lower()
            ):
                wait = 2 ** attempt * 5
                log.warning("GitHub %s; retrying in %ss", resp.status_code, wait)
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            body = resp.json()
            if body.get("errors"):
                # NOT_FOUND inside nodes() is expected for deleted items; let callers see data.
                fatal = [e for e in body["errors"] if e.get("type") != "NOT_FOUND"]
                if fatal or body.get("data") is None:
                    raise GitHubError(body["errors"])
            return body["data"]
        raise GitHubError(f"GitHub kept failing: {resp.status_code}")

    async def owner_repos(self, login: str, *, include_forks: bool) -> list[RepoInfo]:
        repos: list[RepoInfo] = []
        after = None
        while True:
            data = await self.query(
                _OWNER_REPOS,
                {"login": login, "after": after, "isFork": None if include_forks else False},
            )
            owner = data["repositoryOwner"]
            if owner is None:
                raise GitHubError(f"no such GitHub user or org: {login}")
            page = owner["repositories"]
            for n in page["nodes"]:
                if n["isArchived"]:
                    continue
                count = n["issues"]["totalCount"] + n["pullRequests"]["totalCount"]
                repos.append(RepoInfo(n["nameWithOwner"], n["isPrivate"], count))
            if not page["pageInfo"]["hasNextPage"]:
                return repos
            after = page["pageInfo"]["endCursor"]

    async def repo(self, full_name: str) -> RepoInfo:
        owner, name = full_name.split("/", 1)
        data = await self.query(_REPO, {"owner": owner, "name": name})
        n = data["repository"]
        if n is None:
            raise GitHubError(f"no such repository: {full_name}")
        count = n["issues"]["totalCount"] + n["pullRequests"]["totalCount"]
        return RepoInfo(n["nameWithOwner"], n["isPrivate"], count)

    async def open_items(self, full_name: str) -> AsyncIterator[GhItem]:
        owner, name = full_name.split("/", 1)
        for query in (_OPEN_ISSUES, _OPEN_PRS):
            after = None
            while True:
                data = await self.query(query, {"owner": owner, "name": name, "after": after})
                page = data["repository"]["items"]
                for node in page["nodes"]:
                    yield parse_item(node)
                if not page["pageInfo"]["hasNextPage"]:
                    break
                after = page["pageInfo"]["endCursor"]

    async def search(self, q: str) -> tuple[int, list[GhItem]]:
        """Run an issue search; returns (total matches, items). GitHub caps results at 1000."""
        items: list[GhItem] = []
        after = None
        total = 0
        while True:
            data = await self.query(_SEARCH, {"q": q, "after": after})
            page = data["search"]
            total = page["issueCount"]
            items.extend(parse_item(n) for n in page["nodes"] if n)
            if not page["pageInfo"]["hasNextPage"]:
                return total, items
            after = page["pageInfo"]["endCursor"]

    async def nodes(self, node_ids: list[str]) -> dict[str, GhItem | None]:
        """Fetch items by node id; deleted or inaccessible ones map to ``None``."""
        found: dict[str, GhItem | None] = {}
        for i in range(0, len(node_ids), 50):
            batch = node_ids[i : i + 50]
            data = await self.query(_NODES, {"ids": batch})
            for node_id, node in zip(batch, data["nodes"], strict=True):
                found[node_id] = parse_item(node) if node else None
        return found
