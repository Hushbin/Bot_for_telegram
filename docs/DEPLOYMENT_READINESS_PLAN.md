# Deployment readiness implementation plan

Prepared: **18 October 2026**. Status: planned; implementation has not started.
Source: the [deployment readiness finding](IMPROVEMENT_SUGGESTIONS.md).

## Problem and scope

`deploy/activate_release.sh` switches `/opt/smebot/current`, restarts the bot,
waits 10 seconds and checks only `systemctl is-active`. A running process can
still be initializing or unable to use Telegram or Oracle. The five-minute
`smebot-health` timer probes Telegram, Oracle and backup freshness separately;
activation neither waits for those checks nor receives their results.

Add a fresh, bounded readiness gate to deployment. Keep the existing private
Bastion transport, archive verification, serialized GitHub deployment job and
versioned runtime settings. Do not send chat messages, create orders, write test
products, start another Telegram poller or change database schemas.

## Acceptance policy

- Before switching releases, probe the current release's dependencies and backup.
  An unhealthy or indeterminate baseline blocks the deployment before the symlink
  changes. A missing current release is handled as an explicit first deployment.
- After restart, require a readiness signal from this candidate's actual bot
  process, not just a separately launched probe. Match release SHA, systemd
  invocation ID and PID so an old signal cannot satisfy the gate.
- Emit that signal only after schema/configuration checks, command registration,
  configured seller API startup, worker startup and the first successful Telegram
  polling response. `post_init` alone is insufficient: PTB invokes it before the
  updater starts. Remove readiness on shutdown and reject a changed invocation
  or process restart during the observation window.
- Require fresh Telegram `getMe`, Oracle connection/schema/read checks, and an
  accessible logical export younger than 36 hours, matching the current health
  policy. Probe the seller API's loopback health endpoint when configured.
- Require the same ready process to remain active for 10 continuous seconds.
  Finish candidate checks within 90 seconds after restart. Bound each probe and
  the entire probe subprocess; SDK retries must not extend that deadline.
- Never print `Activated <sha>` or remove the incoming archive until all checks
  pass. Report a nonzero exit and a specific redacted reason otherwise.

These checks establish startup readiness; manual chat acceptance and ongoing
health monitoring remain separate checks.

## Failure decisions

| Observation | Action |
| --- | --- |
| Baseline unhealthy, stale backup or probe unavailable | Fail before switching; keep the current release |
| Candidate ready and every required check passes | Report successful activation and remove its archive |
| Restart fails, candidate exits/restarts, or readiness deadline expires while dependencies remain healthy | Restore the previous release once; verify its readiness; report deployment failure |
| Candidate dependency/configuration probe fails | Probe the previous release's dependencies under its own code and runtime settings, without starting another poller |
| Previous-release probes pass while the candidate fails | Treat as a candidate regression; restore and verify the previous release once |
| Both dependency probes fail or comparison is indeterminate | Report an external/unknown failure; leave the candidate selected if its process is running, retain both releases and the archive, and require operator investigation |
| Backup becomes stale/unavailable after a healthy baseline | Fail activation without treating backup freshness as a code-regression rollback signal |
| No previous release exists, or rollback also fails | Report failure explicitly; never claim restoration or activation succeeded |

A dead candidate is restored when a previous release exists even if external
dependencies are unavailable. Keep rollback attempts bounded and never alternate
releases in a loop. An ambiguous failure must remain visible in GitHub Actions;
it must not be converted to a green deployment because systemd is active.

## Implementation steps

1. **Add structured, bounded probes.** Introduce a reusable readiness module and
   a fixed probe CLI. Return versioned JSON containing release, check names,
   pass/fail/unknown, safe reason codes and observation times. Reuse the existing
   Telegram and recovery-storage logic, while adding schema validation and hard
   process deadlines. Health metric publication remains independent: an OCI
   monitoring ingestion failure must not decide readiness. Share probe logic with
   `scripts/recovery.py health` while preserving its existing metrics contract.
2. **Instrument the actual process.** In `main.py`, observe the first successful
   polling request through a supported PTB integration, then publish an atomic
   readiness file under a systemd-managed `/run/smebot` directory only when all
   startup conditions hold. Include release/invocation/PID identity. Extend
   `deploy/smebot.service` for the runtime directory; keep readiness contents free
   of credentials, contacts, object keys and Telegram responses.
3. **Provide constrained VM execution.** The deployment user cannot read
   `/etc/smebot/bot.env`. Install a root-owned helper that accepts only a validated
   40-character release SHA, resolves it beneath `/opt/smebot/releases`, and runs
   the fixed probe as `smebot`. Load the fixed bot environment and that release's
   `deploy/runtime.env` using systemd environment-file semantics. Do not source
   environment files as shell code, expose their values, accept arbitrary commands
   or grant general sudo access. Extend bootstrap and tests for only the required
   fixed helper permissions.
4. **Implement the gate and rollback controller.** Update the root-installed
   activator to collect a baseline, switch/restart, wait for matching process
   readiness, run candidate probes and apply the decision table. Preserve the
   previous target and verify rollback with the previous code/settings. Serialize
   local activation attempts as well as GitHub jobs. Bound comparison and rollback
   phases separately so an entire attempt finishes within five minutes after
   switching, excluding release extraction and dependency installation.
5. **Expose actionable results.** Print a concise readiness/rollback summary in
   the SSH command output consumed by GitHub Actions. Distinguish readiness
   failures from later Bastion cleanup failures, which do not undo a healthy
   release. Retain redacted diagnostics for failed activations.
6. **Document and complete.** Update VM setup, failure recovery and deployment
   instructions. Record dated automated results separately from VM/manual checks,
   remove the completed finding and this plan once implemented, and link the
   maintained readiness guide from the documentation index.

## Verification and staged rollout

Add subprocess-level activation tests with disposable release directories, fake
systemctl/probes and an injectable clock. Cover every decision-table row, stale
readiness files, invocation changes, first deployment, restart loops, hung probes,
deadline enforcement, rollback failure, preserved runtime settings, archives and
redacted output. Test lifecycle signaling before/after first polling success and
on shutdown, plus optional seller API startup and failure. Prove that separate
probes never call `getUpdates` or send messages. Run the full offline suite with
`.venv/bin/python -m unittest discover -s tests -v`.

The activator is copied to `/usr/local/sbin/smebot-activate` by bootstrap; changing
the repository file alone does not update the VM. Use two rollout stages:

1. Install the backward-compatible systemd runtime-directory setting as root
   first. Deploy the readiness instrumentation/probe-capable release through the
   current activator and verify its signal and read-only probes. Preserve this
   probe-capable release as the fallback for the next deployment.
2. Install the reviewed helper, narrow sudo rules and new activator as root,
   then verify them on a disposable/test VM with
   successful activation, candidate failure and simulated dependency outage before
   enabling the new gate for routine deployments.

Reject unsupported previous-release probe protocols before switching rather than
silently weakening the gate. Keep a root-owned recovery copy of the existing
activator for operator rollback. Do not automate destructive failure injection
against the live bot. No implementation tests, VM installation or new live checks
have been performed for this plan.
