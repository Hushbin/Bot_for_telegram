# Migrate the bot default branch from master to main

**Prepared: 25 October 2026. Status: future operational runbook; migration has
not been performed.** Source baseline: `b025719`. Recheck the repository and
GitHub settings before executing. The seller and infrastructure repositories
already use `main`.

## Goal and boundaries

Rename the bot's existing default branch, preserve its complete history, and
make `main` the only automatic deployment branch. This needs no application,
database, Terraform resource or secret-value change. Existing deployed releases
are identified by commit SHA, so a branch rename alone does not replace the
running release. The first deployment after migration still restarts the bot
and ends in-memory browser sessions and conversations as documented.

This document authorizes no migration by itself. Execute it in an agreed change
window with repository administration access and access to the `dev-bot`
environment settings. If those settings are unavailable to automation, the
repository administrator must perform the named GitHub settings steps.

Use GitHub's **branch rename** operation. Do not create an unrelated `main`,
force-push history, or delete `master` before changing the default branch.

## 1. Record the starting state and pause concurrent changes

1. Ask collaborators to pause pushes, merges and manual deployment dispatches
   until the verification in section 7 finishes. Do not combine this migration
   with application fixes or schema changes.
2. From a clean bot checkout, run:

   ```sh
   git status --short
   git fetch origin '+refs/heads/*:refs/remotes/origin/*' --prune
   git ls-remote --symref origin HEAD
   git ls-remote --heads origin master main
   git log -1 --format='%H %s' origin/master
   ```

   Stop if the working tree is dirty, the default branch is no longer `master`,
   or an unexpected `main` already exists. Investigate rather than overwriting
   either branch. Record the full starting SHA outside the repository as the
   migration baseline.
3. In GitHub Actions, wait for all bot test/deploy runs to finish. Record the last
   successful deployment's run URL and release SHA. A successful historic run
   is not proof of current service health; check current readiness using the
   [deployment readiness guide](DEPLOYMENT_READINESS.md).
4. Record open PRs and their base branches, branch protection/rulesets, required
   checks, branch-specific GitHub App integrations, and the `dev-bot` environment's
   deployment branch policy. Record policy names and settings, never secrets.
   Inspect organization rules too if applicable. Do not assume protection or
   environment settings match the README.

## 2. Prepare the migration changes without deploying them

Create a short-lived migration branch from current `origin/master`:

```sh
git switch -c codex/rename-default-branch origin/master
```

Make these changes together:

| Location | Required change |
| --- | --- |
| `.github/workflows/deploy.yml` push trigger | Change `branches: [master]` to `branches: [main]` |
| Same workflow, `deploy.if` | Change `refs/heads/master` to `refs/heads/main`; keep the PR exclusion |
| `README.md` deployment instructions | Replace current deployment-branch and environment-policy references with `main` |
| `AGENTS.md` | Describe automatic deployment from `main` |
| Maintained docs and sibling repos | Update current bot URLs containing `/blob/master/` or `/tree/master/`, and current branch instructions, to `main` |

Keep job names, `dev-bot`, deployment concurrency, readiness checks, rollback,
secrets and Markdown `paths-ignore` filters unchanged. Do not enable deployment
from both branches. Historical validation records and pinned commit URLs need
not be rewritten. This runbook's old-name examples remain intentional.

Find references in the bot, seller and infrastructure checkouts:

```sh
rg -n --hidden -g '!.git/**' -g '!node_modules/**' -g '!.venv/**' \
  'refs/heads/master|origin/master|/blob/master/|/tree/master/|\bmaster\b' \
  .github deploy scripts README.md AGENTS.md docs
```

Run that search from each repository and review each match; do not globally
replace unrelated words or history. Inspect scripts/integrations outside these
paths if the repository has gained any. No branch-name assumptions were found
in bot deployment shell scripts at the preparation baseline, but check again.

Validate YAML and `git diff --check`. Confirm the only automatic push target is
`main`, PRs still test without deploying, and manual dispatch still exists.
Commit and push the migration branch. A PR can validate the workflow before
cutover; direct merge is also possible under the owner's authorized workflow.
With the current trigger, a feature-branch push alone does not run bot CI.

## 3. Prepare GitHub permissions and deployment policy

1. Confirm an administrator can rename the default branch in GitHub's branch
   list (repository **Code → branch selector → View all branches → rename**).
   UI labels may vary. Do not execute the rename yet.
2. Inspect **Settings → Environments → dev-bot → Deployment branches and tags**.
   If it uses selected branch rules, add an exact `main` branch rule while
   temporarily retaining `master`. Preserve existing reviewer/protection rules;
   do not broaden access to all branches or add a wildcard. If it allows only
   protected branches, ensure the renamed branch will satisfy protection.
3. Review exact-name repository/organization rulesets and protections. GitHub
   generally updates branch protection when renaming, but verify the resulting
   coverage rather than relying on that behavior for every integration.
4. Preserve existing secrets and variable bindings. A rename does not require
   rotating or copying their values. Record any administrator-only blocker
   before starting cutover.

The workflow remains the deployment gate: `master` is still its sole deployment
target until the migration commit is merged. Temporarily allowing the future
branch in the environment does not itself launch a deployment.

## 4. Merge the workflow change and rename the branch

1. Recheck that no new deployment is running and that the migration branch
   includes the latest `master`. Resolve any new changes and rerun relevant
   validation before merging.
2. Merge the migration commit into `master` using the approved merge method.
   This commit changes the trigger to `main`, so its push to `master` deliberately
   does **not** launch automatic deployment. This is the short cutover interval;
   keep the change freeze in place. Record the resulting cutover SHA.
3. Immediately use GitHub's branch rename operation to rename **`master` →
   `main`**. GitHub updates the default branch and generally retargets open PR
   bases and protections. Verify all three explicitly. Review PRs whose head
   was the renamed branch separately; GitHub may close them.
4. Verify on GitHub that `main` is default and has the cutover SHA. Confirm
   `master` is absent from the branch list. Do not issue another deletion if
   the rename already removed it.
5. Confirm `dev-bot` accepts `main`, branch rules protect it as intended, required
   checks retain their job names, and PRs target the correct default. Renaming
   does not update every external consumer: Actions references such as
   `owner/repo@master`, clones and external automation need explicit updates.

A rename is not a reliable substitute for a push/dispatch trigger. Explicitly
validate and deploy the new branch in section 6.

## 5. Update local clones and linked documentation

For a clean checkout currently on local `master`, use:

```sh
git fetch origin '+refs/heads/*:refs/remotes/origin/*' --prune
git switch master
git branch -m master main
git branch --set-upstream-to=origin/main main
git remote set-head origin -a
git status --short --branch
git ls-remote --symref origin HEAD
git ls-remote --heads origin master main
```

If a local `main` already exists, stop and compare it before renaming; do not
force-delete it. These commands rename the local branch and set tracking; they
do not overwrite local commits. Check for local divergence before pulling or
pushing. Other feature branches can fetch/rebase onto `origin/main`; existing
seller-order/session fixes must still resolve their own outstanding conflicts.
Update any explicit PR base or tracking configuration that still says `master`.

Commit the reviewed current-link changes in the seller and infrastructure
repositories to their existing `main` branches. Verify links to bot docs now
resolve. No frontend release or Terraform apply is necessary for those
Markdown-only updates. Preserve links to immutable commit snapshots.

## 6. Run and monitor the first main workflow

1. In GitHub Actions, choose **Test and deploy bot → Run workflow → main**.
   Alternatively, with authenticated GitHub CLI:

   ```sh
   gh workflow run deploy.yml --repo Hushbin/Bot_for_telegram --ref main
   gh run list --repo Hushbin/Bot_for_telegram --workflow deploy.yml --branch main --limit 5
   ```

2. Identify the new run by `main`, manual-dispatch event and the recorded cutover
   SHA; inspect its URL and use `gh run watch RUN_ID --repo
   Hushbin/Bot_for_telegram --exit-status` if desired. Do not confuse a previous
   successful `master` run with this validation.
3. Require both `test` and `deploy` jobs to succeed. A skipped deploy is a failed
   migration check: inspect its branch condition and environment policy.
4. Inspect deployment readiness output and confirm the running release SHA
   matches the dispatched commit. Verify temporary Bastion/session cleanup and
   the administrator allowlist restoration through the normal deployment logs.
   Follow [deployment readiness](DEPLOYMENT_READINESS.md) for failures.
5. Check the service and a permitted read-only Telegram interaction. Do not
   create real buyer orders as automated test fixtures. Confirm any expected
   restart/session effects are understood.

Do not apply Terraform or dispatch the infrastructure apply workflow for this
branch-name migration.

## 7. Finish, verify filters and retire remaining references

- Remove the obsolete `master` rule from `dev-bot` after `main` deployment
  succeeds. Retain only the intended deployment policy and existing safeguards.
- Confirm GitHub default branch, remote HEAD, branch protection, PR bases and
  external consumers all use `main`. No writable remote `master` should remain.
- Confirm code/workflow changes on `main` trigger tests and deployment, and PRs
  run tests only. Manual dispatch verifies the branch gate but does not by itself
  prove the automatic push trigger; observe the next authorized code/workflow
  push, or review its trigger now and record automatic validation as pending.
- Confirm Markdown-only pushes/PRs skip the workflow. Use the next actual docs
  update to observe this; do not claim an unperformed test. Non-Markdown files
  in `docs/` and mixed changes should still run CI.
- If required status checks are enabled, account for path-filtered workflows
  leaving required checks pending. Test a docs-only PR; use an always-running
  check with conditional jobs if that policy requires one.
- Delete the merged migration feature branch, verify clean working trees and
  remove stale local remote-tracking refs with a fetch/prune. Do not delete
  other unmerged work as part of the rename.
- Record migration date, baseline/cutover/deployed SHAs, run URL, policy checks,
  observed filter behavior and any pending manual checks. Update this runbook
  with the outcome; remove obsolete preparation instructions if superseded.
  End the change freeze only once the release and policy checks pass.

## Recovery if cutover fails

**Before the rename:** keep `master` as default. If the main-only workflow commit
was already merged, revert that specific migration commit on `master` to restore
its branch trigger and condition. Preserve unrelated new commits; do not reset
or force-push the branch. Observe the resulting test/deployment run and restore
the original environment policy once it is safe.

**After the rename, deployment blocked by settings:** prefer fixing the `main`
environment/protection mismatch and retrying the same reviewed SHA. A branch
rename does not corrupt application data or require a database restore.

**A full naming rollback is necessary:** pause all dispatches and wait for active
jobs to finish. Ensure the environment can deploy `master`, then rename `main`
back to `master` through GitHub. Restore the workflow push filter and deploy
condition to `master`, current documentation and original policies in a reviewed
commit. Verify default branch, protections, PR bases and remote HEAD again;
update clones using the inverse of section 5. Run the workflow explicitly on
`master` and require successful readiness before removing its temporary `main`
allow rule. Keep the recorded cutover SHA for audit; never recreate competing
branches with force pushes.

If application activation itself fails, use the existing release rollback
process. Branch-name rollback and application-release rollback are separate
operations; choose based on the actual failure and preserve durable data.
