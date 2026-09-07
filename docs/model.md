# Model lock, fetch, and verification

`model.lock.json` is the only model source of truth. It pins the public
`deepseek-ai/DeepSeek-V4-Flash-0731` tree to revision
`7872f01b1d1fe23eabc4c98b48bffcef5a386062`, including the 54-file allowlist, 48
safetensors shards, sizes, and hashes. The lock contains no weights or token.

The runtime layout is deliberately exact and consistent across fetch,
verification, configuration, and Docker:

```text
<MODEL_ROOT>/
  config.json
  generation_config.json
  LICENSE
  model.safetensors.index.json
  tokenizer.json
  tokenizer_config.json
  model-*.safetensors
  .model-lock.sha256
```

`MODEL_ROOT` is the **model directory itself**, named
`DeepSeek-V4-Flash-0731`. The container mounts that directory read-only at
`/models/DeepSeek-V4-Flash-0731`, and the rendered vLLM command uses exactly
that path. Do not point `MODEL_ROOT` at its parent `<REMOTE_PATH>/models`.

For manual node diagnostics, define a strict SSH wrapper using the same
operator-owned key and known-hosts file as the deploy environment:

```bash
ssh_dgx() {
  ssh -T -o BatchMode=yes -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile=<PATH>/known_hosts -o ConnectTimeout=10 \
    -i <PATH>/ssh-key "$@"
}
```

## Disk, permissions, and client preflight

The model is approximately 166.9 GB. On each DGX node, confirm storage,
permissions, and a real directory before downloading:

```bash
ssh_dgx DGX-SPARK-0 "df -h <REMOTE_PATH> && test -d <REMOTE_PATH> && test -w <REMOTE_PATH>"
ssh_dgx DGX-SPARK-1 "df -h <REMOTE_PATH> && test -d <REMOTE_PATH> && test -w <REMOTE_PATH>"
ssh_dgx DGX-SPARK-0 "mkdir -p <REMOTE_PATH>/models && test -d <REMOTE_PATH>/models"
ssh_dgx DGX-SPARK-1 "mkdir -p <REMOTE_PATH>/models && test -d <REMOTE_PATH>/models"
```

Install the pinned client version on each node or in the fetch environment:

```bash
python3 -m pip install 'huggingface_hub==0.34.4'
python3 -c 'import huggingface_hub; assert huggingface_hub.__version__ == "0.34.4"; print(huggingface_hub.__version__)'
```

Do not pass a token to these commands. The fetcher sets
`HF_HUB_DISABLE_IMPLICIT_TOKEN=1` and rejects token/configuration injection.

## Lock-byte digest and marker

The marker is the SHA-256 of the exact bytes of this checkout's
`model.lock.json`, not a digest of a directory, archive, or downloaded file.
Compute it before copying a value into the external deployment environment:

```bash
sha256sum model.lock.json
```

The output must equal `MODEL_MANIFEST_SHA256` and the contents of
`<MODEL_ROOT>/.model-lock.sha256` on **both** DGX nodes, with one lowercase
hex digest followed by a newline. The deployment engine stages this marker as
part of contract preparation and checks it before container creation.

## Fetch on both DGX nodes

The fetch destination must not already exist; the fetcher creates a sibling
staging directory and atomically installs a verified model tree. Run the same
reviewed lock and destination layout on each node:

```bash
ssh_dgx DGX-SPARK-0 "python3 -m pip install 'huggingface_hub==0.34.4'"
ssh_dgx DGX-SPARK-1 "python3 -m pip install 'huggingface_hub==0.34.4'"
ssh_dgx DGX-SPARK-0 "python3 <DEPLOY_CHECKOUT>/scripts/model/fetch.py <REMOTE_PATH>/models/DeepSeek-V4-Flash-0731 --dry-run"
ssh_dgx DGX-SPARK-1 "python3 <DEPLOY_CHECKOUT>/scripts/model/fetch.py <REMOTE_PATH>/models/DeepSeek-V4-Flash-0731 --dry-run"
ssh_dgx DGX-SPARK-0 "python3 <DEPLOY_CHECKOUT>/scripts/model/fetch.py <REMOTE_PATH>/models/DeepSeek-V4-Flash-0731"
ssh_dgx DGX-SPARK-1 "python3 <DEPLOY_CHECKOUT>/scripts/model/fetch.py <REMOTE_PATH>/models/DeepSeek-V4-Flash-0731"
```

The `--dry-run` path reads and validates the lock, prints the exact repository,
revision, allowlist, and size plan, and makes no network request. A real fetch
uses the pinned `huggingface_hub` client and the exact 54-file allowlist. Run
no fetch until storage, network, and operator approval are ready.

## Full 54-file verification on both nodes

Verify the entire installed tree—not only the marker—on each node:

```bash
ssh_dgx DGX-SPARK-0 "python3 <DEPLOY_CHECKOUT>/scripts/model/verify.py <REMOTE_PATH>/models/DeepSeek-V4-Flash-0731"
ssh_dgx DGX-SPARK-1 "python3 <DEPLOY_CHECKOUT>/scripts/model/verify.py <REMOTE_PATH>/models/DeepSeek-V4-Flash-0731"
```

Verification checks all 54 locked files, regular-file types, symlink/path
escapes, exact sizes, SHA-256 LFS objects, Git-blob metadata hashes, config,
tokenizer, index/shard closure, generation defaults, absence of a Jinja chat
template, and the exact marker. It must pass independently on both nodes.

The deploy engine additionally checks these required runtime files on both
nodes before mutation:

```text
<MODEL_ROOT>/config.json
<MODEL_ROOT>/model.safetensors.index.json
<MODEL_ROOT>/tokenizer.json
<MODEL_ROOT>/tokenizer_config.json
<MODEL_ROOT>/.model-lock.sha256
```

A missing file, marker mismatch, unexpected file, hash change, or model-root
layout mismatch stops the deployment. Verification never repairs an installed
model.

## Troubleshooting

- If fetch says the destination exists, choose a new reviewed destination; it
  will not overwrite an existing tree.
- If the marker differs, recompute `sha256sum model.lock.json` from the exact
  checkout used by the deploy operator and copy only that digest.
- If one node passes and the other fails, stop deployment and repair the
  failing node; do not bypass the two-node check.
- If the runtime reports model-not-found, confirm `MODEL_ROOT` is the exact
  `DeepSeek-V4-Flash-0731` directory and that the generated mount destination
  and vLLM model path are both `/models/DeepSeek-V4-Flash-0731`.
- If the client cannot reach Hugging Face, rerun both dry-runs and inspect
  network/proxy policy; never enable implicit token use or substitute a tag.
