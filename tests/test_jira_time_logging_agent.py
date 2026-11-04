import json
import tempfile
import unittest
from argparse import Namespace
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from jira_time_logging_agent import (
    JiraClient,
    ManualJiraBrowser,
    Worklog,
    build_jql,
    closure_date_by_account,
    eligibility_reason,
    format_duration,
    has_assignment_history_in_period,
    jira_date,
    parse_worklogs,
    require_weekday,
    schedule_proposals,
    submit_manifest,
    weekdays,
)


class ManualJiraBrowserTests(unittest.TestCase):
    @patch("playwright.sync_api.sync_playwright")
    def test_each_run_uses_a_temporary_context_and_waits_for_manual_login(self, sync_playwright):
        page = Mock()
        page.evaluate.side_effect = [
            {"status": 401, "user": None},
            {"status": 200, "user": {"accountId": "user-1"}},
        ]
        context = Mock()
        context.new_page.return_value = page
        browser = Mock()
        browser.new_context.return_value = context
        playwright = Mock()
        playwright.chromium.launch.return_value = browser
        sync_playwright.return_value.start.return_value = playwright

        manual_browser = ManualJiraBrowser("https://jira.example/", "PROJ")
        with patch("builtins.print"):
            self.assertIs(manual_browser.__enter__(), manual_browser)

        browser.new_context.assert_called_once_with()
        page.goto.assert_called_once_with(
            "https://jira.example", wait_until="domcontentloaded"
        )
        self.assertEqual(page.evaluate.call_count, 2)
        page.wait_for_timeout.assert_called_once_with(2000)

        manual_browser.__exit__(None, None, None)

        context.close.assert_called_once_with()
        browser.close.assert_called_once_with()
        playwright.stop.assert_called_once_with()
        self.assertIsNone(manual_browser.context)


class JiraDateTests(unittest.TestCase):
    def test_duration_formatting(self):
        self.assertEqual(format_duration(0), "0m")
        self.assertEqual(format_duration(5400), "1h 30m")

    def test_worklog_timestamps_use_the_configured_local_date(self):
        parsed = jira_date("2026-10-01T20:30:00.000+0000", ZoneInfo("Asia/Kolkata"))
        self.assertEqual(parsed, date(2026, 10, 2))

    def test_weekends_are_excluded(self):
        self.assertEqual(
            weekdays(date(2026, 10, 23), date(2026, 10, 26)),
            [date(2026, 10, 23), date(2026, 10, 26)],
        )

    def test_worklog_author_and_period_are_filtered(self):
        raw = [
            {
                "id": "1",
                "author": {"accountId": "user-1"},
                "started": "2026-10-05T09:00:00.000+0530",
                "timeSpentSeconds": 3600,
                "comment": {
                    "type": "doc",
                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Verified"}]}],
                },
            },
            {
                "id": "2",
                "author": {"accountId": "user-2"},
                "started": "2026-10-05T09:00:00.000+0530",
                "timeSpentSeconds": 3600,
            },
        ]
        found = parse_worklogs(
            "PROJ-1", raw, "user-1", date(2026, 10, 1), date(2026, 10, 31), ZoneInfo("Asia/Kolkata")
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].description, "Verified")

    def test_jira_cloud_search_uses_next_page_token_endpoint(self):
        client = JiraClient.__new__(JiraClient)
        client.api_version = "3"
        client.api_path = lambda suffix: f"/rest/api/3{suffix}"
        client.request = Mock(
            side_effect=[
                {
                    "issues": [{"key": "PROJ-1"}],
                    "nextPageToken": "page-2",
                },
                {
                    "issues": [{"key": "PROJ-2"}],
                },
            ]
        )

        found = client.search_issues("project = PROJ", ["summary"])

        self.assertEqual([issue["key"] for issue in found], ["PROJ-1", "PROJ-2"])
        self.assertEqual(client.request.call_count, 2)
        first = client.request.call_args_list[0]
        second = client.request.call_args_list[1]
        self.assertEqual(first.args[1], "/rest/api/3/search/jql")
        self.assertNotIn("nextPageToken", first.kwargs["json"])
        self.assertEqual(second.kwargs["json"]["nextPageToken"], "page-2")
        self.assertNotIn("startAt", second.kwargs["json"])

    def test_jira_server_search_uses_start_at_pagination(self):
        client = JiraClient.__new__(JiraClient)
        client.api_version = "2"
        client.api_path = lambda suffix: f"/rest/api/2{suffix}"
        client.request = Mock(
            side_effect=[
                {"issues": [{"key": "PROJ-1"}], "startAt": 0, "total": 2},
                {"issues": [{"key": "PROJ-2"}], "startAt": 1, "total": 2},
            ]
        )

        found = client.search_issues("project = PROJ", ["summary"])

        self.assertEqual([issue["key"] for issue in found], ["PROJ-1", "PROJ-2"])
        self.assertEqual(client.request.call_args_list[0].args[1], "/rest/api/2/search")
        self.assertEqual(client.request.call_args_list[1].kwargs["json"]["startAt"], 1)

    def test_weekday_guard_rejects_saturday_and_sunday(self):
        for weekend in (date(2026, 10, 24), date(2026, 10, 25)):
            with self.subTest(day=weekend):
                with self.assertRaisesRegex(ValueError, "Monday through Friday"):
                    require_weekday(weekend)

    def test_create_worklog_does_not_send_weekend_entries_to_jira(self):
        client = JiraClient.__new__(JiraClient)
        client.api_version = "3"
        client.api_path = lambda suffix: f"/rest/api/3{suffix}"
        client.request = Mock()

        with self.assertRaisesRegex(ValueError, "weekend"):
            client.create_worklog(
                "PROJ-1", "2026-10-24T09:00:00.000+0530", 3600, "Sample work"
            )

        client.request.assert_not_called()

    def test_create_worklog_allows_weekday(self):
        client = JiraClient.__new__(JiraClient)
        client.api_version = "3"
        client.api_path = lambda suffix: f"/rest/api/3{suffix}"
        client.request = Mock(return_value={"id": "created"})

        result = client.create_worklog(
            "PROJ-1", "2026-10-26T09:00:00.000+0530", 3600, "Sample work"
        )

        self.assertEqual(result["id"], "created")
        client.request.assert_called_once()


class SchedulingTests(unittest.TestCase):
    def issue(self, key, issue_type, created=None, resolved=None):
        return {
            "key": key,
            "fields": {
                "summary": f"Summary for {key}",
                "issuetype": {"name": issue_type},
                "created": created,
                "resolutiondate": resolved,
                "creator": {"accountId": "other-user"},
                "reporter": {"accountId": "other-user"},
                "assignee": None,
            },
        }

    def assign_to_user(self, issue):
        issue["fields"]["assignee"] = {"accountId": "user-1"}
        issue["_assignment_changed_to_account_in_period"] = True
        return issue

    def close_by_user(self, issue):
        issue["_confirmed_actor"] = "user-1"
        issue["_confirmed_closure_date"] = date.fromisoformat(
            issue["fields"]["resolutiondate"][:10]
        )
        return issue

    def test_assigned_story_gets_thirty_six_hours_with_eight_hour_daily_cap(self):
        days = weekdays(date(2026, 10, 5), date(2026, 10, 9))
        proposals, exceptions, counts = schedule_proposals(
            [self.assign_to_user(self.issue("PROJ-1", "Story"))],
            [],
            days,
            date(2026, 10, 1),
            date(2026, 10, 31),
            "user-1",
            "UTC",
        )
        self.assertEqual(sum(proposal["hours"] for proposal in proposals), 36)
        self.assertTrue(all(proposal["hours"] <= 8 for proposal in proposals))
        self.assertTrue(any("Confirm each proposed date" in item for item in exceptions))
        self.assertEqual(counts["eligible_assigned_issues"], 1)

    def test_bug_created_by_account_gets_three_hours(self):
        days = weekdays(date(2026, 10, 5), date(2026, 10, 9))
        issue = self.issue("PROJ-1", "Bug", created="2026-10-06T10:00:00.000+0000")
        issue["fields"]["creator"] = {"accountId": "user-1"}
        proposals, exceptions, counts = schedule_proposals(
            [issue],
            [],
            days,
            date(2026, 10, 1),
            date(2026, 10, 31),
            "user-1",
            "UTC",
        )
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["hours"], 3)
        self.assertEqual(proposals[0]["reason"], "created bug")
        self.assertEqual(proposals[0]["worklog_date"], "2026-10-06")
        self.assertFalse(exceptions)
        self.assertEqual(counts["eligible_bugs"], 1)

    def test_assigned_subtask_gets_thirty_six_hours_across_days(self):
        days = weekdays(date(2026, 10, 5), date(2026, 10, 16))
        proposals, exceptions, counts = schedule_proposals(
            [self.assign_to_user(self.issue("PROJ-1", "Sub-task"))],
            [],
            days,
            date(2026, 10, 1),
            date(2026, 10, 25),
            "user-1",
            "UTC",
        )
        self.assertEqual(sum(proposal["hours"] for proposal in proposals), 36)
        self.assertTrue(all(proposal["hours"] <= 8 for proposal in proposals))
        self.assertEqual(len({proposal["worklog_date"] for proposal in proposals}), len(proposals))
        self.assertTrue(any("Confirm each proposed date" in item for item in exceptions))
        self.assertEqual(counts["eligible_assigned_issues"], 1)

    def test_assigned_substory_gets_thirty_six_hours(self):
        days = weekdays(date(2026, 10, 5), date(2026, 10, 16))
        proposals, _, _ = schedule_proposals(
            [self.assign_to_user(self.issue("PROJ-1", "Sub-story"))],
            [],
            days,
            date(2026, 10, 1),
            date(2026, 10, 25),
            "user-1",
            "UTC",
        )
        self.assertEqual(sum(item["hours"] for item in proposals), 36)

    def test_assigned_task_gets_thirty_six_hours(self):
        days = weekdays(date(2026, 10, 5), date(2026, 10, 16))
        proposals, _, _ = schedule_proposals(
            [self.assign_to_user(self.issue("PROJ-1", "Task"))],
            [],
            days,
            date(2026, 10, 1),
            date(2026, 10, 25),
            "user-1",
            "UTC",
        )
        self.assertEqual(sum(item["hours"] for item in proposals), 36)
        self.assertEqual({item["reason"] for item in proposals}, {"assigned task"})

    def test_bug_created_before_period_is_not_eligible(self):
        issue = self.issue("PROJ-1", "Bug", created="2026-09-30T10:00:00.000+0000")
        issue["fields"]["creator"] = {"accountId": "user-1"}
        self.assertIsNone(
            eligibility_reason(
                issue,
                "user-1",
                date(2026, 10, 1),
                date(2026, 10, 25),
                ZoneInfo("UTC"),
            )
        )

    def test_bug_assigned_to_account_without_creation_or_closure_is_not_eligible(self):
        issue = self.assign_to_user(self.issue("PROJ-1", "Bug"))
        self.assertIsNone(
            eligibility_reason(
                issue,
                "user-1",
                date(2026, 10, 1),
                date(2026, 10, 25),
                ZoneInfo("UTC"),
            )
        )

    def test_capacity_counts_existing_logs_on_all_issue_keys(self):
        days = weekdays(date(2026, 10, 5), date(2026, 10, 9))
        existing = [
            Worklog("OTHER-1", "old", date(2026, 10, 7), 7200, "user-1", "Other work"),
        ]
        issue = self.issue("PROJ-1", "Bug", created="2026-10-05T10:00:00.000+0000")
        issue["fields"]["creator"] = {"accountId": "user-1"}
        proposals, _, _ = schedule_proposals(
            [issue],
            existing,
            days,
            date(2026, 10, 1),
            date(2026, 10, 31),
            "user-1",
            "UTC",
        )
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["worklog_date"], "2026-10-05")
        self.assertEqual(proposals[0]["hours"], 3)

    def test_closed_bug_gets_one_three_hour_allocation(self):
        days = weekdays(date(2026, 10, 5), date(2026, 10, 9))
        proposals, exceptions, counts = schedule_proposals(
            [
                self.assign_to_user(
                    self.close_by_user(
                        self.issue(
                            "PROJ-2",
                            "Bug",
                            created="2026-10-06T10:00:00.000+0000",
                            resolved="2026-10-07T12:00:00.000+0000",
                        )
                    )
                )
            ],
            [],
            days,
            date(2026, 10, 1),
            date(2026, 10, 31),
            "user-1",
            "UTC",
        )
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["hours"], 3)
        self.assertEqual(proposals[0]["reason"], "closed bug")
        self.assertEqual(proposals[0]["worklog_date"], "2026-10-07")
        self.assertFalse(exceptions)
        self.assertEqual(counts["eligible_bugs"], 1)

    def test_issue_without_involvement_is_not_scheduled(self):
        days = weekdays(date(2026, 10, 5), date(2026, 10, 9))
        issue = self.issue("PROJ-3", "Bug", created="2026-10-06T10:00:00.000+0000")
        proposals, exceptions, _ = schedule_proposals(
            [issue], [], days, date(2026, 10, 1), date(2026, 10, 31), "user-1", "UTC"
        )
        self.assertFalse(proposals)
        self.assertTrue(exceptions)

    def test_closed_bug_work_is_moved_when_closure_day_lacks_capacity(self):
        days = weekdays(date(2026, 10, 5), date(2026, 10, 9))
        existing = [
            Worklog("OTHER-1", "old", date(2026, 10, 7), 25200, "user-1", "Existing")
        ]
        proposals, _, _ = schedule_proposals(
            [
                self.close_by_user(
                    self.issue("PROJ-4", "Bug", resolved="2026-10-07T16:00:00.000+0000")
                )
            ],
            existing,
            days,
            date(2026, 10, 1),
            date(2026, 10, 31),
            "user-1",
            "UTC",
        )
        self.assertEqual(sum(proposal["hours"] for proposal in proposals), 3)
        self.assertTrue(any(proposal["date_changed_from_preferred"] for proposal in proposals))
        for day in days:
            logged = sum(item.hours for item in existing if item.day == day)
            proposed = sum(
                proposal["hours"]
                for proposal in proposals
                if proposal["worklog_date"] == day.isoformat()
            )
            self.assertLessEqual(logged + proposed, 8)

    def test_existing_issue_worklog_on_preferred_date_is_not_duplicated(self):
        days = weekdays(date(2026, 10, 5), date(2026, 10, 9))
        issue = self.close_by_user(
            self.issue("PROJ-5", "Bug", resolved="2026-10-07T12:00:00.000+0000")
        )
        existing = [
            Worklog(
                "PROJ-5",
                "already-logged",
                date(2026, 10, 7),
                3600,
                "user-1",
                "Different time already logged.",
            )
        ]
        proposals, exceptions, _ = schedule_proposals(
            [issue],
            existing,
            days,
            date(2026, 10, 1),
            date(2026, 10, 31),
            "user-1",
            "UTC",
        )
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["hours"], 2)
        self.assertNotEqual(proposals[0]["worklog_date"], "2026-10-07")
        self.assertTrue(proposals[0]["date_changed_from_preferred"])

    def test_bug_already_at_three_hours_gets_no_additional_proposal(self):
        days = weekdays(date(2026, 10, 5), date(2026, 10, 9))
        issue = self.close_by_user(
            self.issue("PROJ-6", "Bug", resolved="2026-10-07T12:00:00.000+0000")
        )
        existing = [
            Worklog("PROJ-6", "already-logged", date(2026, 10, 7), 10800, "user-1", "Bug work")
        ]
        proposals, _, _ = schedule_proposals(
            [issue],
            existing,
            days,
            date(2026, 10, 1),
            date(2026, 10, 31),
            "user-1",
            "UTC",
        )
        self.assertFalse(proposals)

    def test_jql_includes_bug_creator_closure_and_period_assignment(self):
        query = build_jql(date(2026, 10, 1), date(2026, 10, 24), "PROJ")
        self.assertIn("creator = currentUser()", query)
        self.assertIn("status CHANGED TO Closed BY currentUser()", query)
        self.assertIn("assignee WAS currentUser() DURING", query)
        self.assertIn("subTaskIssueTypes()", query)
        self.assertIn('"Task"', query)
        self.assertIn('"Sub-story"', query)
        self.assertIn('project = "PROJ"', query)

    def test_closure_date_requires_current_user_closed_transition_in_period(self):
        histories = [
            {
                "created": "2026-09-30T10:00:00.000+0000",
                "author": {"accountId": "user-1"},
                "items": [{"field": "status", "toString": "Closed"}],
            },
            {
                "created": "2026-10-06T10:00:00.000+0000",
                "author": {"accountId": "other-user"},
                "items": [{"field": "status", "toString": "Closed"}],
            },
            {
                "created": "2026-10-07T10:00:00.000+0000",
                "author": {"accountId": "user-1"},
                "items": [{"field": "status", "toString": "Closed"}],
            },
        ]
        self.assertEqual(
            closure_date_by_account(
                histories,
                date(2026, 10, 1),
                date(2026, 10, 25),
                "user-1",
                ZoneInfo("UTC"),
            ),
            date(2026, 10, 7),
        )

    def test_assignment_history_must_be_during_the_reporting_period(self):
        histories = [
            {
                "created": "2026-10-05T10:00:00.000+0000",
                "items": [{"field": "assignee", "to": "user-1"}],
            },
            {
                "created": "2026-09-30T10:00:00.000+0000",
                "items": [{"field": "assignee", "to": "user-1"}],
            },
        ]
        self.assertTrue(
            has_assignment_history_in_period(
                histories,
                date(2026, 10, 1),
                date(2026, 10, 25),
                "user-1",
                "Saurabh Kumar",
                ZoneInfo("UTC"),
            )
        )
        self.assertFalse(
            has_assignment_history_in_period(
                histories[1:],
                date(2026, 10, 1),
                date(2026, 10, 25),
                "user-1",
                "Saurabh Kumar",
                ZoneInfo("UTC"),
            )
        )


class SubmissionTests(unittest.TestCase):
    def test_manual_client_creates_worklog_in_ui_and_verifies_readback(self):
        browser = Mock()
        client = JiraClient("https://jira.example", "3", browser=browser)
        client.issue_details = Mock(return_value={"fields": {"timeestimate": 7200}})
        client.current_user = Mock(return_value={"accountId": "user-1", "timeZone": "UTC"})
        client.issue_worklogs = Mock(
            return_value=[
                {
                    "id": "log-1",
                    "author": {"accountId": "user-1"},
                    "started": "2026-10-26T09:00:00.000+0000",
                    "timeSpentSeconds": 3600,
                    "comment": "Actual work",
                }
            ]
        )

        result = client.create_worklog(
            "PROJ-1", "2026-10-26T09:00:00.000+0000", 3600, "Actual work"
        )

        self.assertEqual(result["id"], "log-1")
        browser.create_worklog.assert_called_once_with(
            "PROJ-1",
            "2026-10-26T09:00:00.000+0000",
            3600,
            "Actual work",
            7200,
        )

    def test_submission_requires_activity_and_date_confirmation(self):
        manifest = {
            "schema_version": 1,
            "approval": {"status": "pending"},
            "reporting_period": {"start": "2026-10-01", "end": "2026-10-31"},
            "proposed_worklogs": [
                {
                    "description_confirmed": True,
                    "activity_confirmed": False,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "preview.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "activity_confirmed=true"):
                submit_manifest(Namespace(submit=str(manifest_path)))


if __name__ == "__main__":
    unittest.main()
