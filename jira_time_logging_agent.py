#!/usr/bin/env python3
"""Build a Jira monthly worklog preview and submit it only after explicit approval."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time as time_module
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

API_TIMEOUT_SECONDS = 30
PAGE_SIZE = 100
HOURS_PER_DAY = 8.0
WORKLOG_DESCRIPTIONS = {
    "created_bug": "Bug investigation and testing.",
    "closed_bug": "Bug retesting and closure verification.",
    "assigned_story": "Story validation and associated testing.",
    "assigned_subtask": "Sub-task implementation and validation.",
    "assigned_substory": "Sub-story implementation and validation.",
    "assigned_task": "Task implementation and validation.",
}


class JiraApiError(RuntimeError):
    """A Jira API request failed with an HTTP error."""


def require_weekday(work_day: date) -> None:
    if work_day.weekday() >= 5:
        raise ValueError(
            f"Worklogs may only be recorded Monday through Friday; {work_day.isoformat()} is a weekend."
        )


class ManualJiraBrowser:
    def __init__(self, base_url: str, project_key: Optional[str]) -> None:
        self.base_url = base_url.rstrip("/")
        self.project_key = project_key
        self._playwright = None
        self.browser = None
        self.context = None
        self.page = None

    def __enter__(self) -> "ManualJiraBrowser":
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        try:
            self.browser = self._playwright.chromium.launch(channel="chrome", headless=False)
        except PlaywrightError as exc:
            self._playwright.stop()
            self._playwright = None
            raise RuntimeError(
                "Could not launch Google Chrome. Install Chrome, then run this command again."
            ) from exc
        # A non-persistent context has no Chrome profile, saved cookies, or storage state.
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        try:
            self.page.goto(self.base_url, wait_until="domcontentloaded")
        except PlaywrightError as exc:
            self.__exit__(None, None, None)
            raise RuntimeError("Could not open the Jira site in Chrome.") from exc
        print(
            "A temporary browser session is open with no saved Jira session. Sign in to Jira "
            "manually; this run will continue after sign-in and will not save the session."
        )
        deadline = time_module.monotonic() + 15 * 60
        result: Dict[str, Any] = {}
        while time_module.monotonic() < deadline:
            try:
                result = self.page.evaluate(
                    """async () => {
                        const response = await fetch("/rest/api/3/myself", {
                            credentials: "include"
                        });
                        return {
                            status: response.status,
                            user: response.ok ? await response.json() : null
                        };
                    }"""
                )
                if result.get("status") == 200 and (result.get("user") or {}).get("accountId"):
                    break
            except PlaywrightError as exc:
                if self.page.is_closed():
                    self.__exit__(None, None, None)
                    raise RuntimeError(
                        "The manual Jira login browser was closed before verification; "
                        "no preview or worklogs were created."
                    ) from exc
            try:
                self.page.wait_for_timeout(2000)
            except PlaywrightError as exc:
                self.__exit__(None, None, None)
                raise RuntimeError(
                    "The manual Jira login browser was closed before verification; "
                    "no preview or worklogs were created."
                ) from exc
        else:
            self.__exit__(None, None, None)
            raise RuntimeError(
                "Jira sign-in was not verified within 15 minutes; no preview or worklogs were created."
            )
        self.user = result["user"]
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        try:
            if self.context is not None:
                self.context.close()
        finally:
            try:
                if self.browser is not None:
                    self.browser.close()
            finally:
                if self._playwright is not None:
                    self._playwright.stop()
                self.context = None
                self.browser = None
                self.page = None
                self._playwright = None

    def sync_cookies(self, session: requests.Session) -> None:
        if self.context is None:
            raise RuntimeError("Manual Jira browser session is not active.")
        session.headers["X-Atlassian-Token"] = "no-check"
        for cookie in self.context.cookies([self.base_url]):
            session.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain"),
                path=cookie.get("path", "/"),
                secure=cookie.get("secure", False),
            )

    def create_worklog(
        self,
        issue_key: str,
        started: str,
        seconds: int,
        description: str,
        remaining_seconds: int,
    ) -> None:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        if self.page is None:
            raise RuntimeError("Manual Jira browser session is not active.")
        work_day = date.fromisoformat(started[:10])
        require_weekday(work_day)
        project = f"?project.key={self.project_key}" if self.project_key else ""
        self.page.goto(
            f"{self.base_url}/plugins/servlet/ac/clockwork-free-cloud/clockwork-mywork{project}",
            wait_until="domcontentloaded",
        )
        try:
            self.page.wait_for_function(
                "() => Array.from(document.querySelectorAll('iframe')).some(frame => "
                "frame.src.includes('app.clockwork.report/panels/my_worklogs'))",
                timeout=30000,
            )
            frame = next(
                frame
                for frame in self.page.frames
                if "app.clockwork.report/panels/my_worklogs" in frame.url
            )
            frame.get_by_role("button", name="Add Worklog").click()
            dialog = frame.get_by_role("dialog", name="Log Work")
            issue_input = dialog.get_by_role("textbox", name="Issue")
            issue_input.fill(issue_key)
            loading = dialog.get_by_text("Loading...", exact=True)
            loading.wait_for(state="hidden", timeout=30000)
            issue_input.press("ArrowDown")
            issue_input.press("Enter")
            dialog.get_by_text(issue_key, exact=False).wait_for(timeout=10000)
            selected_issue_text = dialog.inner_text()
            if issue_key not in selected_issue_text:
                raise RuntimeError(f"Clockwork did not select the requested issue {issue_key}.")

            dialog.get_by_role("textbox", name="Time Spent").fill(format_duration(seconds))
            dialog.get_by_role("textbox", name="Remaining Time").fill(
                format_duration(remaining_seconds)
            )
            date_input = dialog.locator('input[id^="react-select-started-"]')
            date_input.fill(work_day.strftime("%m-%d-%Y"))
            date_input.press("Enter")
            dialog.get_by_text(work_day.strftime("%d-%m-%Y"), exact=True).wait_for(
                timeout=10000
            )
            dialog.get_by_text("You will have", exact=False).wait_for(timeout=10000)
            description_input = dialog.get_by_role("textbox", name=re.compile("Description", re.I))
            description_input.fill(description)

            dialog_text = dialog.inner_text()
            if work_day.strftime("%d-%m-%Y") not in dialog_text:
                raise RuntimeError(
                    f"Clockwork did not confirm the requested work date {work_day.isoformat()}."
                )
            total_match = re.search(
                r"You will have\s+(\d+(?:\.\d+)?)h reported for that day\.",
                dialog_text,
            )
            if not total_match or float(total_match.group(1)) > HOURS_PER_DAY:
                raise RuntimeError(
                    f"Clockwork did not confirm a daily total within {HOURS_PER_DAY:g} hours "
                    f"for {work_day.isoformat()}; the entry was not saved."
                )
            dialog.get_by_role("button", name="Save").click()
            dialog.wait_for(state="hidden", timeout=20000)
            try:
                self.page.get_by_text("Worklog has been created", exact=True).wait_for(
                    timeout=5000
                )
            except PlaywrightTimeoutError:
                frame.get_by_text("Worklog has been created", exact=True).wait_for(
                    timeout=5000
                )
        except PlaywrightTimeoutError as exc:
            raise RuntimeError(
                f"Clockwork UI did not expose the expected controls for {issue_key}; "
                "the worklog was not confirmed."
            ) from exc
        except PlaywrightError as exc:
            raise RuntimeError(
                f"Clockwork UI interaction failed for {issue_key}; the worklog was not confirmed."
            ) from exc


def format_duration(seconds: int) -> str:
    if seconds < 0:
        raise ValueError("Remaining estimate cannot be negative.")
    if seconds % 60:
        raise ValueError("Clockwork estimates must be expressible in whole minutes.")
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    return " ".join(parts) or "0m"


@dataclass(frozen=True)
class Worklog:
    issue_key: str
    worklog_id: str
    day: date
    seconds: int
    author_id: str
    description: str

    @property
    def hours(self) -> float:
        return self.seconds / 3600.0


class JiraClient:
    def __init__(
        self,
        base_url: str,
        api_version: str,
        email: str = "",
        token: str = "",
        auth_type: str = "basic",
        session: Optional[requests.Session] = None,
        browser: Optional["ManualJiraBrowser"] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_version = api_version
        self.session = session or requests.Session()
        self.browser = browser
        self.session.headers.update({"Accept": "application/json"})
        if browser is not None:
            browser.sync_cookies(self.session)
        elif auth_type == "basic":
            self.session.auth = (email, token)
        else:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        if self.browser is not None:
            self.browser.sync_cookies(self.session)
        response = self.session.request(
            method,
            f"{self.base_url}{path}",
            timeout=API_TIMEOUT_SECONDS,
            **kwargs,
        )
        if response.status_code >= 400:
            raise JiraApiError(
                f"{method} {path} failed ({response.status_code}): {response.text[:500]}"
            )
        if not response.content:
            return {}
        return response.json()

    def api_path(self, suffix: str) -> str:
        return f"/rest/api/{self.api_version}{suffix}"

    def current_user(self) -> Dict[str, Any]:
        return self.request("GET", self.api_path("/myself"))

    def search_issues(self, jql: str, fields: Sequence[str]) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        start_at = 0
        next_page_token: Optional[str] = None
        while True:
            payload = {
                "jql": jql,
                "maxResults": PAGE_SIZE,
                "fields": list(fields),
            }
            endpoint = "/search"
            if self.api_version == "3":
                endpoint = "/search/jql"
                if next_page_token:
                    payload["nextPageToken"] = next_page_token
            else:
                payload["startAt"] = start_at

            page = self.request("POST", self.api_path(endpoint), json=payload)
            issues = page.get("issues", [])
            results.extend(issues)
            if self.api_version == "3":
                next_page_token = page.get("nextPageToken")
                if not next_page_token:
                    break
            else:
                total = page.get("total")
                if not issues or (total is not None and start_at + len(issues) >= int(total)):
                    break
                if len(issues) < PAGE_SIZE and total is None:
                    break
                start_at += len(issues)
        return results

    def issue_details(self, issue_key: str) -> Dict[str, Any]:
        return self.request(
            "GET",
            self.api_path(f"/issue/{issue_key}"),
            params={"fields": "summary,issuetype,status,created,creator,resolutiondate,assignee,timeestimate"},
        )

    def issue_changelog(self, issue_key: str) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        start_at = 0
        while True:
            page = self.request(
                "GET",
                self.api_path(f"/issue/{issue_key}/changelog"),
                params={"startAt": start_at, "maxResults": PAGE_SIZE},
            )
            histories = page.get("values", page.get("histories", []))
            results.extend(histories)
            total = page.get("total")
            if not histories or (total is not None and start_at + len(histories) >= int(total)):
                break
            start_at += len(histories)
        return results

    def issue_worklogs(self, issue_key: str) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        start_at = 0
        while True:
            page = self.request(
                "GET",
                self.api_path(f"/issue/{issue_key}/worklog"),
                params={"startAt": start_at, "maxResults": PAGE_SIZE},
            )
            worklogs = page.get("worklogs", [])
            results.extend(worklogs)
            total = page.get("total")
            if not worklogs or (total is not None and start_at + len(worklogs) >= int(total)):
                break
            start_at += len(worklogs)
        return results

    def create_worklog(self, issue_key: str, started: str, seconds: int, description: str) -> Dict[str, Any]:
        try:
            work_day = date.fromisoformat(started[:10])
        except ValueError as exc:
            raise ValueError(f"Invalid Jira worklog start date: {started!r}.") from exc
        require_weekday(work_day)
        if getattr(self, "browser", None) is not None:
            issue = self.issue_details(issue_key)
            remaining_seconds = (issue.get("fields") or {}).get("timeestimate") or 0
            self.browser.create_worklog(
                issue_key, started, seconds, description, int(remaining_seconds)
            )
            user = self.current_user()
            account_id = str(user.get("accountId") or user.get("key") or user.get("name") or "")
            timezone_name = str(user.get("timeZone") or user.get("timezone") or "UTC")
            try:
                tz = ZoneInfo(timezone_name)
            except ZoneInfoNotFoundError as exc:
                raise RuntimeError(
                    f"Cannot verify the UI-created worklog; Jira returned invalid timezone {timezone_name!r}."
                ) from exc
            verified = equivalent_worklog(
                issue_key,
                work_day,
                seconds,
                description,
                parse_worklogs(
                    issue_key, self.issue_worklogs(issue_key), account_id, work_day, work_day, tz
                ),
            )
            if verified is None:
                raise RuntimeError(
                    f"Clockwork reported a save for {issue_key} on {work_day}, but Jira read-back "
                    "verification failed."
                )
            return {"id": verified.worklog_id}
        comment: Any = description
        if self.api_version == "3":
            comment = {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": description}],
                    }
                ],
            }
        return self.request(
            "POST",
            self.api_path(f"/issue/{issue_key}/worklog"),
            params={"adjustEstimate": "leave"},
            json={
                "started": started,
                "timeSpentSeconds": seconds,
                "comment": comment,
            },
        )


def jira_datetime(value: Any, tz: ZoneInfo) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    if value.endswith("+0000"):
        normalized = value[:-5] + "+00:00"
    elif value.endswith("-0000"):
        normalized = value[:-5] + "-00:00"
    else:
        normalized = value
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def jira_date(value: Any, tz: ZoneInfo) -> Optional[date]:
    parsed = jira_datetime(value, tz)
    return parsed.date() if parsed else None


def month_range(today: Optional[date] = None) -> Tuple[date, date]:
    today = today or datetime.now().date()
    return date(today.year, today.month, 1), today


def weekdays(start: date, end: date) -> List[date]:
    result = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            result.append(current)
        current += timedelta(days=1)
    return result


def parse_worklogs(
    issue_key: str,
    raw_worklogs: Iterable[Dict[str, Any]],
    account_id: str,
    start: date,
    end: date,
    tz: ZoneInfo,
) -> List[Worklog]:
    result = []
    for raw in raw_worklogs:
        author = raw.get("author") or {}
        raw_author_id = str(author.get("accountId") or author.get("key") or author.get("name") or "")
        if raw_author_id != account_id:
            continue
        started = jira_datetime(raw.get("started"), tz)
        if started is None or not start <= started.date() <= end:
            continue
        result.append(
            Worklog(
                issue_key=issue_key,
                worklog_id=str(raw.get("id", "")),
                day=started.date(),
                seconds=int(raw.get("timeSpentSeconds") or 0),
                author_id=raw_author_id,
                description=comment_text(raw.get("comment")),
            )
        )
    return result


def comment_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        parts = []
        for item in value.get("content", []):
            if isinstance(item, dict):
                parts.append(comment_text(item))
        if value.get("text"):
            parts.append(str(value["text"]))
        return " ".join(part for part in parts if part)
    if isinstance(value, list):
        return " ".join(comment_text(item) for item in value if item)
    return ""


def same_description(left: str, right: str) -> bool:
    return " ".join(left.split()).casefold() == " ".join(right.split()).casefold()


def equivalent_worklog(
    issue_key: str,
    work_day: date,
    seconds: int,
    description: str,
    worklogs: Iterable[Worklog],
) -> Optional[Worklog]:
    for worklog in worklogs:
        if (
            worklog.issue_key == issue_key
            and worklog.day == work_day
            and worklog.seconds == seconds
            and same_description(worklog.description, description)
        ):
            return worklog
    return None


def worklog_for_issue_day(
    issue_key: str,
    work_day: date,
    worklogs: Iterable[Worklog],
) -> Optional[Worklog]:
    return next(
        (
            worklog
            for worklog in worklogs
            if worklog.issue_key == issue_key and worklog.day == work_day
        ),
        None,
    )


def normalized_issue_type(issue_type: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", issue_type.casefold())


def allocation_hours(issue_type: str) -> Optional[float]:
    normalized = normalized_issue_type(issue_type)
    if normalized == "bug":
        return 3.0
    if normalized in ("story", "subtask", "substory", "task"):
        return 36.0
    return None


def eligibility_reason(
    issue: Dict[str, Any],
    account_id: str,
    period_start: date,
    period_end: date,
    tz: ZoneInfo,
) -> Optional[str]:
    fields = issue.get("fields", {})
    issue_type = normalized_issue_type(str((fields.get("issuetype") or {}).get("name", "")))
    if issue_type == "bug":
        created = jira_date(fields.get("created"), tz)
        creator = fields.get("creator") or {}
        if (
            creator.get("accountId") == account_id
            and created is not None
            and period_start <= created <= period_end
        ):
            return "created_bug"
        closure_day = issue.get("_confirmed_closure_date")
        if (
            issue.get("_confirmed_actor") == account_id
            and isinstance(closure_day, date)
            and period_start <= closure_day <= period_end
        ):
            return "closed_bug"
        return None

    if (
        issue_type in ("story", "subtask", "substory", "task")
        and issue.get("_assignment_changed_to_account_in_period") is True
    ):
        return f"assigned_{issue_type}"
    return None


def has_assignment_history_in_period(
    histories: Sequence[Dict[str, Any]],
    period_start: date,
    period_end: date,
    account_id: str,
    display_name: str,
    tz: ZoneInfo,
) -> bool:
    for history in histories:
        changed_at = jira_date(history.get("created"), tz)
        if changed_at is None or not period_start <= changed_at <= period_end:
            continue
        for item in history.get("items", []):
            if str(item.get("field", "")).casefold() != "assignee":
                continue
            assigned_account = str(item.get("to") or "")
            assigned_name = str(item.get("toString") or "")
            if assigned_account == account_id or (
                not assigned_account and assigned_name == display_name
            ):
                return True
    return False


def closure_date_by_account(
    histories: Sequence[Dict[str, Any]],
    period_start: date,
    period_end: date,
    account_id: str,
    tz: ZoneInfo,
) -> Optional[date]:
    for history in histories:
        actor = history.get("author") or {}
        actor_id = str(actor.get("accountId") or actor.get("key") or actor.get("name") or "")
        changed_at = jira_date(history.get("created"), tz)
        if (
            actor_id != account_id
            or changed_at is None
            or not period_start <= changed_at <= period_end
        ):
            continue
        if any(
            str(item.get("field", "")).casefold() == "status"
            and str(item.get("toString", "")).casefold() == "closed"
            for item in history.get("items", [])
        ):
            return changed_at
    return None


def schedule_proposals(
    issues: Sequence[Dict[str, Any]],
    all_worklogs: Sequence[Worklog],
    business_days: Sequence[date],
    period_start: date,
    period_end: date,
    account_id: str,
    timezone_name: str,
) -> Tuple[List[Dict[str, Any]], List[str], Dict[str, int]]:
    existing_by_day: Dict[date, float] = defaultdict(float)
    existing_by_issue: Dict[str, float] = defaultdict(float)
    for entry in all_worklogs:
        existing_by_day[entry.day] += entry.hours
        existing_by_issue[entry.issue_key] += entry.hours

    proposals: List[Dict[str, Any]] = []
    exceptions: List[str] = []
    counts = {"eligible_bugs": 0, "eligible_assigned_issues": 0}
    remaining = {day: max(0.0, HOURS_PER_DAY - existing_by_day[day]) for day in business_days}
    for day, hours in existing_by_day.items():
        if hours > HOURS_PER_DAY:
            exceptions.append(
                f"{day.isoformat()}: existing account worklogs total {hours:g} hours, already exceeding the 8-hour daily maximum."
            )
    candidates: List[Tuple[str, Dict[str, Any], Optional[date]]] = []

    for issue in issues:
        fields = issue.get("fields", {})
        key = str(issue.get("key", ""))
        issue_type = str((fields.get("issuetype") or {}).get("name", ""))
        total_hours = allocation_hours(issue_type)
        if total_hours is None:
            exceptions.append(
                f"{key}: excluded; only Bug, Task, Story, Sub-task, and Sub-story issues are included in this policy."
            )
            continue

        kind = eligibility_reason(
            issue, account_id, period_start, period_end, ZoneInfo(timezone_name)
        )
        if kind is None:
            exceptions.append(
                f"{key}: excluded; a Bug must have been created by or explicitly closed by "
                "the account this month; a Task, Story, Sub-task, or Sub-story must have "
                "been assigned to the account this month."
            )
            continue

        trigger_day = (
            issue.get("_confirmed_closure_date")
            if kind == "closed_bug"
            else jira_date(fields.get("created"), ZoneInfo(timezone_name))
            if kind == "created_bug"
            else None
        )
        candidates.append((kind, issue, trigger_day))
        counts["eligible_bugs" if kind.endswith("_bug") else "eligible_assigned_issues"] += 1
        if existing_by_issue[key] > total_hours:
            exceptions.append(
                f"{key}: already has {existing_by_issue[key]:g} hours logged this month, "
                f"exceeding its {total_hours:g}-hour allocation; no additional time will be proposed."
            )

    candidates.sort(
        key=lambda row: (
            row[2] is None,
            row[2] or period_start,
            row[1].get("key", ""),
        )
    )
    proposed_issue_days = set()
    for kind, issue, preferred_day in candidates:
        fields = issue.get("fields", {})
        key = str(issue.get("key", ""))
        summary = str(fields.get("summary") or key)
        issue_type = str((fields.get("issuetype") or {}).get("name", ""))
        issue_status = str((fields.get("status") or {}).get("name", "Unknown"))
        description = WORKLOG_DESCRIPTIONS[kind]
        total_hours = allocation_hours(issue_type)
        if total_hours is None:
            continue

        if preferred_day is not None:
            candidate_days = sorted(
                business_days,
                key=lambda day: (
                    abs((day - preferred_day).days),
                    day > preferred_day,
                    -day.toordinal(),
                ),
            )
        else:
            candidate_days = sorted(
                business_days,
                key=lambda day: (
                    existing_by_day[day] + HOURS_PER_DAY - remaining[day],
                    day,
                ),
            )

        hours_left = max(0.0, total_hours - existing_by_issue[key])
        for selected_day in candidate_days:
            if hours_left <= 0:
                break
            if (
                remaining.get(selected_day, 0.0) <= 0
                or (key, selected_day) in proposed_issue_days
                or worklog_for_issue_day(key, selected_day, all_worklogs) is not None
            ):
                continue

            duration_hours = min(hours_left, remaining[selected_day])
            if duration_hours <= 0:
                continue
            date_changed = preferred_day is not None and selected_day != preferred_day
            proposals.append(
                make_proposal(
                    key,
                    issue_type,
                    summary,
                    kind,
                    preferred_day or selected_day,
                    selected_day,
                    duration_hours,
                    description,
                    issue_status,
                    date_changed,
                )
            )
            remaining[selected_day] -= duration_hours
            proposed_issue_days.add((key, selected_day))
            hours_left -= duration_hours

        if hours_left > 0:
            exceptions.append(
                f"{key}: {hours_left:g} of {total_hours:g} hours could not be scheduled "
                "without exceeding 8 hours per day or repeating this issue on the same date."
            )

        if kind.startswith("assigned_"):
            exceptions.append(
                f"{key}: assignment changed to the account during the period, but the Jira history "
                "does not verify actual work dates. Confirm each proposed date before submission."
            )
    return proposals, exceptions, counts


def make_proposal(
    issue_key: str,
    issue_type: str,
    summary: str,
    kind: str,
    trigger_day: date,
    work_day: date,
    hours: float,
    description: str,
    issue_status: str,
    date_changed: bool,
) -> Dict[str, Any]:
    return {
        "issue_key": issue_key,
        "issue_type": issue_type,
        "issue_status": issue_status,
        "summary": summary,
        "reason": kind.replace("_", " "),
        "trigger_date": trigger_day.isoformat(),
        "worklog_date": work_day.isoformat(),
        "hours": hours,
        "seconds": int(hours * 3600),
        "description": description,
        "description_confirmed": False,
        "activity_confirmed": False,
        "date_changed_from_preferred": date_changed,
        "approval_status": "pending",
        "worklog_id": None,
        "execution_result": "not_submitted",
    }


def daily_report(
    business_days: Sequence[date],
    worklogs: Sequence[Worklog],
    proposals: Sequence[Dict[str, Any]],
    exceptions: Sequence[str],
) -> List[Dict[str, Any]]:
    existing: Dict[date, float] = defaultdict(float)
    proposed: Dict[date, float] = defaultdict(float)
    for item in worklogs:
        existing[item.day] += item.hours
    for item in proposals:
        proposed[date.fromisoformat(item["worklog_date"])] += float(item["hours"])
    result = []
    for day in business_days:
        old = existing[day]
        pending = proposed[day]
        total = old + pending
        if old > HOURS_PER_DAY or total > HOURS_PER_DAY:
            status = "Blocked"
        elif pending > 0:
            status = "Proposed"
        elif old >= HOURS_PER_DAY:
            status = "Fully logged"
        elif old > 0:
            status = "Partially logged"
        else:
            status = "No confirmed work"
        result.append(
            {
                "date": day.isoformat(),
                "existing_hours": round(old, 2),
                "proposed_hours": round(pending, 2),
                "final_hours": round(total, 2),
                "remaining_hours_to_8": round(max(0.0, HOURS_PER_DAY - total), 2),
                "status": status,
            }
        )
    return result


def report_csv_rows(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for day in report["daily_reconciliation"]:
        rows.append(
            {
                "record_type": "daily",
                "date": day["date"],
                "issue_key": "",
                "issue_type": "",
                "issue_status": "",
                "summary": "",
                "trigger_date": "",
                "hours": "",
                "existing_hours": day["existing_hours"],
                "proposed_hours": day["proposed_hours"],
                "final_hours": day["final_hours"],
                "status": day["status"],
                "description": "",
                "activity_confirmed": "",
                "approval_status": "",
                "exception": "",
            }
        )
    for item in report["proposed_worklogs"]:
        rows.append(
            {
                "record_type": "proposed_worklog",
                "date": item["worklog_date"],
                "issue_key": item["issue_key"],
                "issue_type": item["issue_type"],
                "issue_status": item["issue_status"],
                "summary": item["summary"],
                "trigger_date": item["trigger_date"],
                "hours": item["hours"],
                "existing_hours": "",
                "proposed_hours": "",
                "final_hours": "",
                "status": item["execution_result"],
                "description": item["description"],
                "activity_confirmed": item["activity_confirmed"],
                "approval_status": item["approval_status"],
                "exception": "",
            }
        )
    for exception in report["exceptions"]:
        rows.append(
            {
                "record_type": "exception",
                "date": "",
                "issue_key": "",
                "issue_type": "",
                "issue_status": "",
                "summary": "",
                "trigger_date": "",
                "hours": "",
                "existing_hours": "",
                "proposed_hours": "",
                "final_hours": "",
                "status": "Needs review",
                "description": "",
                "activity_confirmed": "",
                "approval_status": "",
                "exception": exception,
            }
        )
    return rows


def write_report(report: Dict[str, Any], output: Path) -> Path:
    if output.suffix.casefold() != ".json":
        raise ValueError("Preview output must use a .json extension so the companion CSV is written separately.")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    csv_path = output.with_suffix(".csv")
    rows = report_csv_rows(report)
    headers = [
        "record_type", "date", "issue_key", "issue_type", "issue_status", "summary",
        "trigger_date", "hours",
        "existing_hours", "proposed_hours", "final_hours", "status",
        "description", "activity_confirmed", "approval_status", "exception",
    ]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)
    return csv_path


def build_jql(start: date, end: date, project_key: Optional[str]) -> str:
    tomorrow = end + timedelta(days=1)
    escaped_project = project_key.replace("\\", "\\\\").replace('"', '\\"') if project_key else ""
    scope = f' AND project = "{escaped_project}"' if project_key else ""
    return (
        f'(issuetype in ("Bug", "Task", "Story", "Sub-task", "Sub-story") '
        f'OR issuetype in subTaskIssueTypes()){scope} AND ('
        f'(issuetype = "Bug" AND creator = currentUser() AND created >= "{start.isoformat()}" '
        f'AND created < "{tomorrow.isoformat()}") OR '
        f'(issuetype = "Bug" AND status CHANGED TO Closed BY currentUser() DURING '
        f'("{start.isoformat()}", "{tomorrow.isoformat()}")) OR '
        f'((issuetype in ("Task", "Story", "Sub-task", "Sub-story") '
        f'OR issuetype in subTaskIssueTypes()) AND '
        f'assignee WAS currentUser() DURING ("{start.isoformat()}", "{tomorrow.isoformat()}")))'
    )


def load_jira_data(
    client: JiraClient,
    start: date,
    end: date,
    project_key: Optional[str],
    user: Dict[str, Any],
    tz: ZoneInfo,
) -> Tuple[List[Dict[str, Any]], List[Worklog], List[str]]:
    account_id = str(user.get("accountId") or user.get("key") or user.get("name") or "")
    if not account_id:
        raise ValueError("Jira did not return an account identifier for the authenticated user.")
    raw_issues = client.search_issues(
        build_jql(start, end, project_key),
        ["summary", "issuetype", "status", "created", "creator", "resolutiondate", "assignee"],
    )
    issue_map = {str(item["key"]): item for item in raw_issues if item.get("key")}
    detailed_issues = []
    exceptions = []
    display_name = str(user.get("displayName") or user.get("name") or "")
    for key in issue_map:
        issue = client.issue_details(key)
        fields = issue.get("fields", {})
        kind = str((fields.get("issuetype") or {}).get("name", ""))
        if allocation_hours(kind) is None:
            continue
        histories = client.issue_changelog(key)
        issue["_assignment_changed_to_account_in_period"] = has_assignment_history_in_period(
            histories, start, end, account_id, display_name, tz
        )
        closure_day = closure_date_by_account(histories, start, end, account_id, tz)
        if closure_day is not None:
            issue["_confirmed_actor"] = account_id
            issue["_confirmed_closure_date"] = closure_day
        if eligibility_reason(issue, account_id, start, end, tz) is None:
            continue
        detailed_issues.append(issue)
    all_worklogs = fetch_all_period_worklogs(client, start, end, account_id, tz)
    return detailed_issues, all_worklogs, exceptions


def make_preview(
    args: argparse.Namespace,
    client: Optional[JiraClient] = None,
) -> Dict[str, Any]:
    if client is None:
        raise ValueError("Preview requires a fresh manually authenticated Jira browser session.")
    try:
        tz = ZoneInfo(args.timezone) if args.timezone else None
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {args.timezone}") from exc
    user = client.current_user()
    profile_timezone = user.get("timeZone") or user.get("timezone")
    timezone_assumed = not args.timezone and not profile_timezone
    timezone_name = args.timezone or profile_timezone or "UTC"
    if tz is None:
        try:
            tz = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Invalid timezone from Jira profile: {timezone_name}") from exc
    start, end = month_range(datetime.now(tz).date())
    days = weekdays(start, end)
    issues, worklogs, load_exceptions = load_jira_data(
        client, start, end, args.project_key, user, tz
    )
    existing_issue_seconds: Dict[str, int] = defaultdict(int)
    for worklog in worklogs:
        existing_issue_seconds[worklog.issue_key] += worklog.seconds
    account_id = str(user.get("accountId") or user.get("key") or user.get("name") or "")
    proposals, schedule_exceptions, counts = schedule_proposals(
        issues, worklogs, days, start, end, account_id, timezone_name
    )
    exceptions = load_exceptions + schedule_exceptions
    if timezone_assumed:
        exceptions.insert(
            0,
            "Jira profile did not provide a timezone; UTC was used. Confirm UTC or rerun with the correct --timezone.",
        )
    for entry in proposals:
        if entry["date_changed_from_preferred"]:
            exceptions.append(
                f"{entry['issue_key']} on {entry['worklog_date']}: alternative date proposed because the preferred weekday was unsuitable; confirm before approval."
            )
    daily = daily_report(days, worklogs, proposals, exceptions)
    for entry in daily:
        if entry["remaining_hours_to_8"] > 0:
            exceptions.append(
                f"{entry['date']}: {entry['remaining_hours_to_8']:g} hours remain unlogged; confirm whether this is leave, holiday, meetings, non-Jira work, or other activity."
            )
    daily = daily_report(days, worklogs, proposals, exceptions)
    period_days = (end - start).days + 1
    weekends = sum(1 for offset in range(period_days) if (start + timedelta(days=offset)).weekday() >= 5)
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "jira_url": client.base_url,
        "api_version": args.api_version,
        "jira_account": {
            "account_id": account_id,
            "display_name": user.get("displayName") or user.get("name") or account_id,
        },
        "timezone": timezone_name,
        "reporting_period": {"start": start.isoformat(), "end": end.isoformat()},
        "totals": {
            "issues_reviewed": len(issues),
            "eligible_bugs": counts["eligible_bugs"],
            "eligible_tasks_stories_and_subtasks": counts["eligible_assigned_issues"],
            "existing_hours": round(sum(item.hours for item in worklogs), 2),
            "proposed_hours": round(sum(float(item["hours"]) for item in proposals), 2),
            "hours_approved": 0.0,
            "hours_successfully_logged": 0.0,
            "hours_requiring_clarification": round(
                sum(item["remaining_hours_to_8"] for item in daily), 2
            ),
            "total_proposed_worklogs": len(proposals),
            "excluded_weekend_dates": weekends,
            "weekdays_fully_reconciled": sum(
                1 for item in daily if item["final_hours"] == HOURS_PER_DAY
            ),
            "weekdays_with_gaps": sum(
                1 for item in daily if item["remaining_hours_to_8"] > 0
            ),
        },
        "daily_reconciliation": daily,
        "existing_issue_seconds": {
            str(issue["key"]): existing_issue_seconds[str(issue["key"])]
            for issue in issues
        },
        "proposed_worklogs": proposals,
        "exceptions": exceptions,
        "exception_resolutions": [
            {"exception": item, "resolution": ""} for item in exceptions
        ],
        "approval": {
            "required": True,
            "status": "pending",
            "instructions": (
                "Review each description and date. Set description_confirmed=true and "
                "activity_confirmed=true only after confirming the described work occurred "
                "on that date, and resolve every exception before submitting with --submit. "
                "The script will require typing the displayed approval token."
            ),
        },
    }


def canonical_manifest_hash(manifest: Dict[str, Any]) -> str:
    content = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def local_started(day: date, tz: ZoneInfo) -> str:
    require_weekday(day)
    started = datetime.combine(day, time(hour=9), tzinfo=tz)
    return started.strftime("%Y-%m-%dT%H:%M:%S.000%z")


def current_worklogs_for_issue(
    client: JiraClient,
    issue_key: str,
    account_id: str,
    start: date,
    end: date,
    tz: ZoneInfo,
) -> List[Worklog]:
    return parse_worklogs(
        issue_key, client.issue_worklogs(issue_key), account_id, start, end, tz
    )


def submit_manifest(args: argparse.Namespace, client: Optional[JiraClient] = None) -> int:
    manifest_path = Path(args.submit)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or manifest.get("approval", {}).get("status") not in ("pending", "partial", "approved"):
        raise ValueError("This is not a pending or resumable preview manifest supported by this agent.")
    entries = manifest.get("proposed_worklogs", [])
    if not entries:
        print("The preview contains no proposed worklogs; nothing to submit.")
        return 0
    start = date.fromisoformat(manifest["reporting_period"]["start"])
    end = date.fromisoformat(manifest["reporting_period"]["end"])
    if any(not entry.get("description_confirmed") for entry in entries):
        raise ValueError("Confirm every proposed description by setting description_confirmed=true in the preview JSON.")
    if any(not entry.get("activity_confirmed") for entry in entries):
        raise ValueError(
            "Confirm each proposed activity and date by setting activity_confirmed=true "
            "only after verifying the work occurred."
        )
    issue_days = set()
    allocated_seconds: Dict[str, int] = defaultdict(int)
    for entry in entries:
        key = str(entry.get("issue_key", ""))
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*-\d+", key):
            raise ValueError(f"Invalid Jira issue key in preview: {key!r}.")
        description = str(entry.get("description", "")).strip()
        if not description:
            raise ValueError(f"{key} has an empty description; no worklogs were submitted.")
        seconds = int(entry.get("seconds", 0))
        if seconds <= 0 or seconds > int(HOURS_PER_DAY * 3600) or float(entry.get("hours", 0)) != seconds / 3600.0:
            raise ValueError(f"{key} has an unsupported or inconsistent duration.")
        reason_parts = str(entry.get("reason", "")).split(maxsplit=1)
        issue_type = str(entry.get("issue_type", ""))
        total_allocation = allocation_hours(issue_type)
        normalized_type = normalized_issue_type(issue_type)
        allowed_reasons = ("created", "closed") if normalized_type == "bug" else ("assigned",)
        if (
            len(reason_parts) != 2
            or reason_parts[0] not in allowed_reasons
            or normalized_issue_type(reason_parts[1]) != normalized_issue_type(issue_type)
            or total_allocation is None
        ):
            raise ValueError(f"{key} issue type does not match its proposed allocation type.")
        work_day = date.fromisoformat(entry.get("worklog_date", ""))
        require_weekday(work_day)
        if not start <= work_day <= end:
            raise ValueError(f"Invalid proposed worklog date for {key}: {work_day}.")
        issue_day = (key, entry.get("worklog_date"))
        if issue_day in issue_days:
            raise ValueError(f"Preview contains a repeated worklog entry for {key} on {entry.get('worklog_date')}.")
        issue_days.add(issue_day)
        allocated_seconds[key] += seconds
    issue_types_by_key = {
        str(entry["issue_key"]): str(entry["issue_type"])
        for entry in entries
    }
    for key, seconds in allocated_seconds.items():
        required_seconds = int(allocation_hours(issue_types_by_key[key]) * 3600)
        if "existing_issue_seconds" not in manifest or key not in manifest["existing_issue_seconds"]:
            raise ValueError(
                f"{key} has no existing-worklog baseline in this preview; regenerate it before submission."
            )
        existing_seconds = int(manifest["existing_issue_seconds"][key])
        if existing_seconds < 0 or existing_seconds + seconds != required_seconds:
            raise ValueError(
                f"{key} must total exactly {required_seconds // 3600} hours across existing "
                "and proposed worklogs before submission."
            )
    resolutions = {
        item.get("exception"): str(item.get("resolution", "")).strip()
        for item in manifest.get("exception_resolutions", [])
    }
    unresolved = [item for item in manifest.get("exceptions", []) if not resolutions.get(item)]
    if unresolved:
        raise ValueError(
            f"Resolve every preview exception in exception_resolutions before submission; "
            f"{len(unresolved)} remain unresolved."
        )
    token = canonical_manifest_hash(manifest)
    print(f"Approval required. Review all {len(entries)} entries and exceptions in: {manifest_path.resolve()}")
    print(f"Type exactly: APPROVE {token}")
    if not sys.stdin.isatty():
        raise ValueError("Approval requires an interactive terminal; no worklogs were submitted.")
    if input("> ").strip() != f"APPROVE {token}":
        print("Approval did not match; no worklogs were submitted.")
        return 1

    if client is None:
        raise ValueError(
            "Submission requires a fresh manually authenticated Jira browser session."
        )
    user = client.current_user()
    account_id = str(user.get("accountId") or user.get("key") or user.get("name") or "")
    if account_id != manifest["jira_account"]["account_id"]:
        raise ValueError("Authenticated Jira account differs from the account in this preview.")
    tz = ZoneInfo(manifest["timezone"])

    if client.base_url != str(manifest.get("jira_url", "")).rstrip("/"):
        raise ValueError("Jira URL differs from the preview; no worklogs were submitted.")
    if args.api_version != str(manifest.get("api_version")):
        raise ValueError("Jira API version differs from the preview; no worklogs were submitted.")
    all_current = fetch_all_period_worklogs(client, start, end, account_id, tz)
    daily_existing: Dict[date, float] = defaultdict(float)
    for item in all_current:
        daily_existing[item.day] += item.hours
    pending_by_day: Dict[date, float] = defaultdict(float)
    pending_by_issue: Dict[str, int] = defaultdict(int)
    duplicate_entries = set()
    for index, item in enumerate(entries):
        day = date.fromisoformat(item["worklog_date"])
        require_weekday(day)
        if not start <= day <= end:
            raise ValueError(f"Invalid proposed date {day}; no worklogs were submitted.")
        seconds = int(item["seconds"])
        if seconds <= 0 or seconds > int(HOURS_PER_DAY * 3600):
            raise ValueError(f"Invalid duration for {item['issue_key']}; no worklogs were submitted.")
        duplicate = worklog_for_issue_day(str(item["issue_key"]), day, all_current)
        is_already_processed = item.get("execution_result") in (
            "created_and_verified",
            "created_and_verified_after_timeout",
            "skipped_duplicate",
        )
        if is_already_processed:
            continue
        if duplicate:
            duplicate_entries.add(index)
            item["approval_status"] = "skipped_duplicate"
            item["execution_result"] = "skipped_duplicate"
            item["worklog_id"] = duplicate.worklog_id
            continue
        pending_by_day[day] += seconds / 3600.0
        pending_by_issue[str(item["issue_key"])] += seconds
    current_by_issue: Dict[str, int] = defaultdict(int)
    for item in all_current:
        current_by_issue[item.issue_key] += item.seconds
    for key, proposed_seconds in allocated_seconds.items():
        target_seconds = int(allocation_hours(issue_types_by_key[key]) * 3600)
        if current_by_issue[key] + pending_by_issue[key] != target_seconds:
            raise ValueError(
                f"{key} worklogs changed since preview; existing plus pending time would not "
                f"equal its {target_seconds // 3600}-hour allocation. Regenerate the preview."
            )
    for day, hours in pending_by_day.items():
        if daily_existing[day] + hours > HOURS_PER_DAY:
            raise ValueError(f"Capacity changed on {day}; the daily limit would be exceeded. No worklogs were submitted.")

    manifest["approval"]["status"] = "approved"
    write_report(manifest, manifest_path)
    for index, entry in enumerate(entries):
        if index in duplicate_entries or entry.get("execution_result") in (
            "created_and_verified",
            "created_and_verified_after_timeout",
            "skipped_duplicate",
        ):
            continue
        day = date.fromisoformat(entry["worklog_date"])
        seconds = int(entry["seconds"])
        description = str(entry["description"]).strip()
        if not description:
            raise ValueError("Every proposed worklog needs a non-empty description; no worklogs were submitted.")
        key = str(entry["issue_key"])
        latest_worklogs = fetch_all_period_worklogs(client, start, end, account_id, tz)
        duplicate = worklog_for_issue_day(key, day, latest_worklogs)
        if duplicate:
            entry["worklog_id"] = duplicate.worklog_id
            entry["execution_result"] = "skipped_duplicate"
            entry["approval_status"] = "skipped_duplicate"
            write_report(manifest, manifest_path)
            continue
        current_issue_seconds = sum(
            item.seconds for item in latest_worklogs if item.issue_key == key
        )
        pending_issue_seconds = sum(
            int(other["seconds"])
            for other_index, other in enumerate(entries[index:], start=index)
            if other.get("issue_key") == key
            and other.get("execution_result") not in (
                "created_and_verified",
                "created_and_verified_after_timeout",
                "skipped_duplicate",
            )
            and worklog_for_issue_day(
                key, date.fromisoformat(other["worklog_date"]), latest_worklogs
            )
            is None
        )
        target_issue_seconds = int(allocation_hours(str(entry["issue_type"])) * 3600)
        if current_issue_seconds + pending_issue_seconds != target_issue_seconds:
            entry["execution_result"] = "blocked_issue_allocation_changed"
            entry["approval_status"] = "pending"
            manifest["approval"]["status"] = "partial"
            write_report(manifest, manifest_path)
            raise RuntimeError(
                f"Monthly worklog total changed for {key}; execution stopped before creating "
                "another entry."
            )
        latest_hours = sum(item.hours for item in latest_worklogs if item.day == day)
        if latest_hours + seconds / 3600.0 + sum(
            int(other["seconds"]) / 3600.0
            for other_index, other in enumerate(entries[index + 1 :], start=index + 1)
            if date.fromisoformat(other["worklog_date"]) == day
            and other_index not in duplicate_entries
            and other.get("execution_result") not in (
                "created_and_verified",
                "created_and_verified_after_timeout",
                "skipped_duplicate",
            )
        ) > HOURS_PER_DAY:
            entry["execution_result"] = "blocked_capacity_changed"
            entry["approval_status"] = "pending"
            write_report(manifest, manifest_path)
            raise RuntimeError(f"Daily capacity changed on {day}; execution stopped before creating {key}.")
        started = local_started(day, tz)
        try:
            created = client.create_worklog(key, started, seconds, description)
        except (requests.Timeout, requests.ConnectionError):
            refreshed = current_worklogs_for_issue(client, key, account_id, start, end, tz)
            match = equivalent_worklog(key, day, seconds, description, refreshed)
            if match:
                entry["worklog_id"] = match.worklog_id
                entry["execution_result"] = "created_and_verified_after_timeout"
                entry["approval_status"] = "approved"
                write_report(manifest, manifest_path)
                continue
            entry["execution_result"] = "ambiguous_after_timeout"
            entry["approval_status"] = "pending_verification"
            write_report(manifest, manifest_path)
            raise RuntimeError(
                f"Jira timed out creating {key} on {day}; no matching worklog was found. "
                "Execution stopped to prevent a duplicate. Check Jira before retrying."
            )
        except JiraApiError:
            entry["execution_result"] = "failed_api_error"
            entry["approval_status"] = "pending"
            manifest["approval"]["status"] = "partial"
            write_report(manifest, manifest_path)
            raise
        worklog_id = str(created.get("id", ""))
        refreshed = current_worklogs_for_issue(client, key, account_id, start, end, tz)
        verified = next(
            (
                item for item in refreshed
                if item.worklog_id == worklog_id
                and item.day == day
                and item.seconds == seconds
                and same_description(item.description, description)
            ),
            None,
        )
        if verified is None:
            entry["execution_result"] = "created_but_verification_failed"
            entry["approval_status"] = "pending_verification"
            write_report(manifest, manifest_path)
            raise RuntimeError(f"Jira returned worklog {worklog_id} for {key}, but read-back verification failed.")
        entry["worklog_id"] = worklog_id
        entry["execution_result"] = "created_and_verified"
        entry["approval_status"] = "approved"
        write_report(manifest, manifest_path)

    manifest["approval"]["status"] = "completed"
    manifest["execution_completed_at"] = datetime.now(timezone.utc).isoformat()
    manifest["execution_summary"] = {
        "created_and_verified": sum(1 for entry in entries if entry["execution_result"] == "created_and_verified"),
        "created_and_verified_after_timeout": sum(1 for entry in entries if entry["execution_result"] == "created_and_verified_after_timeout"),
        "skipped_duplicates": sum(1 for entry in entries if entry["execution_result"] == "skipped_duplicate"),
        "failed_or_pending": sum(1 for entry in entries if entry["execution_result"] not in ("created_and_verified", "created_and_verified_after_timeout", "skipped_duplicate")),
    }
    final_worklogs = fetch_all_period_worklogs(client, start, end, account_id, tz)
    actually_created = [
        entry for entry in entries
        if entry["execution_result"] in ("created_and_verified", "created_and_verified_after_timeout")
    ]
    final_days = weekdays(start, end)
    manifest["daily_reconciliation"] = daily_report(final_days, final_worklogs, [], manifest.get("exceptions", []))
    manifest["totals"]["hours_approved"] = round(
        sum(float(entry["hours"]) for entry in actually_created), 2
    )
    manifest["totals"]["hours_successfully_logged"] = manifest["totals"]["hours_approved"]
    manifest["totals"]["proposed_hours"] = manifest["totals"]["hours_approved"]
    manifest["totals"]["weekdays_fully_reconciled"] = sum(
        1 for item in manifest["daily_reconciliation"] if item["final_hours"] == HOURS_PER_DAY
    )
    manifest["totals"]["weekdays_with_gaps"] = sum(
        1 for item in manifest["daily_reconciliation"] if item["remaining_hours_to_8"] > 0
    )
    write_report(manifest, manifest_path)
    print(json.dumps(manifest["execution_summary"], indent=2))
    return 0


def fetch_all_period_worklogs(
    client: JiraClient,
    start: date,
    end: date,
    account_id: str,
    tz: ZoneInfo,
) -> List[Worklog]:
    jql = (
        f'worklogAuthor = currentUser() AND worklogDate >= "{start.isoformat()}" '
        f'AND worklogDate <= "{end.isoformat()}"'
    )
    issues = client.search_issues(jql, ["summary"])
    result = []
    for issue in issues:
        key = str(issue.get("key", ""))
        if not key:
            continue
        result.extend(
            parse_worklogs(key, client.issue_worklogs(key), account_id, start, end, tz)
        )
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a Jira monthly worklog reconciliation preview; writes require a separate approved submission."
    )
    parser.add_argument("--jira-url", default=os.getenv("JIRA_URL"), help="Jira base URL; alternatively set JIRA_URL.")
    parser.add_argument("--api-version", choices=("2", "3"), default="3")
    parser.add_argument("--project-key", default=os.getenv("JIRA_PROJECT_KEY"))
    parser.add_argument("--timezone", help="IANA timezone override; default is the Jira profile timezone.")
    parser.add_argument("--output", default="jira_monthly_preview.json", help="Preview JSON output (paired CSV is created beside it).")
    parser.add_argument("--submit", help="Submit a reviewed preview JSON after mandatory interactive approval.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.jira_url:
        print("Set JIRA_URL or pass --jira-url.", file=sys.stderr)
        return 2
    try:
        with ManualJiraBrowser(args.jira_url, args.project_key) as browser:
            client = JiraClient(
                args.jira_url,
                args.api_version,
                session=requests.Session(),
                browser=browser,
            )
            if args.submit:
                return submit_manifest(args, client)
            report = make_preview(args, client)
            csv_path = write_report(report, Path(args.output))
            print(
                f"Read-only preview: {args.output} and {csv_path}\n"
                f"Period {report['reporting_period']['start']} through {report['reporting_period']['end']}; "
                f"{report['totals']['total_proposed_worklogs']} proposed entries; "
                f"{len(report['exceptions'])} exceptions."
            )
            print(json.dumps(report["totals"], indent=2))
            return 0
    except (
        RuntimeError,
        requests.RequestException,
        OSError,
        ValueError,
        OSError,
        KeyError,
        TypeError,
        AttributeError,
        ZoneInfoNotFoundError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
