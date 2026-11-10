#!/usr/bin/env python3
"""Deterministic state and planning for the daily engineering lab.

Subcommands:

  plan    Read the portfolio from GitHub through the gh CLI and choose today's
          single action, without involving a model:

            revise   an open lab pull request has owner feedback newer than
                     the lab's last response, or failing checks the lab has
                     tried to fix fewer than MAX_CI_FIX_ATTEMPTS times
            improve  a portfolio repository with no open lab pull request,
                     picked by least-recently-considered rotation (RUNS.md is
                     the memory; owner-filed issues jump the queue)
            skip     nothing is eligible, e.g. MAX_OPEN_PRS lab pull requests
                     already await review

          It runs in the workflow's `plan` job, before any model or project
          code, and is one of only two places that hold the GitHub token.

  render    Fill ${PLACEHOLDER}s in a prompt template, strictly.

  sanitize  Make model-written Markdown safe to post under the owner's
            account: no co-author lines, no @mentions, no closing keywords.

Standard library only: it runs on a bare ubuntu-latest runner.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

# Every comment the lab posts on a pull request starts with this hidden
# marker. The lab and the owner post under the same account, so the marker is
# the only way to tell the lab's own replies apart from the owner's feedback.
MARKER = "<!-- daily-lab"

CO_AUTHOR_RE = re.compile(r"^\s*co-authored-by:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
RUNS_ROW_RE = re.compile(r"^\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*([A-Za-z-]+)\s*\|\s*([^|]*?)\s*\|")
PLACEHOLDER_RE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
RUN_URL_RE = re.compile(r"/actions/runs/(\d+)")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
REPO_ARG_RE = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)?$")

FAILING_CHECK_CONCLUSIONS = frozenset({"FAILURE", "TIMED_OUT", "STARTUP_FAILURE"})
FAILING_STATUS_STATES = frozenset({"FAILURE", "ERROR"})

# Context handed to the model goes through a command-line argument, which
# Linux caps at 128 KiB; stay far below it.
MAX_CONTEXT_CHARS = 40_000

REPO_FIELDS = "name,nameWithOwner,description,primaryLanguage,defaultBranchRef,isPrivate,url"
PR_FIELDS = "number,title,state,url,headRefName,createdAt,closedAt,mergedAt,isCrossRepository,author"
PR_VIEW_FIELDS = "title,body,url,headRefName,headRefOid,createdAt,commits,statusCheckRollup"


# --------------------------------------------------------------------------
# gh
# --------------------------------------------------------------------------

class GhError(RuntimeError):
    """A gh invocation failed."""


Runner = Callable[[list[str]], str]


def _run_gh(args: list[str]) -> str:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8", check=False)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or f"exit status {proc.returncode}"
        raise GhError(f"gh {' '.join(args)}: {detail}")
    return proc.stdout


class Gh:
    """Thin wrapper over the gh CLI; tests swap the runner for fixtures."""

    def __init__(self, runner: Runner = _run_gh) -> None:
        self._run = runner

    def text(self, *args: str) -> str:
        return self._run(list(args))

    def json(self, *args: str) -> Any:
        out = self._run(list(args))
        return json.loads(out) if out.strip() else None

    def pages(self, path: str) -> list[Any]:
        """Every item of a paginated REST list endpoint."""
        pages = self.json("api", "--paginate", "--slurp", path) or []
        return [item for page in pages for item in page]


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def parse_ts(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def same_login(a: str | None, b: str | None) -> bool:
    return bool(a and b and a.lower() == b.lower())


def by(item: Mapping[str, Any], login: str) -> bool:
    return same_login((item.get("user") or {}).get("login"), login)


def clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "\n[... truncated]"


def one_line(text: str, limit: int = 200) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def quote_block(text: str) -> str:
    return "\n".join("> " + line if line else ">" for line in text.splitlines()) or "> (empty)"


def fence(text: str, lang: str = "") -> str:
    return f"```{lang}\n{text.replace('```', chr(39) * 3)}\n```"


# --------------------------------------------------------------------------
# configuration and identity
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Config:
    topic: str = "daily-gemini-project"
    branch_prefix: str = "lab/"
    max_open_prs: int = 3
    max_ci_fix_attempts: int = 2
    issue_boost_days: int = 7
    require_clean_history: bool = True
    force_repo: str = ""
    commit_name: str = ""
    commit_email: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Config:
        def text(key: str, default: str) -> str:
            return (env.get(key) or "").strip() or default

        def number(key: str, default: int) -> int:
            raw = (env.get(key) or "").strip()
            if not raw:
                return default
            if not raw.isdigit():
                raise ValueError(f"{key} must be a non-negative integer, got {raw!r}")
            return int(raw)

        force = (env.get("FORCE_REPO") or "").strip()
        if force and not REPO_ARG_RE.match(force):
            raise ValueError(f"FORCE_REPO {force!r} is not a repository name")
        prefix = text("BRANCH_PREFIX", cls.branch_prefix)
        if not prefix.endswith("/"):
            raise ValueError(f"BRANCH_PREFIX must end with '/', got {prefix!r}")
        return cls(
            topic=text("TOPIC", cls.topic),
            branch_prefix=prefix,
            max_open_prs=number("MAX_OPEN_PRS", cls.max_open_prs),
            max_ci_fix_attempts=number("MAX_CI_FIX_ATTEMPTS", cls.max_ci_fix_attempts),
            issue_boost_days=number("ISSUE_BOOST_DAYS", cls.issue_boost_days),
            require_clean_history=text("REQUIRE_CLEAN_HISTORY", "true").lower() != "false",
            force_repo=force,
            commit_name=(env.get("COMMIT_NAME") or "").strip(),
            commit_email=(env.get("COMMIT_EMAIL") or "").strip(),
        )


@dataclass(frozen=True)
class Identity:
    login: str
    name: str
    email: str
    # None when the token does not report OAuth scopes (fine-grained tokens).
    scopes: tuple[str, ...] | None

    @property
    def can_edit_workflows(self) -> str:
        if self.scopes is None:
            return "unknown"
        return "true" if "workflow" in self.scopes else "false"


def split_http_response(raw: str) -> tuple[dict[str, str], str]:
    """Split `gh api --include` output into lower-cased headers and the body."""
    text = raw.replace("\r\n", "\n")
    head, sep, body = text.partition("\n\n")
    if not sep:
        raise ValueError("gh api --include output has no blank line between headers and body")
    headers: dict[str, str] = {}
    for line in head.split("\n")[1:]:  # the first line is the HTTP status line
        name, colon, value = line.partition(":")
        if colon:
            headers[name.strip().lower()] = value.strip()
    return headers, body


def resolve_identity(gh: Gh, cfg: Config, *, fine_grained: bool) -> Identity:
    """The account behind the token, and the commit identity to use for it.

    The default email is the account's ID-based noreply address: GitHub always
    links it to the account (so the commit shows up as yours in contributor
    lists), it survives username changes, and it keeps your real address out
    of public history.
    """
    headers, body = split_http_response(gh.text("api", "--include", "user"))
    user = json.loads(body)
    login = user["login"]
    name = cfg.commit_name or (user.get("name") or "").strip() or login
    email = cfg.commit_email or f"{user['id']}+{login}@users.noreply.github.com"
    header = headers.get("x-oauth-scopes")
    scopes: tuple[str, ...] | None
    if fine_grained or header is None:
        scopes = None
    else:
        scopes = tuple(s.strip() for s in header.split(",") if s.strip())
    return Identity(login=login, name=name, email=email, scopes=scopes)


# --------------------------------------------------------------------------
# reading GitHub
# --------------------------------------------------------------------------

def list_portfolio(gh: Gh, owner: str, topic: str) -> list[dict[str, Any]]:
    repos = gh.json(
        "repo", "list", owner, "--topic", topic, "--no-archived", "--source",
        "--limit", "200", "--json", REPO_FIELDS,
    ) or []
    return sorted(repos, key=lambda r: r["name"].lower())


def is_lab_pr(pr: Mapping[str, Any], owner: str, prefix: str) -> bool:
    """Opened by the lab: same-repo branch with the lab prefix, authored by the owner.

    Pull requests from forks are never lab pull requests, whatever their branch
    is called — anyone can open one on a public repository.
    """
    return (
        str(pr.get("headRefName") or "").startswith(prefix)
        and not pr.get("isCrossRepository")
        and same_login((pr.get("author") or {}).get("login"), owner)
    )


def owner_feedback(
    issue_comments: list[dict[str, Any]],
    review_comments: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    owner: str,
    since: dt.datetime,
) -> list[dict[str, Any]]:
    """Owner-written feedback newer than `since`, oldest first.

    Only the owner's words are ever returned. The repositories are public, so
    anyone can comment, and whatever is returned here is handed to a model
    with a shell — a stranger's comment must never get that far.
    """
    items: list[dict[str, Any]] = []
    for c in issue_comments:
        body = (c.get("body") or "").strip()
        if by(c, owner) and body and MARKER not in body and parse_ts(c["created_at"]) > since:
            items.append({"kind": "comment", "at": c["created_at"], "url": c.get("html_url", ""),
                          "body": clip(body, 4000)})
    for c in review_comments:
        body = (c.get("body") or "").strip()
        if by(c, owner) and body and MARKER not in body and parse_ts(c["created_at"]) > since:
            items.append({"kind": "inline", "at": c["created_at"], "url": c.get("html_url", ""),
                          "path": c.get("path") or "", "line": c.get("line") or c.get("original_line"),
                          "diff_hunk": (c.get("diff_hunk") or "")[-1500:], "body": clip(body, 4000)})
    for r in reviews:
        body = (r.get("body") or "").strip()
        submitted = r.get("submitted_at")
        if by(r, owner) and body and submitted and MARKER not in body and parse_ts(submitted) > since:
            items.append({"kind": "review", "at": submitted, "url": r.get("html_url", ""),
                          "state": r.get("state") or "", "body": clip(body, 4000)})
    return sorted(items, key=lambda i: parse_ts(i["at"]))


def failing_checks(rollup: list[dict[str, Any]]) -> list[dict[str, str]]:
    failing = []
    for item in rollup:
        kind = item.get("__typename")
        if kind == "CheckRun":
            if item.get("status") == "COMPLETED" and item.get("conclusion") in FAILING_CHECK_CONCLUSIONS:
                failing.append({"name": item.get("name") or "?", "workflow": item.get("workflowName") or "",
                                "url": item.get("detailsUrl") or ""})
        elif kind == "StatusContext":
            if item.get("state") in FAILING_STATUS_STATES:
                failing.append({"name": item.get("context") or "?", "workflow": "",
                                "url": item.get("targetUrl") or ""})
    return failing


def evaluate_open_pr(gh: Gh, full: str, pr: Mapping[str, Any], owner: str) -> dict[str, Any]:
    number = str(pr["number"])
    view = gh.json("pr", "view", number, "-R", full, "--json", PR_VIEW_FIELDS) or {}
    commits = view.get("commits") or []
    head_time = parse_ts(commits[-1]["committedDate"] if commits else (view.get("createdAt") or pr["createdAt"]))
    issue_comments = gh.pages(f"repos/{full}/issues/{number}/comments?per_page=100")
    review_comments = gh.pages(f"repos/{full}/pulls/{number}/comments?per_page=100")
    reviews = gh.pages(f"repos/{full}/pulls/{number}/reviews?per_page=100")
    replies = [c for c in issue_comments if by(c, owner) and MARKER in (c.get("body") or "")]
    # Feedback counts only if it is newer than both the latest commit and the
    # lab's latest reply: whatever came before was already answered.
    watermark = max([head_time, *(parse_ts(c["created_at"]) for c in replies)])
    return {
        "number": pr["number"],
        "title": view.get("title") or pr.get("title") or "",
        "url": view.get("url") or pr.get("url") or "",
        "branch": view.get("headRefName") or pr.get("headRefName") or "",
        "head_oid": view.get("headRefOid") or "",
        "created_at": pr.get("createdAt") or "",
        "body": clip(view.get("body") or "", 6000),
        "feedback": owner_feedback(issue_comments, review_comments, reviews, owner, watermark),
        "failing_checks": failing_checks(view.get("statusCheckRollup") or []),
        "ci_attempts": sum("kind=ci" in (c.get("body") or "") for c in replies),
        "watermark": watermark.isoformat(),
    }


def history_offenders(gh: Gh, full: str, branch: str, owner: str) -> dict[str, int]:
    """Identities besides the owner that GitHub would list as contributors.

    GitHub builds a repository's contributor list from the commit authors on
    its default branch, so this checks every commit there: authors that are
    not linked to the owner's account, and Co-authored-by trailers. An empty
    result means the owner is the only contributor.
    """
    commits = gh.pages(f"repos/{full}/commits?sha={quote(branch, safe='')}&per_page=100")
    found: Counter[str] = Counter()
    for c in commits:
        login = (c.get("author") or {}).get("login")
        if not same_login(login, owner):
            name = ((c.get("commit") or {}).get("author") or {}).get("name") or "unknown"
            found[login or f"{name} (email not linked to any account)"] += 1
        for match in CO_AUTHOR_RE.finditer((c.get("commit") or {}).get("message") or ""):
            found[f"co-author {match.group(1)}"] += 1
    return dict(found)


def brief(pr: Mapping[str, Any]) -> dict[str, Any]:
    return {"number": pr["number"], "title": pr.get("title") or "", "url": pr.get("url") or "",
            "closed_at": pr.get("mergedAt") or pr.get("closedAt") or ""}


def gather_repo(gh: Gh, repo: Mapping[str, Any], ident: Identity, cfg: Config) -> dict[str, Any]:
    full = repo["nameWithOwner"]
    info: dict[str, Any] = {
        "name": repo["name"],
        "full": full,
        "description": repo.get("description") or "",
        "language": (repo.get("primaryLanguage") or {}).get("name") or "",
        "default_branch": (repo.get("defaultBranchRef") or {}).get("name") or "main",
        "private": bool(repo.get("isPrivate")),
        "url": repo.get("url") or f"https://github.com/{full}",
        "open_prs": [],
        "merged_prs": [],
        "rejected_prs": [],
        "owner_issues": [],
        "history_offenders": {},
        "errors": [],
    }
    try:
        prs = gh.json("pr", "list", "-R", full, "--state", "all", "--limit", "50", "--json", PR_FIELDS) or []
        lab = [p for p in prs if is_lab_pr(p, ident.login, cfg.branch_prefix)]
        info["merged_prs"] = [brief(p) for p in lab if p.get("state") == "MERGED"][:10]
        info["rejected_prs"] = [brief(p) for p in lab if p.get("state") == "CLOSED"][:10]
        info["open_prs"] = [evaluate_open_pr(gh, full, p, ident.login) for p in lab if p.get("state") == "OPEN"]
    except GhError as exc:
        info["errors"].append(str(exc))
    try:
        issues = gh.json(
            "issue", "list", "-R", full, "--state", "open", "--author", ident.login,
            "--limit", "10", "--json", "number,title,body,url",
        ) or []
        info["owner_issues"] = [
            {"number": i["number"], "title": i.get("title") or "", "url": i.get("url") or "",
             "body": clip(i.get("body") or "", 2000)}
            for i in issues
        ]
    except GhError as exc:
        if "disabled issues" not in str(exc):
            info["errors"].append(str(exc))
    if cfg.require_clean_history:
        try:
            info["history_offenders"] = history_offenders(gh, full, info["default_branch"], ident.login)
        except GhError as exc:
            info["errors"].append(str(exc))
    return info


def failed_logs(gh: Gh, full: str, failing: list[dict[str, str]],
                max_runs: int = 2, max_lines: int = 150) -> str:
    """Tail of the failed-step logs for the Actions runs behind failing checks."""
    run_ids: list[str] = []
    for check in failing:
        match = RUN_URL_RE.search(check.get("url") or "")
        if match and match.group(1) not in run_ids:
            run_ids.append(match.group(1))
    chunks = []
    for run_id in run_ids[:max_runs]:
        try:
            log = gh.text("run", "view", run_id, "-R", full, "--log-failed")
        except GhError as exc:
            chunks.append(f"(could not fetch the log of run {run_id}: {one_line(str(exc), 300)})")
            continue
        lines = [line[:400] for line in ANSI_RE.sub("", log).splitlines()[-max_lines:]]
        chunks.append(f"Failed steps of Actions run {run_id}, last {len(lines)} lines:\n\n" + fence("\n".join(lines)))
    return "\n\n".join(chunks)


# --------------------------------------------------------------------------
# the ledger and the decision
# --------------------------------------------------------------------------

def last_considered(runs_md: str) -> dict[str, dt.date]:
    """Latest date each repository appears in RUNS.md, whatever happened then."""
    latest: dict[str, dt.date] = {}
    for line in runs_md.splitlines():
        match = RUNS_ROW_RE.match(line)
        if not match:
            continue
        day, _action, repo = match.groups()
        repo = repo.strip().strip("`").split("/")[-1].strip()
        if not repo or repo == "-":
            continue
        date = dt.date.fromisoformat(day)
        if repo not in latest or date > latest[repo]:
            latest[repo] = date
    return latest


@dataclass
class Selection:
    action: str  # improve | revise | skip
    reason: str
    repo: dict[str, Any] | None = None
    pr: dict[str, Any] | None = None
    revise_kind: str = ""  # feedback | ci
    blocked: list[str] = field(default_factory=list)


def choose(repos: list[dict[str, Any]], cfg: Config, considered: Mapping[str, dt.date]) -> Selection:
    pool = repos
    if cfg.force_repo:
        wanted = cfg.force_repo.split("/")[-1].lower()
        pool = [r for r in repos if r["name"].lower() == wanted]
        if not pool:
            return Selection("skip", f"{cfg.force_repo} is not an active repository tagged {cfg.topic}")

    # 1. Answer feedback before starting anything new; then fix red checks.
    candidates = []
    for repo in pool:
        for pr in repo["open_prs"]:
            if pr["feedback"]:
                candidates.append((0, pr["created_at"], repo, pr, "feedback"))
            elif pr["failing_checks"] and pr["ci_attempts"] < cfg.max_ci_fix_attempts:
                candidates.append((1, pr["created_at"], repo, pr, "ci"))
    if candidates:
        _, _, repo, pr, kind = min(candidates, key=lambda c: (c[0], c[1]))
        if kind == "feedback":
            reason = f"{len(pr['feedback'])} new owner comment(s) on {repo['name']}#{pr['number']}"
        else:
            reason = (f"failing checks on {repo['name']}#{pr['number']} "
                      f"(fix attempt {pr['ci_attempts'] + 1} of {cfg.max_ci_fix_attempts})")
        return Selection("revise", reason, repo, pr, kind)

    # 2. Never let unreviewed work pile up. A manual run aimed at one repo is
    #    an explicit request, so it may exceed the cap.
    open_total = sum(len(r["open_prs"]) for r in repos)
    if open_total >= cfg.max_open_prs and not cfg.force_repo:
        return Selection("skip", f"{open_total} lab pull request(s) await review (cap {cfg.max_open_prs})")

    # 3. Who may receive a new pull request today.
    eligible, blocked = [], []
    for repo in pool:
        if repo["errors"]:
            blocked.append(f"{repo['name']}: could not read its state from GitHub")
        elif repo["open_prs"]:
            blocked.append(f"{repo['name']}: already has an open lab pull request")
        elif repo["private"]:
            blocked.append(f"{repo['name']}: private (the work job clones without a token)")
        elif cfg.require_clean_history and repo["history_offenders"]:
            who = ", ".join(sorted(repo["history_offenders"]))
            blocked.append(f"{repo['name']}: history has other contributors ({who}); run reattribute-history")
        else:
            eligible.append(repo)
    if not eligible:
        reason = "no eligible repository"
        if any("reattribute-history" in b for b in blocked):
            reason += ": history still lists other contributors, run the reattribute-history workflow once"
        return Selection("skip", reason, blocked=blocked)

    # 4. Rotation: least recently considered first; owner issues jump ahead.
    def rank(repo: Mapping[str, Any]) -> tuple[int, str]:
        last = considered.get(repo["name"])
        ordinal = last.toordinal() if last else 0
        if repo["owner_issues"]:
            ordinal -= cfg.issue_boost_days
        return ordinal, repo["name"].lower()

    pick = min(eligible, key=rank)
    last = considered.get(pick["name"])
    reason = f"rotation: last considered {last.isoformat() if last else 'never'}"
    if pick["owner_issues"]:
        reason += f", {len(pick['owner_issues'])} open owner issue(s)"
    return Selection("improve", reason, pick, blocked=blocked)


# --------------------------------------------------------------------------
# context for the model
# --------------------------------------------------------------------------

def improve_context(repo: Mapping[str, Any]) -> str:
    out = [
        f"Repository: {repo['full']} ({repo['language'] or 'unknown language'})",
        f"Description: {repo['description'] or '(none)'}",
        "",
        "### Open issues filed by the owner",
    ]
    if repo["owner_issues"]:
        out.append("These are requests from the owner. When one is actionable it outranks everything else.")
        for issue in repo["owner_issues"][:5]:
            out += ["", f"#{issue['number']} {issue['title']}", quote_block(issue["body"] or "(no description)")]
    else:
        out.append("None.")
    out += ["", "### Lab pull requests the owner merged (already done)"]
    out += [f"- #{p['number']} {p['title']}" for p in repo["merged_prs"]] or ["None yet."]
    out += ["", "### Lab pull requests the owner closed without merging (rejected; do not re-propose)"]
    out += [f"- #{p['number']} {p['title']}" for p in repo["rejected_prs"]] or ["None."]
    return clip("\n".join(out), MAX_CONTEXT_CHARS)


def revise_context(repo: Mapping[str, Any], pr: Mapping[str, Any], kind: str, logs: str) -> str:
    out = [
        f"Pull request: {pr['url']}",
        f"Branch: {pr['branch']}",
        "",
        "### Its current description",
        quote_block(clip(pr["body"] or "(empty)", 3000)),
        "",
    ]
    if kind == "feedback":
        out.append(f"### Owner feedback to address ({len(pr['feedback'])} item(s), oldest first)")
        for n, item in enumerate(pr["feedback"], 1):
            if item["kind"] == "inline":
                where = f"on `{item['path']}`" + (f" line {item['line']}" if item.get("line") else "")
                out += ["", f"{n}. Inline review comment {where}:", quote_block(item["body"])]
                if item.get("diff_hunk"):
                    out += ["", "   The lines it refers to:", fence(item["diff_hunk"], "diff")]
            else:
                label = "Review" if item["kind"] == "review" else "Comment"
                out += ["", f"{n}. {label}:", quote_block(item["body"])]
    if pr["failing_checks"]:
        out += ["", "### Failing checks on the branch head"]
        out += [f"- {c['workflow'] + ' / ' if c['workflow'] else ''}{c['name']}" for c in pr["failing_checks"]]
        if kind == "ci":
            out += ["", f"This is fix attempt {pr['ci_attempts'] + 1}."]
            if logs:
                out += ["", logs]
    return clip("\n".join(out), MAX_CONTEXT_CHARS)


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------

def plan_outputs(sel: Selection, ident: Identity) -> dict[str, str]:
    repo, pr = sel.repo or {}, sel.pr or {}
    issues = " ".join(str(i["number"]) for i in repo.get("owner_issues", []))
    return {
        "action": sel.action,
        "reason": one_line(sel.reason, 300),
        "owner": ident.login,
        "git_name": ident.name,
        "git_email": ident.email,
        "can_edit_workflows": ident.can_edit_workflows,
        "repo": repo.get("full", ""),
        "repo_name": repo.get("name", ""),
        "default_branch": repo.get("default_branch", ""),
        "owner_issues": issues,
        "pr_number": str(pr.get("number", "")),
        "pr_branch": pr.get("branch", ""),
        "pr_head": pr.get("head_oid", ""),
        "pr_title": one_line(pr.get("title", ""), 200),
        "pr_url": pr.get("url", ""),
        "revise_kind": sel.revise_kind,
    }


def summary_markdown(repos: list[dict[str, Any]], sel: Selection, considered: Mapping[str, dt.date],
                     ident: Identity) -> str:
    rows = ["| repository | open lab PR | last considered | other contributors | owner issues |",
            "| --- | --- | --- | --- | --- |"]
    for r in repos:
        prs = ", ".join(f"[#{p['number']}]({p['url']})" for p in r["open_prs"]) or "-"
        last = considered.get(r["name"])
        who = ", ".join(f"{k} ×{v}" for k, v in sorted(r["history_offenders"].items())) or "none"
        if r["errors"]:
            who = "unknown (read error)"
        rows.append(f"| {r['name']} | {prs} | {last.isoformat() if last else 'never'} | "
                    f"{who.replace('|', '/')} | {len(r['owner_issues'])} |")
    target = f" → **{sel.repo['name']}**" if sel.repo else ""
    if sel.pr:
        target += f" [#{sel.pr['number']}]({sel.pr['url']})"
    lines = [
        "## plan",
        "",
        f"**{sel.action}**{target} — {sel.reason}",
        "",
        f"Commits are authored as `{ident.name} <{ident.email}>`. "
        f"Token can edit workflow files: {ident.can_edit_workflows}.",
        "",
        *rows,
    ]
    if sel.blocked:
        lines += ["", "<details><summary>Not eligible today</summary>", ""]
        lines += [f"- {b}" for b in sel.blocked]
        lines += ["", "</details>"]
    return "\n".join(lines) + "\n"


def write_outputs(path: str | None, outputs: Mapping[str, str]) -> None:
    lines = []
    for key, value in outputs.items():
        value = str(value)
        if "\n" in value or "\r" in value:
            raise ValueError(f"output {key} must be a single line")
        lines.append(f"{key}={value}\n")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.writelines(lines)
    else:
        sys.stdout.writelines(lines)


def cmd_plan(args: argparse.Namespace, gh: Gh | None = None, env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    gh = gh or Gh()
    cfg = Config.from_env(env)
    ident = resolve_identity(gh, cfg, fine_grained=(env.get("GH_TOKEN") or "").startswith("github_pat_"))
    repos = [gather_repo(gh, r, ident, cfg) for r in list_portfolio(gh, ident.login, cfg.topic)]
    if not repos:
        raise SystemExit(f"no repositories owned by {ident.login} carry the topic {cfg.topic!r}")
    runs = Path(args.runs)
    considered = last_considered(runs.read_text(encoding="utf-8")) if runs.exists() else {}
    sel = choose(repos, cfg, considered)

    context = ""
    if sel.action == "improve" and sel.repo:
        context = improve_context(sel.repo)
    elif sel.action == "revise" and sel.repo and sel.pr:
        logs = failed_logs(gh, sel.repo["full"], sel.pr["failing_checks"]) if sel.revise_kind == "ci" else ""
        context = revise_context(sel.repo, sel.pr, sel.revise_kind, logs)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "context.md").write_text(context, encoding="utf-8")
    state = {"identity": {"login": ident.login, "name": ident.name, "email": ident.email,
                          "can_edit_workflows": ident.can_edit_workflows},
             "selection": {"action": sel.action, "reason": sel.reason, "revise_kind": sel.revise_kind,
                           "repo": (sel.repo or {}).get("full"), "pr": (sel.pr or {}).get("number"),
                           "blocked": sel.blocked},
             "repos": repos}
    (out / "state.json").write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")

    write_outputs(args.github_output, plan_outputs(sel, ident))
    summary = summary_markdown(repos, sel, considered, ident)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as handle:
            handle.write(summary)
    print(summary)
    return 0


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------

def render(template: str, values: Mapping[str, str]) -> str:
    """Replace ${NAME} placeholders in one pass; a missing value is an error.

    Values are inserted verbatim and never re-scanned, so text that came from
    GitHub cannot smuggle in a placeholder of its own.
    """
    missing = sorted({m.group(1) for m in PLACEHOLDER_RE.finditer(template)} - set(values))
    if missing:
        raise KeyError(f"no value for placeholder(s): {', '.join(missing)}")
    return PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], template)


def cmd_render(args: argparse.Namespace, env: Mapping[str, str] | None = None) -> int:
    # Values arrive by environment-variable name rather than on the command
    # line: model-written text such as a plan can start with "-", which an
    # argument parser would take for an option.
    env = os.environ if env is None else env
    values: dict[str, str] = {}
    for key in args.env or []:
        if key not in env:
            raise KeyError(f"environment variable {key} is not set")
        values[key] = env[key]
    for key, path in args.file or []:
        values[key] = Path(path).read_text(encoding="utf-8").strip() or "(none)"
    text = render(Path(args.template).read_text(encoding="utf-8"), values)
    Path(args.output).write_text(text, encoding="utf-8")
    return 0


# --------------------------------------------------------------------------
# sanitize
# --------------------------------------------------------------------------

CREDIT_LINE_RE = re.compile(r"^\s*(co-authored-by|signed-off-by)\s*:", re.IGNORECASE)
FENCE_RE = re.compile(r"^\s*(```|~~~)")
INLINE_CODE_RE = re.compile(r"(`+[^`]*`+)")
MENTION_RE = re.compile(r"(?<![\w`/.@-])@([A-Za-z0-9][A-Za-z0-9-]{0,38}(?:/[A-Za-z0-9._-]+)?)")
CLOSING_RE = re.compile(
    r"\b(close[sd]?|fix(?:e[sd])?|resolve[sd]?)(\s*:?\s*)((?:[\w.-]+/[\w.-]+)?#\d+)", re.IGNORECASE)


def _defuse(text: str) -> str:
    parts = INLINE_CODE_RE.split(text)
    for i in range(0, len(parts), 2):  # even indexes are outside inline code
        parts[i] = MENTION_RE.sub(lambda m: f"`@{m.group(1)}`", parts[i])
        parts[i] = CLOSING_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}`{m.group(3)}`", parts[i])
    return "".join(parts)


def sanitize_markdown(text: str) -> str:
    """Make model-written text safe to post under the owner's account.

    - Drops Co-authored-by and Signed-off-by lines: a squash merge would
      turn them into credit for someone else.
    - Wraps @mentions in code: the lab must never notify strangers.
    - Defuses closing keywords ("fixes #3") so a merge cannot close an
      arbitrary issue; the workflow adds its own "Closes #N" when it should.
    Code blocks and inline code are otherwise left as they are.
    """
    out, in_fence = [], False
    for line in text.splitlines():
        if CREDIT_LINE_RE.match(line):
            continue
        if FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
        else:
            out.append(line if in_fence else _defuse(line))
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def cmd_sanitize(_args: argparse.Namespace) -> int:
    sys.stdout.write(sanitize_markdown(sys.stdin.read()))
    return 0


# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("plan", help="choose today's action")
    p.add_argument("--runs", default="RUNS.md", help="the run ledger (default: RUNS.md)")
    p.add_argument("--out", required=True, help="directory for context.md and state.json")
    p.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    p.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"))
    p.set_defaults(func=cmd_plan)

    r = sub.add_parser("render", help="fill a prompt template")
    r.add_argument("template")
    r.add_argument("output")
    r.add_argument("--env", action="append", metavar="NAME",
                   help="fill ${NAME} from the environment variable of the same name")
    r.add_argument("--file", nargs=2, action="append", metavar=("NAME", "PATH"),
                   help="fill ${NAME} with the contents of PATH")
    r.set_defaults(func=cmd_render)

    s = sub.add_parser("sanitize", help="make model-written Markdown (stdin) safe to post (stdout)")
    s.set_defaults(func=cmd_sanitize)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
