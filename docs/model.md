# Model lock, fetch, and verification

## Pinned source

`model.lock.json` is the reviewed source of truth for
`deepseek-ai/DeepSeek-V4-Flash-0731` at the immutable revision
`7872f01b1d1fe23eabc4c98b48bffcef5a386062`. The public Hugging Face model API
reports the MIT license, `transformers` library, `deepseek_v4` model type, and
`DeepseekV4ForCausalLM` architecture. The lock was recorded from the public
model and tree API metadata on 2026-09-02; it does not contain model data or a
credential.

The allowlist contains 54 files: the model config, generation config, license,
index, two tokenizer files, and 48 safetensors shards. The selected files total
166,898,509,296 bytes (the weight shards account for 166,886,535,336 bytes).
The 48 LFS objects have their public SHA-256 OIDs and sizes locked. For small
non-LFS files, the public tree API exposes Git blob SHA-1 OIDs instead; the
verifier computes the Git blob digest from the installed bytes. No hash is
invented when the public API does not expose it.

The lock fixes the official generation defaults:

- `do_sample: true`
- `temperature: 1.0`
- `top_p: 1.0`

The tokenizer configuration must have no `chat_template`; the lock explicitly
requires the absence of a Jinja template.

## Fetching (not run by this repository)

The model is about 166 GB. Do not run the fetch command unless the destination
has the required storage, network access, and operator approval:

```bash
python3 scripts/model/fetch.py /srv/models/deepseek-v4 --dry-run
python3 scripts/model/fetch.py /srv/models/deepseek-v4
```

`--dry-run` reads and validates the lock, prints the exact repository, commit,
allowlist, and size plan, and makes no network request. A real fetch requires
the optional `huggingface_hub` package (for example,
`python3 -m pip install huggingface_hub`) and calls `snapshot_download` with
the full revision and an exact list of the 54 locked paths. Implicit Hugging
Face token use is disabled; there is no credential argument, credential file,
or secret output in this repository. Network/client failures leave no
installed model.

The destination must be an absolute directory outside this checkout and must
not already exist. Downloading occurs in a unique sibling staging directory.
The fetched tree is checked for symlinks, path escapes, missing/extra files,
regular-file types, sizes, hashes, JSON metadata, and index/shard closure. Only
after verification is complete is `.model-lock.sha256` written and the sibling
directory atomically renamed into place. Existing destinations are never
replaced.

## Read-only verification

Verify an already-installed tree without network access:

```bash
scripts/model/verify.sh /srv/models/deepseek-v4
# equivalent:
python3 scripts/model/verify.py /srv/models/deepseek-v4
```

Verification reads the lock and requires the marker to equal the SHA-256 of
the exact `model.lock.json` bytes. It rejects symlinks, unexpected files,
missing files, size/hash changes, unsafe paths, invalid config/tokenizer/index
JSON, index references outside the 48-shard set, generation-default changes,
and a Jinja chat template. It does not repair or modify an installed model.

This change only establishes the public lock and local fetch/verify boundary.
It does not download weights, publish artifacts, or change production state.
