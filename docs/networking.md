# Networking and RoCE

Control and data planes are separate. `HEAD_HOST` and `WORKER_HOST` are
explicit SSH addresses used only for control. `HEAD_NODE_ADDR`,
`WORKER_NODE_ADDR`, and `MASTER_ADDR` are explicit RoCE addresses rendered
into vLLM and NCCL environment values.

## Read-only preflight

A future preflight must verify, on both nodes:

- interface presence and operational state;
- expected address, link speed, MTU, active port, and RDMA device/HCA mapping;
- reviewed RoCEv2 GID/index and free master/API ports;
- GPU, driver, and container-runtime readiness;
- cross-node TCP reachability and absence of unexpected socket fallback.

Preflight is read-only. It must never guess an interface, change a route, or
modify switch, VLAN, PFC/ECN, firmware, DHCP/DNS, or firewall policy.

## Capture and restore

A future capture command may write a bounded before-manifest under external
`STATE_ROOT`, containing only the interface/HCA settings required for restore.
It must not collect unrelated environment, process, prompt, response, or
metric data. Apply and restore remain separate operations requiring a matching
manifest, explicit per-node fields, preflight, and both mutation confirmations.

Forwarding must bind local loopback only. No launchd, OMP, hidden daemon,
broad bind, or arbitrary remote command hook belongs in this repository.
