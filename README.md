# Jira monthly time logging agent

This tool creates a full-month Jira worklog reconciliation preview. Running it without
`--submit` is read-only: it does not create or modify worklogs. The preview consists
of a JSON approval manifest and a companion CSV report.

## Setup

Use Python 3.10 or newer and install the dependencies:

```powershell
python -m pip install -r requirements.txt
```

## Manual Jira login

Manual browser login is required for every invocation. Each run opens a temporary,
isolated Chrome context without a saved browser profile, cookies, or storage state.
Sign in to Jira in that window; the app detects sign-in and continues automatically.
Authentication cookies are used only in memory for that run and are discarded when
the context closes. The script never saves or reuses a Jira session, and no Jira
password or API token is needed.
Set the site URL for the run:

```powershell
$env:JIRA_URL = "https://your-company.atlassian.net"
$env:JIRA_PROJECT_KEY = "PROJECT"
```

Create a read-only preview (manual sign-in is required):

```powershell
python .\jira_time_logging_agent.py --output .\jira_monthly_preview.json
```

The Jira profile timezone is used when available. To override it, pass an IANA timezone,
for example `--timezone Asia/Kolkata`. `--project-key` limits issue eligibility, while
the account's existing worklogs are checked across all projects when enforcing daily
capacity.

## Review and submission

1. Review the JSON and CSV, including every weekday, issue, proposed date, duration,
   description, and exception.
2. Edit each proposed description to describe work that actually occurred, then set
   its `description_confirmed` value to `true`. Set `activity_confirmed` to `true` only
   after verifying that the work occurred on the proposed date.
3. Add a non-empty resolution for every item in `exception_resolutions`. For every
   proposed entry, confirm the work actually occurred on that date; issue creation,
   closure, or assignment by itself is not proof of work.
4. Submit the reviewed JSON with the same Jira account and URL. This run opens a fresh Chrome session and requires another manual sign-in:

```powershell
python .\jira_time_logging_agent.py --submit .\jira_monthly_preview.json
```

The agent prints a SHA-256-bound approval phrase and requires it to be typed in an
interactive terminal. It rechecks duplicates and current daily capacity, then enters
each approved worklog through Clockwork's **My Work → Add Worklog** UI and reads it
back from Jira to verify the saved entry. The existing Jira remaining estimate is
read before each entry and restored in the UI, so logging time does not intentionally
change the estimate. Do not edit the manifest after the approval phrase is printed;
any edits require restarting the submission command. If the UI result or read-back
cannot be established, execution stops to avoid repeating an uncertain entry.
A partially executed manifest can be reviewed and re-approved to resume remaining
entries.
Every invocation uses a new browser session; the CLI has no API-token authentication
mode.

## Scheduling defaults

- A Bug is eligible only if its Jira creator is you and it was created during the
  reporting month, or Jira history verifies that you changed its status to **Closed**
  during that month. Each eligible Bug has a 3-hour monthly target.
- A Task, Story, Sub-task, or Sub-story is eligible only if Jira assignment history
  shows it was assigned to you during the reporting month. Merely remaining assigned
  from a prior month does not qualify. Each eligible issue has a 36-hour monthly
  target.
- The reporting window starts on the first day of the current month and ends today.
  No prior-month assignments, creations, or closures qualify.
- Allocations are split across weekdays with capacity available, never exceeding 8
  total hours per day. Existing worklogs by your account for an eligible issue count
  toward its monthly target, so a target already met does not get logged again. Each
  issue can have at most one entry per date.
- A created/closed Bug uses its creation/closure date as the preferred date; assigned
  issue dates are capacity-balanced suggestions. Proposed dates are not proof of work
  and require explicit confirmation that the work actually occurred on each date.
- If a weekday lacks capacity, an allocation is spread across additional weekdays; no
  individual worklog exceeds the remaining capacity under the 8-hour daily maximum.
- A single issue is never proposed more than once on the same date. Existing worklogs
  on an issue/date block another proposal for that issue/date.
- Worklogs are hard-blocked on Saturdays and Sundays both in the preview schedule and
  immediately before the Jira API write. Existing weekend entries are preserved and
  reported as exceptions; they are never silently shifted to another day.
- Existing worklogs by the authenticated account count toward
  the 8-hour daily limit across all Jira issues.
- Insufficient capacity, duplicate candidates, and weekdays without confirmed activity
  are reported rather than silently discarded. Issue creation, closure, or assignment
  alone does not verify that the suggested work description or date is accurate.
- Descriptions begin as editable suggestions; both the description and activity/date
  must be confirmed before submission.

Run focused tests with:

```powershell
python -m unittest discover -s tests -v
```
