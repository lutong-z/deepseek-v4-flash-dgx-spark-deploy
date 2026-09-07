# Production evidence

This document records observed, verifiable production telemetry for the
DeepSeek V4 two-node service managed by this repository. Every number below
was read from the live Prometheus instance (`job="vllm"`, scrape interval
15&nbsp;s) that is deployed by
[`scripts/observability/stack.py`](../scripts/observability/stack.py), or from
container/sysfs state on the DGX hosts. No value is estimated or extrapolated.

The purpose is to establish, in one reproducible place, that the reviewed
profile (B12X MLA, NVFP4 DS MLA KV, DSpark K5 speculative decoding, TP2 over
two ARM64 GB10 nodes) has carried real production traffic without failure, and
to give future operators an honest baseline for what "healthy" looks like.

## Deployment under test

| Item | Value |
| --- | --- |
| Profile | `dsv4-native432-b12x-tp2` (see [`config/profiles/`](../config/profiles/)) |
| Service name | `deepseek-v4-flash-0731-native432` |
| Model | `DeepSeek-V4-Flash-0731`, 48 safetensors shards, locked in [`model.lock.json`](../model.lock.json) |
| Topology | 2 × ARM64 GB10, TP2, worker (rank 1) before head (rank 0) |
| Engine | vLLM + B12X attention/MoE/linear, DSpark K5, 5 speculative tokens |
| API | `head:8101`, `GET /metrics` exported, `GET /health` + `/v1/models` |
| Observability | Prometheus + Grafana + Loki + Alertmanager (this repository's stack) |

## Snapshot window

The figures below cover a rolling **24-hour** window ending at
2026-09-07T12:30Z, with the container `dsv4-native432-lmcache-head` running
continuously since **2026-09-06T07:24:28Z** (restart count 0, no restart
since start). The single down sample in `up{job="vllm"}` corresponds to the
deployment switch itself (07:23:30Z–07:27:00Z), i.e. planned container
replacement, not an incident. No unplanned outage was observed in the 7-day
lookback available in Prometheus (`up{job="vllm"}` returns to 1 immediately
after the switch and stays 1 for the remaining 6+ days).

## Quality of service

| Metric | Value | Meaning |
| --- | --- | --- |
| Successful requests (24h) | **≈ 3,990** | `sum(increase(vllm:request_success_total[24h]))` |
| Failed requests (24h) | **0** | `sum(vllm:request_failed_total)` is absent/zero |
| Preemptions | **0** | `sum(vllm:num_preemptions_total)` |
| Max concurrent running requests (24h) | **8** | `max_over_time(vllm:num_requests_running[24h])` |
| Requests waiting (p95, 24h) | **1** | `quantile_over_time(0.95, vllm:num_requests_waiting[24h])` |
| Queue time (avg, 1h) | 0.91 s | `sum(rate(vllm:request_queue_time_seconds_sum[1h])) / sum(rate(vllm:request_queue_time_seconds_count[1h]))` |
| Time to first token (avg, 1h) | 3.02 s | `sum(rate(vllm:time_to_first_token_seconds_sum[1h])) / sum(rate(vllm:time_to_first_token_seconds_count[1h]))` |
| Inter-token latency (avg, 1h) | 145 ms | `sum(rate(vllm:inter_token_latency_seconds_sum[1h])) / sum(rate(vllm:inter_token_latency_seconds_count[1h]))` |
| E2E request latency (avg, 1h) | 38.1 s | `sum(rate(vllm:e2e_request_latency_seconds_sum[1h])) / sum(rate(vllm:e2e_request_latency_seconds_count[1h]))` |
| Engine sleep state | `awake` | `vllm:engine_sleep_state{sleep_state="awake"} == 1` |

Reason for the long e2e average: the workload is dominated by long prompts.
Input tokens outnumber output tokens ≈ 110:1 (see throughput below), so most of
the e2e time is prefill of a large prompt, not generation. Queue time is
sub-second and no request waits at capacity, so the tail is bounded by prompt
size rather than by scheduler backlog.

## Throughput

| Metric | Value |
| --- | --- |
| Input tokens (24h) | **≈ 457 M** (`sum(increase(vllm:prompt_tokens_total[24h]))`) |
| Output tokens (24h) | **≈ 4.14 M** (`sum(increase(vllm:generation_tokens_total[24h]))`) |
| Speculative decode acceptance (1h) | ≈ 47% accepted / drafted |
| Drafts (1h) | ≈ 16.6 drafts/s |

## Caching (the architecture's point)

| Metric | Value |
| --- | --- |
| Prompt-token cache hit ratio (1h) | **≈ 98.0 %** (`sum(rate(vllm:prompt_tokens_cached_total[1h])) / sum(rate(vllm:prompt_tokens_total[1h]))`) |
| Internal prefix-cache hit ratio (1h) | ≈ 93.6 % (`vllm:prefix_cache_*`) |
| External prefix-cache hit ratio (1h) | ≈ 68.5 % (`vllm:external_prefix_cache_*`) |
| L2 footprint (current) | ≈ 87.9 GB (`dgx_lmcache_l2_bytes`) |

96–98 % of input tokens are served from cache. This is the primary cost lever
of the layered prefix-cache design (GPU block cache + external persistent L2)
and matches the intent of the rollout.

## Fabric / RDMA health

| Metric | Value |
| --- | --- |
| `dgx_fabric_gid_valid` (both nodes) | 1 |
| `dgx_fabric_link_up` (both nodes) | 1 |
| `dgx_fabric_rdma_link_up` (both nodes) | 1 |
| `dgx_fabric_rocev2_gid` (both nodes) | 1 |
| `dgx_fabric_mtu` | 9000 |
| `link_downed` (RDMA counters, both nodes) | 0 |

`dgx_fabric_link_flaps` (`carrier_changes`) counts slowly increase over hours
(≈ 1436 total at snapshot) while link state stays `up` and `link_downed` stays
0; this matches a long-lived Active carrier with occasional transient carrier
changes and is not an RDMA link failure. Track the trend if you care about NIC
health, but it has not caused a single vLLM request failure.

## How to reproduce this evidence

All of it comes from the observability stack this repository deploys:

```bash
# 1. Deploy the stack (also covers /metrics, rules, dashboard). See docs/observability.md
bin/dgx-deploy --help   # or: python scripts/observability/stack.py --help
```

```bash
# 2. Query Prometheus on the head node (default port 19090)
curl -s 'http://<head>:19090/api/v1/query' \
  --data-urlencode 'query=sum(increase(vllm:request_success_total[24h]))'
```

The exact metric names, the dashboard the stack provisions, and the alert
rules are versioned in this repository under
[`scripts/observability/`](../scripts/observability/), so the evidence is
reproducible against the same revision instead of an opaque external service.

> Scope note: numbers are a point-in-time snapshot, not a continuous SLA.
> They prove the profile can carry this traffic pattern for a day without
> failures; they do not guarantee the same under every workload. Use
> `docs/deployment.md` + this repository's tests as the contract, and keep
> the Prometheus/Grafana stack running to verify your own deployment.
