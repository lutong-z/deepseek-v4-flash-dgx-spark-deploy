# Rollback and recovery

`apply` and `update` capture an external rollback state before the first
container stop. The state file is not a public artifact and must remain under
the operator-owned deployment directory:

```text
<PATH>/dgx-spark-production/rollback.json
```

## State contents and integrity

The state records, for both head and worker:

- deployment ID, mode, and configuration hash;
- exact previous Docker image ID and command vector;
- complete previous labels, including ownership labels;
- environment and working directory;
- bind mounts and host network/IPC/GPU/security/ulimit settings; and
- whether the role was running before mutation.

The engine adds `state_sha256`, a SHA-256 over the canonical state object
without that field. Rollback rejects a missing, malformed, tampered, or
mismatched state file before mutation. The rollback state integrity hash is an
integrity check, not a secret MAC; protect the external state directory with
normal filesystem permissions.

## Normal rollback

Get the current deployment ID from the redacted plan and use the exact value:

```bash
bin/dgx-deploy rollback \
  --env-file <PATH>/dgx-spark-production/production.env \
  --lock-file <PATH>/dgx-spark-production/production.lock.json \
  --state-file <PATH>/dgx-spark-production/rollback.json \
  --confirm <DEPLOYMENT_ID>
```

Rollback first validates the current ready lock, proves each previous image is
available, and inspects the current pair. It may remove only exact names whose
image/command match the captured current-or-previous contract and whose labels
prove current deployment ownership or match the captured owner/role labels. It
then recreates all captured host settings, labels, environment, mounts, image,
and command, starts worker before head, and verifies the complete restored
state plus `/health` and `/v1/models`.

A same-name container with an unexpected image, command, role, owner, or label
set is never removed. A missing role is treated as absent only when the other
state and target identity remain exact; an ambiguous partial pair fails closed.

## Failed update and partial recovery

An `update` failure automatically attempts rollback when both previous roles
were captured completely. The state file remains the source of truth even if
the automatic rollback also reports an error. Inspect its integrity and rerun:

```bash
python3 -c 'import json; from pathlib import Path; p=Path("<PATH>/dgx-spark-production/rollback.json"); v=json.loads(p.read_text()); assert v.get("schema_version")==1 and v.get("state_sha256"); print("rollback state present")'
bin/dgx-deploy rollback \
  --env-file <PATH>/dgx-spark-production/production.env \
  --lock-file <PATH>/dgx-spark-production/production.lock.json \
  --state-file <PATH>/dgx-spark-production/rollback.json \
  --confirm <DEPLOYMENT_ID>
```

For a partial-target or empty-target `apply`, the captured roles are absent. If
creation fails after one role is created, the engine cleans up only the partial
containers carrying the current deployment and role labels. It does not claim
success or invent rollback data for a target that had no previous pair. If
ownership cannot be proved, leave the pair untouched and obtain an
operator-approved recovery review; never remove it by PID or unqualified name.

## Post-rollback verification

Always run repository verification after rollback:

```bash
bin/dgx-deploy verify \
  --env-file <PATH>/dgx-spark-production/production.env \
  --lock-file <PATH>/dgx-spark-production/production.lock.json
```

Then run the bounded health/models smoke documented in
[`README.md`](../README.md) and preserve only redacted results under the
external result/log roots. A rollback is not complete while either role is not
running, labels/command differ, model files/marker fail, or the API is not
ready.
