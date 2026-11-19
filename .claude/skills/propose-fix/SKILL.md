---
name: propose-fix
description: Propose the smallest correct fix for a confirmed root cause as a diff with rationale, impact analysis, risks and a test plan. In apply mode, and only with the user's approval, deploy it to a non-production org, run tests, verify and roll back if it does not hold. Use after investigate-ticket has confirmed a root cause.
---

# Propose a fix

Prerequisite: a root cause with evidence in `tickets/<KEY>/report.md`. If the status is
"Not confirmed", say so and propose how to confirm it instead of a fix.

## 1. Choose the layer

Prefer, in this order:

1. correcting the faulty line or setting where it is;
2. extending an existing pattern in the org (the existing handler method, flow, custom metadata);
3. adding something new only when 1 and 2 cannot work, and say why.

State in one or two sentences why this layer is right and why the alternatives are worse (for
example: "the rule lives in `CaseEscalationService`; fixing it in a flow would leave two places
that decide priority").

## 2. Impact analysis

- `find_dependencies` on each component you would change, then `search_metadata` for its name to
  catch references the dependency API does not record.
- List every caller and say whether its behaviour changes.
- Check the existing tests that cover the component: do they still express the intended behaviour?

## 3. Draft the change locally

1. Edit the retrieved files under `tickets/<KEY>/metadata/force-app/…` (local files only).
2. Add or update an Apex test that fails before the change and passes after it. A fix without
   such a test is incomplete; if a test is impossible, explain why and give manual steps.
3. `workspace_diff` produces the unified diff against the org's version and saves `fix.diff`.
4. Keep the diff minimal: no reformatting, no renames, no unrelated cleanup.

## 4. Write the proposal

In the report's Fix section:

- the diff (fenced as `diff`);
- rationale: why this is the smallest correct change;
- impact: what else uses the changed component and what changes for it;
- risks, including data already affected (existing records are not repaired by a code fix: say
  how many, with a query, and how they could be corrected);
- test plan: Apex tests to add or update, and manual steps with expected results.

## 5a. Advise mode (default)

Stop here. Tell the user what to change, where, and how to verify it:

- the file and line, and the diff;
- the test to add;
- deployment order if more than one component;
- the manual check that proves the ticket is resolved.

Mention that apply mode exists if they want you to deploy and test in a sandbox.

## 5b. Apply mode (only with approval, never on production)

Ask before starting, naming the org and the components. Then loop, at most five attempts:

1. `deploy_component` with `validate_only: true` and `test_level: "RunSpecifiedTests"` when a test
   class exists. Fix compile or test failures locally and repeat.
2. `deploy_component` for real. It snapshots the org's version, stops on drift, deploys,
   verifies and returns a `change_id`.
3. `run_apex_tests` on the relevant test classes.
4. Re-check the original symptom with records or logs: repeat the query that showed the problem,
   or reproduce and read a new debug log. Tests passing is not enough on its own.
5. If it holds: record the `change_id`, test results and the re-check in the report.
   If it does not: `rollback_change`, record what happened, revise the hypothesis or the fix.

Each write prompts the user. If a prompt is declined, stop and ask how they want to proceed.
If `deploy_component` reports drift, the org changed since you retrieved it: read the
difference, re-apply the fix on the current version, and say so in the report.
If it answers `deploy_in_progress`, the deploy is still running in the org and may still change
it: do not deploy again and do not count the attempt as failed. Tell the user to check Setup >
Deployment Status, wait, then continue from step 3 once it has finished, or `rollback_change` it
(which does nothing while the org still holds the version from before).

After five attempts without success, roll back, report what was tried and what was learned, and
stop.

## 6. Finish

- Update the report: Fix, Test results, Risks.
- `validate_report`.
- Offer `write-ticket-comment`.

## Do not

- Propose a fix for an unconfirmed cause without labelling it as a hypothesis-driven attempt.
- Change production. If the only org is production, the proposal is advice.
- Use `update_records` to hide a symptom. Data corrections are proposed separately and named as such.
- Leave a deployed change in place when verification failed.
