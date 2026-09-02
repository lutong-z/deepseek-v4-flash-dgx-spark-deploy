# Rollback contract

`apply` and `update` capture an external rollback state file before mutation.
The state records the deployment ID/mode, exact previous Docker image ID,
command vector, ownership labels, environment, and running state for both
roles. It contains no secrets, raw logs, prompts, responses, or developer
paths.

`rollback` renders and validates the current mode/lock, reads the complete
state file, proves each previous image is locally available on its node, and
refuses incomplete or mismatched state. It stops and removes only the current
pair carrying the exact deployment/role ownership labels, recreates the
captured image and command, starts worker before head, and verifies exact
image/command/labels plus service readiness.

The command requires `--confirm` equal to the current rendered deployment ID.
A missing, tampered, unhealthy, or unavailable prerequisite fails before
mutation. The engine never guesses a mutable tag, kills by PID, or touches an
unowned same-name container.
