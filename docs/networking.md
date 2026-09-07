# Networking, RoCE, and endpoint access

The deployment has separate control and data-plane values:

- `HEAD_HOST`/`WORKER_HOST` are reviewed node addresses for SSH control;
- `HEAD_SSH_HOST`/`WORKER_SSH_HOST` may be local aliases such as
  `DGX-SPARK-0`/`DGX-SPARK-1`;
- `HEAD_NODE_ADDR`/`WORKER_NODE_ADDR`/`MASTER_ADDR` are explicit RoCE and
  rendezvous addresses rendered into vLLM/NCCL; and
- production uses API `8101` and master `29619`, while candidate uses isolated
  API `18101` and master `29621`.

Do not put host inventory, private addresses, SSH paths, or network captures in
this public repository. Use placeholders in copied examples.

## Strict manual diagnostics

The repository engine uses batch mode, strict host-key checking, an explicit
known-hosts file, identity file, and connect timeout. Use the same wrapper for
manual diagnostics instead of bypassing configured SSH policy:

```bash
ssh_dgx() {
  ssh -T -o BatchMode=yes -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile=<PATH>/known_hosts -o ConnectTimeout=10 \
    -i <PATH>/ssh-key "$@"
}
```

## Read-only preflight

`dgx-deploy verify` performs the repository preflight. It checks Docker/ARM64,
GPU visibility, RDMA links, interface presence, HCA mapping, image IDs/labels,
model files and marker, and exact mode ports before API readiness. The direct
network checks represented by that gate are:

```bash
ssh_dgx DGX-SPARK-0 "ip link show <HEAD_DATA_INTERFACE>"
ssh_dgx DGX-SPARK-1 "ip link show <WORKER_DATA_INTERFACE>"
ssh_dgx DGX-SPARK-0 "rdma link"
ssh_dgx DGX-SPARK-1 "rdma link"
ssh_dgx DGX-SPARK-0 "ibdev2netdev -v"
ssh_dgx DGX-SPARK-1 "ibdev2netdev -v"
ssh_dgx DGX-SPARK-0 "nvidia-smi --query-gpu=name,memory.total --format=csv,noheader"
ssh_dgx DGX-SPARK-1 "nvidia-smi --query-gpu=name,memory.total --format=csv,noheader"
```

Confirm the reviewed RoCE GID/index, MTU, peer addresses, and free API/master
ports before any mutation. Preflight is read-only: it must not change routes,
VLAN/PFC/ECN, firmware, DHCP/DNS, firewall policy, interfaces, or GPU state.

## Production endpoint

Production's validated API endpoint is `<HEAD_NODE_ADDR>:8101`. A public bind is
an explicit exception and requires `ALLOW_PUBLIC_API=1`; otherwise the parser
requires a loopback bind. After `plan`, `render`, and `apply --dry-run` pass:

```bash
bin/dgx-deploy verify \
  --env-file <PATH>/dgx-spark-production/production.env \
  --lock-file <PATH>/dgx-spark-production/production.lock.json
ssh_dgx DGX-SPARK-0 "curl --fail --silent --show-error http://127.0.0.1:8101/health"
ssh_dgx DGX-SPARK-0 "curl --fail --silent --show-error http://127.0.0.1:8101/v1/models"
```

Do not use a broad bind in candidate mode. Candidate always uses loopback and
its API is `18101`; its rendezvous port is `29621`.

## Loopback tunnel

If public bind is not approved, use a local loopback SSH tunnel with strict
options. The local forward port must not overlap either service port:

```bash
ssh -N -T -o BatchMode=yes -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=<PATH>/known_hosts -o ConnectTimeout=10 \
  -i <PATH>/ssh-key -L <LOCAL_FORWARD_PORT>:127.0.0.1:8101 DGX-SPARK-0
```

Bind the local listener only to `127.0.0.1`. Do not add launchd/systemd units,
hidden daemons, proxy credentials, or arbitrary remote shell hooks to this
repository.

## Candidate endpoint, health, smoke, and cleanup

Candidate configuration must use isolated roots and refs as well as isolated
ports:

```text
DEPLOYMENT_MODE=candidate
MASTER_ADDR=192.168.100.10
MASTER_PORT=29621
API_PORT=18101
API_BIND_ADDR=127.0.0.1
ALLOW_PUBLIC_API=0
REMOTE_ROOT=<REMOTE_PATH>/dgx-spark-candidate/deploy
MODEL_ROOT=<REMOTE_PATH>/models/DeepSeek-V4-Flash-0731
STATE_ROOT=<REMOTE_PATH>/dgx-spark-candidate/state
CACHE_ROOT=<REMOTE_PATH>/dgx-spark-candidate/cache
LOG_ROOT=<REMOTE_PATH>/dgx-spark-candidate/logs
RESULT_ROOT=<REMOTE_PATH>/dgx-spark-candidate/results
HEAD_IMAGE_REF=<CANDIDATE_HEAD_DIGEST>
WORKER_IMAGE_REF=<CANDIDATE_WORKER_DIGEST>
```

Validate and dry-run candidate files independently:

```bash
bin/dgx-deploy config validate --env-file <PATH>/dgx-spark-candidate/candidate.env
bin/dgx-deploy render --env-file <PATH>/dgx-spark-candidate/candidate.env --lock-file <PATH>/dgx-spark-candidate/candidate.lock.json --output-dir <PATH>/dgx-spark-candidate/contracts
bin/dgx-deploy plan --env-file <PATH>/dgx-spark-candidate/candidate.env --lock-file <PATH>/dgx-spark-candidate/candidate.lock.json
bin/dgx-deploy apply --env-file <PATH>/dgx-spark-candidate/candidate.env --lock-file <PATH>/dgx-spark-candidate/candidate.lock.json --state-file <PATH>/dgx-spark-candidate/rollback.json --dry-run
```

Only after the isolated candidate is applied with its own confirmation should
health/models and bounded smoke be run:

```bash
ssh_dgx DGX-SPARK-0 "curl --fail --silent --show-error http://127.0.0.1:18101/health"
ssh_dgx DGX-SPARK-0 "curl --fail --silent --show-error http://127.0.0.1:18101/v1/models"
ssh_dgx DGX-SPARK-0 "curl --fail --silent --show-error --max-time 180 -H 'Content-Type: application/json' --data-binary @<REMOTE_PATH>/candidate-smoke.json http://127.0.0.1:18101/v1/chat/completions"
```

Candidate behavior gates must include exact model ID, deterministic output,
reasoning/tool behavior, prefix-cache behavior, capacity/long-context checks,
GPU/RDMA evidence, logs, and error review. A candidate failure leaves
production containers, refs, ports, and locks unchanged. Clean up only exact
candidate-owned containers and roots after evidence is preserved.

## Capture and restore

The lifecycle engine captures a rollback state before `apply`/`update` and
never mutates network policy. A network configuration capture, if required by
an operator, belongs under an external `STATE_ROOT` and must be bounded to
reviewed interface/HCA values. It must not collect credentials, prompts,
responses, unrelated processes, or private logs.
