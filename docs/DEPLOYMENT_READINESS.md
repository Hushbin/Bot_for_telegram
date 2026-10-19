# Deployment readiness

The deployment gate uses the running bot's identity and fresh read-only dependency
checks. `systemctl is-active` alone no longer completes activation once the new
root-installed activator is installed. This establishes startup readiness; manual
buyer/seller acceptance and ongoing health monitoring remain separate.

## What is checked

The bot writes `/run/smebot/ready.json` atomically after configuration/schema
validation, Telegram command registration, configured seller API startup, order
worker startup and its first successful polling response. A public `ExtBot`
subclass observes the existing polling call; it never starts another poller.
The file contains protocol version, release SHA, systemd invocation ID, PID and
observation time only. Local/non-systemd runs do not publish it. Shutdown clears
it, and subsequent polling responses remove it if a worker or seller API task
has exited. Systemd owns the runtime directory's lifecycle.

The fixed `scripts/readiness.py` CLI reports protocol-1 JSON with release identity,
check names, `pass`/`fail`/`unknown`, fixed reason codes and observation times:

| Check | Requirement |
| --- | --- |
| Configuration | Valid OCI VM configuration, including Vault resolution when configured |
| Telegram | Successful `getMe`; no `getUpdates`, messages or command changes |
| Database | Fresh Oracle connection, `SELECT 1` and required schema-column reads |
| Backup | Latest logical export is under 36 hours old, with readable pointer/manifest and accessible data object metadata |
| Seller API | Loopback `/api/health` responds successfully when the panel is configured |

Each check has an eight-second subprocess deadline, including imports, Vault,
instance-principal authentication, connection setup and SDK retries. The root
helper also gives the whole probe service 43 seconds plus two seconds to stop its
control group. A timeout or malformed/missing result is unknown, never healthy.
The backup check does not download or checksum the export payload; restore drills
remain necessary. Probes do not write application data or change schemas.

The scheduled health job shares these probes while retaining its existing four
`Healthy` dimensions: `service`, `telegram`, `database`, `backup`. Metric ingestion
is separate and cannot decide activation readiness.

## Activation and failure decisions

The activator verifies the incoming archive, prepares the release and takes a
local `flock` for the entire attempt. GitHub also serializes deployment jobs.
Before switching, it requires the existing release's protocol-1 dependency and
backup probes to pass. An unsupported or unhealthy baseline blocks deployment.
A missing `current` is an explicit first deployment.

After switching, restart and candidate checks have a combined 90-second budget.
The gate matches the signal's release, invocation and PID against systemd,
rejects restarts, requires a fresh signal, and observes the same ready process
for at least ten seconds. It probes once readiness appears, or reserves the last
48 seconds for dependency classification if startup has not signaled. Probe time
counts toward the observation period; identity is rechecked afterward.
Comparison and rollback have separate bounded phases. The controller limits the
whole post-switch attempt to 295 seconds, below the five-minute ceiling; archive
extraction and dependency installation precede that deadline.

| Failure | Result |
| --- | --- |
| Unhealthy/unknown baseline or unsupported previous protocol | No symlink switch |
| All candidate checks pass and process is stable | Print `Activated <sha>` and remove incoming archive |
| Restart fails, candidate dies/restarts, or readiness fails with healthy dependencies | Restore previous release once and verify its readiness; deployment still fails |
| Candidate dependency/configuration check fails, previous release's probes pass | Restore and verify previous release once |
| Both releases' dependency checks fail, or comparison is unknown | Leave a running candidate selected; fail deployment for investigation |
| Backup alone becomes stale/unavailable | Leave running candidate selected; fail deployment without calling this a code regression |
| No fallback, or rollback is not ready | Explicit failure; no claim of successful restoration |

A dead candidate is restored even during an external outage. Both releases and
the incoming archive are retained on every activation failure. There is no loop
alternating releases and no automatic bypass for an unhealthy baseline.
An unknown candidate probe alone does not establish a regression, even if the
fallback's probes succeed; automatic comparison rollback requires a definite
candidate failure.

SSH output includes concise `READINESS` events with safe reason codes. Failed
attempts retain normalized events in `/opt/smebot/diagnostics/`; raw subprocess
stdout/stderr and exception contents are excluded. Bastion cleanup failures are
reported separately, fail the workflow and do not undo a healthy activation.

## Install on an existing VM in two stages

The normal workflow deploys application files; it does not replace root-owned
helpers or service units. **Do not install the new activator before establishing
a probe-capable fallback release.** Back up the current root-installed activator
and sudo rules as root before starting:

```sh
sudo install -d -m 0700 /root/smebot-readiness-recovery
sudo cp --no-clobber /usr/local/sbin/smebot-activate /root/smebot-readiness-recovery/activate-before-readiness
sudo cp --no-clobber /etc/sudoers.d/smebot-deploy /root/smebot-readiness-recovery/sudoers-before-readiness
```

### Stage 1: instrumentation and report-only probes

Copy the reviewed `deploy/smebot.service` to the VM from the workstation, install
it as root, and reload systemd. Its new `RuntimeDirectory` setting works with the
old bot and old activator:

```sh
sudo install -m 0644 -o root -g root /tmp/smebot.service /etc/systemd/system/smebot.service
sudo systemctl daemon-reload
```

Deploy this application release through the **existing** activator. Check:

```sh
systemctl show smebot.service -p MainPID -p InvocationID -p ActiveState
cat /run/smebot/ready.json
sudo systemd-run --quiet --wait --pipe --collect --service-type=exec \
  -p User=smebot -p Group=smebot -p WorkingDirectory=/opt/smebot/current \
  -p EnvironmentFile=/etc/smebot/bot.env \
  -p EnvironmentFile=/opt/smebot/current/deploy/runtime.env \
  -p RuntimeMaxSec=43 -p TimeoutStopSec=2 -p KillMode=control-group \
  /opt/smebot/current/.venv/bin/python /opt/smebot/current/scripts/readiness.py
```

Every check must pass, and the signal's SHA/PID/invocation must match the selected
release and systemd. Preserve this release directory as the fallback. No gate is
enforced in this report-only stage; it lets operators resolve configuration,
backup access and deadline issues first.

### Stage 2: install enforcement

Before production enforcement, verify successful activation, a failed candidate,
and a simulated dependency outage on a disposable/test VM. Do not inject failures
into the live bot. Then install the reviewed files from the selected release:

```sh
sudo install -d -m 0755 -o root -g root /usr/local/lib/smebot
sudo install -m 0644 -o root -g root /opt/smebot/current/deploy/activation.py /usr/local/lib/smebot/activation.py
sudo install -m 0755 -o root -g root /opt/smebot/current/deploy/probe_release.sh /usr/local/sbin/smebot-probe
```

Add this line to the existing sudoers file using `sudo visudo -f
/etc/sudoers.d/smebot-deploy`; keep the existing exact service-restart permission:

```sudoers
smebot-deploy ALL=(root) NOPASSWD: /usr/local/sbin/smebot-probe *
```

The helper rejects anything except one lowercase 40-character SHA, resolves it
beneath `/opt/smebot/releases` without accepting a symlink alias, and launches
only the fixed probe CLI as `smebot`. Systemd loads `/etc/smebot/bot.env` and then
that release's `deploy/runtime.env`. Environment files are never sourced as shell
code; sudo does not grant arbitrary commands or access to their values.

Validate and install the activator last:

```sh
sudo visudo -cf /etc/sudoers.d/smebot-deploy
sudo install -m 0755 -o root -g root /opt/smebot/current/deploy/activate_release.sh /usr/local/sbin/smebot-activate
```

A new VM's `bootstrap_vm.sh` installs all these files together. Its initial
activation still requires healthy candidate dependencies and a recent accessible
export. If its first activation fails only because no export exists yet, it
leaves the running candidate selected and retains the archive. Establish a real
initial export with `sudo systemctl start smebot-export.service`, check its
journal and retry the normal deployment. The failed first attempt must remain
visible until the backup requirement is satisfied.

## Investigating a failed deployment

Read the `READINESS` lines in the workflow and redacted VM diagnostics. Determine
the selected target with `readlink -f /opt/smebot/current`; a red workflow can leave
a running candidate selected during an external/unknown failure. Check systemd,
its journal, `ready.json` and the dependency/backup reason codes before acting.
Fix external dependencies or refresh a legitimate export, then retry deployment.
Do not create dummy production orders or exports with fabricated timestamps.

For emergency operator recovery, restore the root-owned saved activator and sudo
rules if necessary. This is an explicit manual return to the old activation
policy, not a workflow environment flag. Verify the selected release and runtime
settings afterward. The new controller refuses fallback releases without
protocol 1; an older-code rollback requires an operator to choose compatible
service units, schema and activation policy. Review ordering's rollback limits.
Keep failed archives/releases until investigation is complete. No schema changes
are introduced by this feature.

## Validation

Automated validation on **19 October 2026** uses disposable local directories,
fake systemctl/probe subprocesses and an injected clock; it does not contact the
VM, Oracle or Telegram. The full suite was run in an isolated Python 3.13
environment installed from the pinned, hashed `requirements.txt`, using
`python -m unittest discover -s tests -v`.
Result: **435 tests ran, 383 passed and 52 optional Oracle tests were skipped**.
Shell syntax, Python 3.10 syntax compatibility and changed documentation links
also passed validation.
Coverage includes the failure table, identity/restart/staleness rejection, first
deployment, stable observation, timeouts, bounded rollback, unsupported protocols,
archive/settings retention, redaction, helper argument/path restrictions,
lifecycle gating, optional seller startup, read-only SQL/Telegram calls and the
existing health metric contract.

VM installation, test-VM failure drills and live manual chat acceptance have
**not** been performed for this change. They remain rollout work even when the
local suite passes.
