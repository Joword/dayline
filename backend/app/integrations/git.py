"""Git activity: GitHub / GitLab sources and preprocessing (plan §4.1 step 2, §9.1).

Preprocessing drops merge and formatting commits and groups the rest by repository
and pull/merge request, so the model sees "what was worked on", not a commit log.
Commit counts are context only: they are not a measure of output.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Any

from app.integrations.http import TargetClient
from app.integrations.models import GitConfig, IntegrationAuthError


@dataclass
class Commit:
    sha: str
    repo: str
    title: str
    url: str
    authored_at: datetime
    parents: int = 1
    message: str = ""
    author_email: str | None = None


@dataclass
class PullRequest:
    repo: str
    number: int
    title: str
    url: str
    state: str  # open | merged | closed


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# --- sources ------------------------------------------------------------------------


class GitHubSource:
    def __init__(self, cfg: GitConfig, http: TargetClient):
        self.cfg, self.http = cfg, http
        self.api = (cfg.base_url or "https://api.github.com").rstrip("/")
        self.headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {cfg.token.get_secret_value()}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def fetch(self, start: datetime, end: datetime) -> tuple[list[Commit], list[PullRequest]]:
        user = self.cfg.username
        rng = f"{start.isoformat(timespec='seconds')}..{end.isoformat(timespec='seconds')}"
        commits: list[Commit] = []
        for page in range(1, 4):
            data = await self.http.json(
                "GET",
                f"{self.api}/search/commits",
                params={
                    "q": f"author:{user} author-date:{rng}",
                    "per_page": 100,
                    "page": page,
                    "sort": "author-date",
                    "order": "asc",
                },
                headers=self.headers,
            )
            items = data.get("items", [])
            for it in items:
                c = it["commit"]
                commits.append(
                    Commit(
                        sha=it["sha"],
                        repo=it["repository"]["full_name"],
                        title=c["message"].splitlines()[0] if c["message"] else "",
                        message=c["message"],
                        url=it["html_url"],
                        authored_at=_dt(c["author"]["date"]),
                        parents=len(it.get("parents") or [None]),
                        author_email=c["author"].get("email"),
                    )
                )
            if len(items) < 100:
                break
        data = await self.http.json(
            "GET",
            f"{self.api}/search/issues",
            params={"q": f"type:pr author:{user} updated:{rng}", "per_page": 100},
            headers=self.headers,
        )
        prs = [
            PullRequest(
                repo="/".join(it["repository_url"].rstrip("/").split("/")[-2:]),
                number=it["number"],
                title=it["title"],
                url=it["html_url"],
                state="merged" if (it.get("pull_request") or {}).get("merged_at") else it["state"],
            )
            for it in data.get("items", [])
        ]
        return commits, prs


class GitLabSource:
    MAX_PROJECTS = 20

    def __init__(self, cfg: GitConfig, http: TargetClient):
        self.cfg, self.http = cfg, http
        self.api = (cfg.base_url or "https://gitlab.com").rstrip("/") + "/api/v4"
        self.headers = {"PRIVATE-TOKEN": cfg.token.get_secret_value()}

    async def _get(self, path: str, **params: Any) -> Any:
        return await self.http.json("GET", f"{self.api}{path}", params=params, headers=self.headers)

    async def fetch(self, start: datetime, end: datetime) -> tuple[list[Commit], list[PullRequest]]:
        me = await self._get("/user")
        if not me.get("id"):
            raise IntegrationAuthError("GitLab token does not identify a user")
        emails = {
            e.lower()
            for e in [*self.cfg.emails, me.get("email"), me.get("commit_email"), me.get("public_email")]
            if e
        }
        events = await self._get(
            f"/users/{me['id']}/events",
            action="pushed",
            after=(start.date() - timedelta(days=1)).isoformat(),
            before=(end.date() + timedelta(days=1)).isoformat(),
            per_page=100,
        )
        project_ids = list(dict.fromkeys(e["project_id"] for e in events if e.get("project_id")))[
            : self.MAX_PROJECTS
        ]
        commits: list[Commit] = []
        for pid in project_ids:
            proj = await self._get(f"/projects/{pid}")
            rows = await self._get(
                f"/projects/{pid}/repository/commits",
                since=start.isoformat(timespec="seconds"),
                until=end.isoformat(timespec="seconds"),
                all="true",
                per_page=100,
            )
            for c in rows:
                mine = (
                    (c.get("author_email") or "").lower() in emails
                    if emails
                    else c.get("author_name") == me.get("name")
                )
                if not mine:
                    continue
                commits.append(
                    Commit(
                        sha=c["id"],
                        repo=proj["path_with_namespace"],
                        title=c.get("title") or "",
                        message=c.get("message") or "",
                        url=c.get("web_url") or "",
                        authored_at=_dt(c["authored_date"]),
                        parents=len(c.get("parent_ids") or [None]),
                        author_email=c.get("author_email"),
                    )
                )
        mrs = await self._get(
            "/merge_requests",
            scope="created_by_me",
            state="all",
            updated_after=start.isoformat(timespec="seconds"),
            updated_before=end.isoformat(timespec="seconds"),
            per_page=100,
        )
        prs = [
            PullRequest(
                repo=(m.get("references") or {}).get("full", "").split("!")[0] or str(m.get("project_id")),
                number=m["iid"],
                title=m["title"],
                url=m["web_url"],
                state={"opened": "open"}.get(m["state"], m["state"]),
            )
            for m in mrs
        ]
        return commits, prs


def make_git_source(cfg: GitConfig, http: TargetClient) -> GitHubSource | GitLabSource:
    return GitHubSource(cfg, http) if cfg.provider == "github" else GitLabSource(cfg, http)


# --- preprocessing --------------------------------------------------------------------

MERGE_RE = re.compile(r"^merge (branch|pull request|remote-tracking|tag|.+ into )", re.IGNORECASE)
FORMAT_RE = re.compile(
    r"^(style|fmt|format|lint)(\([^)]*\))?!?:"
    r"|\b(prettier|gofmt|black|isort|reformat(ted)?|formatting|whitespace|lint fix(es)?|eslint --fix)\b"
    r"|格式化|代码格式|代码风格",
    re.IGNORECASE,
)
PR_REF_RE = re.compile(
    r"\(#(\d+)\)|pull request #(\d+)|merge request [\w./-]+!(\d+)|\(!(\d+)\)", re.IGNORECASE
)


def _pr_number(c: Commit) -> int | None:
    m = PR_REF_RE.search(c.message or c.title)
    return int(next(g for g in m.groups() if g)) if m else None


def summarize_git(
    commits: list[Commit], prs: list[PullRequest], *, provider: str, tz: tzinfo
) -> dict[str, Any]:
    dropped = {"merge": 0, "format": 0}
    kept: list[Commit] = []
    seen: set[str] = set()
    for c in sorted(commits, key=lambda x: x.authored_at):
        if c.sha in seen:
            continue
        seen.add(c.sha)
        if c.parents > 1 or MERGE_RE.match(c.title):
            dropped["merge"] += 1
        elif FORMAT_RE.search(c.title):
            dropped["format"] += 1
        else:
            kept.append(c)

    pr_index = {(p.repo, p.number): p for p in prs}
    repos: dict[str, dict[str, Any]] = {}

    def repo_entry(name: str) -> dict[str, Any]:
        return repos.setdefault(name, {"repo": name, "commit_count": 0, "prs": {}, "other_commits": []})

    for p in prs:  # PRs opened / updated / merged today, even without own commits today
        repo_entry(p.repo)["prs"][p.number] = {
            "number": p.number,
            "title": p.title,
            "url": p.url,
            "state": p.state,
            "commits": [],
        }
    for c in kept:
        r = repo_entry(c.repo)
        r["commit_count"] += 1
        n = _pr_number(c)
        if n is not None:
            pr = pr_index.get((c.repo, n))
            entry = r["prs"].setdefault(
                n,
                {
                    "number": n,
                    "title": pr.title if pr else "",
                    "url": pr.url if pr else "",
                    "state": pr.state if pr else "",
                    "commits": [],
                },
            )
            if len(entry["commits"]) < 10:
                entry["commits"].append(c.title)
        elif len(r["other_commits"]) < 15:
            r["other_commits"].append(
                {"title": c.title, "url": c.url, "time": c.authored_at.astimezone(tz).strftime("%H:%M")}
            )

    out = sorted(repos.values(), key=lambda r: -r["commit_count"])
    for r in out:
        r["prs"] = list(r["prs"].values())
    return {
        "provider": provider,
        "repos": out,
        "totals": {"commits": len(kept), "repos": len(out), "prs": sum(len(r["prs"]) for r in out)},
        "dropped": dropped,
    }
