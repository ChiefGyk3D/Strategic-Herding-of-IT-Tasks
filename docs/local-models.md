# Local models

Local models are optional. They can do text-in, text-out helper work such as
triage drafts, summaries, and digests. They do not replace a person or a
security-focused review, and they must never make a security decision.

This guide is for someone who wants to try that helper tier on a workstation or
small team server. Every endpoint, port, model, and context window is a local
choice. Nothing in this guide hard-codes a GPU, port, or model.

## Linux with Ollama

Use [Ollama's Linux installation page](https://docs.ollama.com/linux) and follow
the current vendor instructions. This guide deliberately does not copy a
pipe-to-shell installer command.

The Linux package can run Ollama as a systemd service. Check it without changing
anything:

```console
systemctl status ollama
```

Use `systemctl start ollama` or `systemctl enable --now ollama` only if you want
the service started now or at every boot. Keep its listen address on loopback
unless another machine truly needs it.

Ollama has no built-in authentication. Exposing its port beyond localhost lets
any client that can reach it make requests and load models. For remote use,
prefer an authenticated tunnel or VPN that leaves Ollama bound to loopback. If
you must bind it to another interface, restrict the source addresses with the
host firewall and do not expose it to the public internet. An authenticating
reverse proxy is another option, but the proxy must be the only network path to
Ollama.

Pull the model tag you selected:

```console
ollama pull <model-tag>
```

Check the local server with the read-only tags endpoint. Replace the port if
your installation uses a different one:

```console
curl --fail --silent --show-error http://127.0.0.1:<port>/api/tags
```

Set the context window explicitly when you load or create the model. For
example, an Ollama `Modelfile` can contain:

```text
FROM <model-tag>
PARAMETER num_ctx <context-tokens>
```

Create a local name from that file with `ollama create <local-name> -f
<modelfile>`. A client must send the same `num_ctx` that the model was loaded
with. Sending a different value can reload the model and change its memory use.
The model's trained limit is not proof that the chosen window fits in available
memory.

### Point this edition at Ollama

Today, `config.toml` has one local-model setting:

```toml
[local_model]
context_tokens = 32768
```

The accepted values are `32768` for the default 32K profile and `65536` for the
64K opt-in profile. This value must match the `num_ctx` loaded by the server.
The project does not yet have configuration keys or a runtime module for an
endpoint or model name. That work is planned in issue 25 and the
[local-model design](design/local-model-tier.md). Until it lands, this setting
is validated configuration, not a connection from this edition to Ollama.

## macOS with Ollama

Download Ollama from the [vendor download page](https://ollama.com/download) and
follow its current macOS instructions. Pull a model with `ollama pull
<model-tag>`, then use the same read-only `/api/tags` check and exact `num_ctx`
rule described for Linux.

Apple silicon uses unified memory, so the operating system, applications, model
weights, and context cache all compete for the same pool. Apple silicon was not
part of this project's measurements. Run a representative workload on your own
machine before relying on a model or context size.

Running local models is NOT recommended on any MacBook with less than 32 GB of
memory. If you have an AMD or Nvidia GPU with 8 GB or more of VRAM you may start
to see real benefits; below that, use a hosted service instead.

## LM Studio: the simpler path

[Download LM Studio from its vendor site](https://lmstudio.ai/download) for
macOS, Windows, or Linux. In the application, download and load a model, choose
its context size, and start the local server. LM Studio presents an
OpenAI-compatible endpoint on localhost. Keep it on loopback unless you apply
the same tunnel, firewall, and authentication protections described above.

Confirm that the server is listening and the intended model is loaded by using
LM Studio's server status in the application and a read-only model-list request:

```console
curl --fail --silent --show-error http://127.0.0.1:<port>/v1/models
```

LM Studio is a GUI application and does not run as a system service by default.
Its model-loading screen controls the context allocation, while Ollama commonly
gets that value from a model definition or request option. Prefer LM Studio when
you want to compare models interactively or avoid managing a background service.
Prefer Ollama when you want a service that starts with the system or a CLI-first
workflow.

This edition does not yet have endpoint or model-name settings for LM Studio.
Its OpenAI-compatible API is a candidate for the planned local-model module, not
a working integration today.

## Hardware sizing from project measurements

These are fit results, not shopping advice or a quality guarantee. The project
measured two discrete-GPU tiers. It did not measure Apple silicon or the other
memory tiers in this table. Model size means parameter class, and the context is
the largest measured context that stayed fully on the GPU for at least one model
in that class.

| Memory or VRAM tier | Model size class that fit | Measured context that fit | Evidence |
|---|---:|---:|---|
| 8 GB VRAM | 4B | 65,536 tokens | `Largest context fully on GPU, 8 GB tier` in the public tables |
| 12 GB VRAM or memory | not measured | not measured | No project measurement |
| 16 GB VRAM | 12B | 65,536 tokens | `Largest context fully on GPU, 16 GB tier` in the public tables |
| 24 GB VRAM or memory | not measured | not measured | No project measurement |
| 32 GB VRAM or memory | not measured | not measured | No project measurement |
| 64 GB VRAM or memory | not measured | not measured | No project measurement |

The 8 GB class also measured 8B and 9B models, but their context ceilings varied
from 8,192 to 16,384 tokens. The 16 GB class measured 14B models at 32,768
tokens. Those results appear in `Largest context fully on GPU, 8 GB tier` and
`Largest context fully on GPU, 16 GB tier`. The speed and memory figures behind
the fit calls are in `Speed, VRAM and fit, 8 GB tier`, `Speed, VRAM and fit, 16
GB tier`, and `speed_vram_fit.csv`. Do not infer an unmeasured tier from a model's
advertised context limit.

<!-- hardware-humor: pending owner approval (issue 90) -->

## Safety

Treat every model response as untrusted text. Check facts against the source,
validate structured output with code, and have a person review anything that
could change state. Never let a local model's answer decide a security question,
approve access, dismiss a finding, or choose a security control.

Resilience work, including truncation guards and honest fallback reporting, is
tracked in the project issues and the [local-model design](design/local-model-tier.md).

## Troubleshooting

**Model not found:** Run `ollama list` or check LM Studio's loaded-model view.
Pull or load the exact model tag your client requests. A similar name is not the
same model.

**Context too small:** Confirm the client setting and the value used when the
model was loaded. For this edition, `context_tokens` must be either `32768` or
`65536`, and it must match Ollama's `num_ctx`. Do not raise it past the model's
trained limit or the memory you have measured.

**Out of memory:** Stop other model workloads, unload models you do not need, or
choose a smaller model or context. A model fitting at 8K does not prove it fits
at 32K or 64K.

**Slow first token:** The server may be loading model weights or allocating the
context cache. Repeat one representative request after the model is resident
before judging steady-state speed.

**Server unreachable:** Check the service or application first, then run the
read-only model-list request from the same machine as the client. Confirm the
listen address, port, tunnel, and firewall rule. Do not solve a connection error
by exposing an unauthenticated server to the internet.
