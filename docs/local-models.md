# Local models

Local models are optional. They can do text-in, text-out helper work such as
triage drafts, summaries, and digests. They do not replace a person or a
security-focused review, and they must never make a security decision.

This guide is for someone who wants to try that helper tier on a workstation or
small team server. Every endpoint, port, model, and context window is a local
choice. Nothing in this guide hard-codes a GPU, port, or model.

## Linux with Ollama

Use [Ollama's Linux installation page](https://docs.ollama.com/linux) and follow
the current vendor instructions. The page provides an installation command that
pipes a script to a shell; this guide prefers reading both the page and the
script before running it. (Vendor documentation read 2026-11-28.)

The Linux documentation names the systemd service `ollama` and places its unit
file under `/etc/systemd/system`. Check it without changing anything. (Source:
[Ollama Linux documentation](https://docs.ollama.com/linux), read 2026-11-28.)

```console
systemctl status ollama
```

Use `systemctl start ollama` or `systemctl enable --now ollama` only if you want
the service started now or at every boot. The exact effects of these generic
systemd commands were not separately verified in Ollama's vendor documentation.
The vendor documents environment overrides with `sudo systemctl edit ollama`
and logs with `journalctl -e -u ollama`. (Source: [Ollama Linux
documentation](https://docs.ollama.com/linux), read 2026-11-28.)

Do not expose the port to networks you do not trust. The vendor pages read on
2026-11-28 do not describe authentication for the local server, so put your own
access control in front of it if you expose it. Ollama's network binding and
authentication behavior remain unverified here. Restrict access with controls
appropriate to your environment, and do not expose the port directly to the
public internet.

The exact model-pull behavior was not verified against vendor documentation for
this review. A commonly used command is:

```console
ollama pull <model-tag>
```

The local tags endpoint and its response behavior were not re-verified against
vendor documentation on 2026-11-28. If your installation provides it, a
read-only check is:

```console
curl --fail --silent --show-error http://127.0.0.1:<port>/api/tags
```

Context length is the maximum number of tokens that the model has access to in
memory. Ollama documents defaults by VRAM: 4K context below 24 GiB, 32K from 24
through 48 GiB, and 256K at 48 GiB or more. It recommends at least 64,000 tokens
for large-context tasks such as web search, agents, and coding tools. Set the
server-wide value with the slider in the Ollama app settings or by serving with
`OLLAMA_CONTEXT_LENGTH=64000 ollama serve`. Use `ollama ps` to inspect its
`CONTEXT` column and processor split after loading. (Source: [Ollama context
length documentation](https://docs.ollama.com/context-length), read
2026-11-28.)

Per-request `num_ctx` semantics and reload behavior were NOT verified in the
vendor documentation reviewed here. Do not rely on either as a rule; check the
effective context with `ollama ps` after loading. A model's trained limit is not
proof that the chosen window fits in available memory.

### Point this edition at Ollama

Today, `config.toml` has one local-model setting:

```toml
[local_model]
context_tokens = 32768
```

The accepted values are `32768` for the default 32K profile and `65536` for the
64K opt-in profile. Whether this value must match a per-request Ollama `num_ctx`
was NOT verified; use `ollama ps` to check the effective context after loading.
The project does not yet have configuration keys or a runtime module for an
endpoint or model name. That work is planned in issue 25 and the
[local-model design](design/local-model-tier.md). Until it lands, this setting
is validated configuration, not a connection from this edition to Ollama.

## macOS with Ollama

Download Ollama from the [vendor download page](https://ollama.com/download) and
follow its current macOS instructions. The download, model-pull, and local API
behavior in this paragraph were not re-verified against vendor documentation on
2026-11-28. Use the same `ollama ps` check described for Linux; there is no
verified exact per-request `num_ctx` rule in this guide.

Apple silicon uses unified memory, so the operating system, applications, model
weights, and context cache all compete for the same pool. Apple silicon was not
part of this project's measurements. Run a representative workload on your own
machine before relying on a model or context size.

Running local models is NOT recommended on any MacBook with less than 32 GB of
memory. An AMD or Nvidia GPU with 8 GB or more of VRAM may start to see real
benefits. Below that, use a hosted service instead.

## LM Studio: the simpler path

[Download LM Studio from its vendor site](https://lmstudio.ai/download) for
macOS, Windows, or Linux. The server can be started from the Developer tab or
with `lms server start`, and the vendor's OpenAI-compatible examples use
`http://localhost:1234/v1`. The documentation says the server can run on
localhost or on the network. (Sources: [LM Studio local server
documentation](https://lmstudio.ai/docs/developer/core/server) and [OpenAI
compatibility documentation](https://lmstudio.ai/docs/developer/openai-compat),
read 2026-11-28.) Do not expose it to networks you do not trust.

The default bind address and the way context length is set at load were NOT
verified. Check the vendor's [server settings
page](https://lmstudio.ai/docs/developer/core/server/settings) and [model load
page](https://lmstudio.ai/docs/developer/rest/load) before relying on either
behavior.

The status display and model-list response behavior were NOT verified against
the vendor documentation reviewed here. Check the Developer tab, and, if the
OpenAI-compatible server is running, probe the conventional model-list path:

```console
curl --fail --silent --show-error http://127.0.0.1:<port>/v1/models
```

Whether LM Studio runs as a system service by default was NOT verified in the
vendor documentation reviewed here. Prefer LM Studio when you want a graphical
workflow, and prefer Ollama when you want the documented Linux systemd service
or a CLI-first workflow. Context-at-load behavior for both products remains
unverified except for Ollama's documented server-wide setting above.

This edition does not yet have endpoint or model-name settings for LM Studio.
Its OpenAI-compatible API is a candidate for the planned local-model module, not
a working integration today.

## Rough hardware sizing guidance

This is rough guidance, not measured by this project yet; the project's
measurements will be published with its evidence write-up, tracked in the
project issues. Model architecture, quantization, context length, and other
workloads all affect what fits and how well it runs.

| Memory or VRAM tier | Rough model size class | Plain advice |
|---|---|---|
| 8 GB | Small models, a few billion parameters | Start small, keep context modest, and verify that the workload stays within memory. |
| 12 GB | Small-to-mid-size models | Expect more choice than at 8 GB, but test the exact model and context together. |
| 16 GB | Mid-size models | Suitable for experimenting with broader model choices; leave room for context and other workloads. |
| 24 GB | Mid-size to larger models | A practical tier for heavier local use, subject to quantization and context requirements. |
| 32 GB | Larger models or more context | Offers useful headroom, but still validate latency, quality, and full-workload memory use. |
| 64 GB | Large models or demanding context | Supports more ambitious workloads; measure the complete workflow before relying on it. |

Every row is rough guidance, not measured by this project yet; the project's
measurements will be published with its evidence write-up, tracked in the
project issues. Do not infer fit from a model's advertised context limit.

Apple marketing has performed an incredible trick: convincing people that memory stops being memory when the case is thin enough and the price has enough commas. It does not. Put a MacBook under a real workload instead of asking it to look expensive beside a latte, and the same laws of computing come back from lunch. I say this as a scorned former Apple user who deployed Xserve and remembers when Apple knew how to do business, not just arithmetic theater. Buy for the workload: 32 GB minimum for a MacBook and 8 GB VRAM on AMD or Nvidia.

## Safety

Treat every model response as untrusted text. Check facts against the source,
validate structured output with code, and have a person review anything that
could change state. Never let a local model's answer decide a security question,
approve access, dismiss a finding, or choose a security control.

Resilience work, including truncation guards and honest fallback reporting, is
tracked in the project issues and the [local-model design](design/local-model-tier.md).

## Troubleshooting

**Model not found:** The behavior of `ollama list` and LM Studio's loaded-model
view was NOT verified against vendor documentation for this review. Use the
product's documented status tools to confirm the exact model identifier.

**Context too small:** Confirm the client setting, then inspect the effective
Ollama value with `ollama ps`. For this edition, `context_tokens` must be either
`32768` or `65536`. Per-request `num_ctx` and reload behavior remain unverified;
do not raise context past the model's trained limit or the memory you have
tested.

**Out of memory:** Stop other model workloads, unload models you do not need, or
choose a smaller model or context. A model fitting at 8K does not prove it fits
at 32K or 64K.

**Slow first token:** Loading or allocation as the cause is unverified vendor
behavior. Repeat one representative request before judging steady-state speed,
and inspect the product's status tools.

**Server unreachable:** Check the service or application first, then run the
read-only model-list request from the same machine as the client. Confirm the
listen address, port, tunnel, and firewall rule. Do not solve a connection error
by exposing the server port to an untrusted network. Network binding and
authentication behavior remain unverified as noted above.
