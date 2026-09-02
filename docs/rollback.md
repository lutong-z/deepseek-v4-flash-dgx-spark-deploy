# Rollback contract

Every future successful deployment must write an external manifest containing
the deployment ID, config/profile/model/image identities, role labels, and the
label-owned container IDs. It must retain the previous successful manifest.
No secret, raw inspect output, prompt, response, or operator path belongs in
that manifest.

Rollback must first render and validate the target manifest, verify strict host
keys, and prove the old image, model, config, and network prerequisites on both
nodes. It may then stop only the currently owned head/worker pair, start the
old worker followed by the old head, and run health and smoke gates. A missing,
tampered, unhealthy, or unavailable prerequisite must stop before mutation.

The current scaffold cannot roll back or mutate anything. Its cluster rollback
entry point fails closed. Future failure handling must never guess a mutable
tag, kill a process by PID, or affect an unowned same-name container. An
interrupted rollback must leave explicit external state and support recovery
to the just-replaced manifest.
