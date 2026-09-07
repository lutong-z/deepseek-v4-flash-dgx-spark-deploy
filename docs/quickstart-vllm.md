# Quick path: model service from zero

This page is the minimal command sequence for going from a fresh checkout to a
serving two-node DeepSeek V4 model with this repository. It is the compressed
version of the full reviewed flow in
[`deployment.md`](deployment.md), [`image.md`](image.md), and
[`model.md`](model.md); those documents remain authoritative for the gates and
for troubleshooting.

> **Time budget.** A fully cold start is **not** 10 minutes, and no operator
> should trust a claim that it is. The physical floors are:
>
> - the locked model tree is **166.9 GB** and has to reach both nodes' model
>   root (bandwidth-bound);
> - the vLLM+B12X service image is a **~25 GB ARM64** build from the reviewed
>   forks (build is hours; transfer is bandwidth-bound);
> - every production mutation is gated by render, preflight, and an explicit
>   `--confirm` of the printed deployment ID (seconds, but mandatory).
>
> The 10-minute path in this repository is the
> [observability stack](observability.md) (public images only, no model).
> For the model service, use this page to hit the minimum wall-clock time
> possible and to know exactly where the hours go.

## When "quick" actually is quick

The fastest realistic scenario is a **reprovision** or **second site**: model
already staged on the nodes (or a fast local mirror), service image already
loaded, and only the contract/lifecycle steps left. That path is genuinely
minutes and is the bottom section here.

## Cold start, minimized

Prereqs: two reachable DGX hosts, SSH key auth from this machine, Python >=
3.11, and a fast path for the 166.9 GB model (10 GbE LAN copy, direct HF
download, or a pre-staged mirror).

### 1. Install

```bash
git clone <DEPLOY_REPOSITORY_URL> <DEPLOY_CHECKOUT>
cd <DEPLOY_CHECKOUT>
python3 -m venv <PATH>/venv
. <PATH>/venv/bin/activate
python -m pip install -e .
bin/dgx-deploy --help
```

### 2. Model: fetch once, verify against the lock

The lock pins the exact `DeepSeek-V4-Flash-0731` commit and 48 shard
SHA-256s. Fetch into an external root (never inside the checkout):

```bash
# ~166.9 GB; bandwidth-bound. --dry-run shows the plan without touching disk.
python3 scripts/model/fetch.py <MODEL_ROOT> --dry-run
python3 scripts/model/fetch.py <MODEL_ROOT>
python3 scripts/model/verify.py <MODEL_ROOT>
```

`verify.py` writes/checks `<MODEL_ROOT>/.model-lock.sha256` — the exact
SHA-256 of `model.lock.json` bytes — which the deploy stage re-checks.

### 3. Image: build from fork sources, or load a prebuilt one

The reviewed build is one command on a DGX node — the three forks are cloned
at the pinned commits of [`image.fork.lock.json`](../image.fork.lock.json) and
the image is built from source with **zero patches** (every change is
committed in the forks):

```bash
# (a) one-command source build on a DGX node (multi-hour, the reviewed path)
scripts/image/build-fork.sh --work-dir <EXTERNAL_BUILD_DIR> --dry-run
scripts/image/build-fork.sh --work-dir <EXTERNAL_BUILD_DIR>

# (b) image archive already transferred
ssh_dgx DGX-SPARK-0 "sha256sum -c <REMOTE_PATH>/head.tar.sha256 && docker load --input <REMOTE_PATH>/head.tar"
ssh_dgx DGX-SPARK-1 "sha256sum -c <REMOTE_PATH>/worker.tar.sha256 && docker load --input <REMOTE_PATH>/worker.tar"

# (c) registry RepoDigest already published
#     put <PRODUCTION_HEAD_IMAGE_REF> / <PRODUCTION_WORKER_IMAGE_REF> as
#     repository@sha256:<digest> in the external lock; nodes pull on create
```

Details and label requirements are in [`image.md`](image.md). There is no
magic 10-minute build for this profile.

### 4. Environment + lock

```bash
cp .env.example <PATH>/dgx-spark-production/production.env
$EDITOR <PATH>/dgx-spark-production/production.env    # hosts, aliases, roots, model digest, image refs, ALLOW_PUBLIC_API
# write the external deployment lock (schema + example in README "Step 5"
# and config/deployment-lock.schema.json)
bin/dgx-deploy config validate --env-file <PATH>/dgx-spark-production/production.env
```

### 5. Render, plan, dry-run, confirm

```bash
bin/dgx-deploy render \
  --env-file <PATH>/dgx-spark-production/production.env \
  --lock-file <PATH>/dgx-spark-production/production.lock.json \
  --output-dir <PATH>/dgx-spark-production/contracts

bin/dgx-deploy plan \
  --env-file <PATH>/dgx-spark-production/production.env \
  --lock-file <PATH>/dgx-spark-production/production.lock.json

bin/dgx-deploy apply \
  --env-file <PATH>/dgx-spark-production/production.env \
  --lock-file <PATH>/dgx-spark-production/production.lock.json \
  --state-file <PATH>/dgx-spark-production/rollback.json \
  --dry-run

# copy the deployment ID from plan, then
bin/dgx-deploy apply \
  --env-file <PATH>/dgx-spark-production/production.env \
  --lock-file <PATH>/dgx-spark-production/production.lock.json \
  --state-file <PATH>/dgx-spark-production/rollback.json \
  --confirm <DEPLOYMENT_ID>
```

### 6. Verify and smoke

```bash
bin/dgx-deploy verify \
  --env-file <PATH>/dgx-spark-production/production.env \
  --lock-file <PATH>/dgx-spark-production/production.lock.json

ssh_dgx DGX-SPARK-0 "curl --fail --silent http://127.0.0.1:8101/health"
ssh_dgx DGX-SPARK-0 "curl --fail --silent http://127.0.0.1:8101/v1/models"
# then the 1-token smoke request from README "Verification and minimal smoke"
```

## Reprovison / second-site (minutes, not hours)

When model + images are already staged on both nodes:

1. steps 4-6 above only (env + lock + render + plan + apply + verify).
2. That is the "10-minute" model-service path: it is the deploy half of the
   flow, and it is fast because the heavy artifacts are local.

## Where the time actually goes (so you can plan)

| Stage | Typical time | Bound by |
| --- | --- | --- |
| Model fetch 166.9 GB | 20 min – 4 h | network / mirror |
| Service image build/transfer ~25 GB | 1 – 6 h | CPU build, network |
| Env + lock authoring | 10 – 30 min | host facts, image digests |
| render/plan/apply/verify | 5 – 15 min | SSH round-trips, container start |

The observability stack ([`observability.md`](observability.md)) is the part
of this repository that is fully public-image and genuinely 10 minutes; run it
on the same hosts before or after the model service either way.
