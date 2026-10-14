# Hardware-aware suggestions

Which local models fit this computer, and where a local model could take work from a paid one.

[← Back to the README](../README.md)

---

```bash
python -m trioditis hardware          # readable report
python -m trioditis hardware --json   # same data as GET /v1/hardware
```

Trioditis detects your CPU, RAM and GPU (NVIDIA via `nvidia-smi`, AMD via `/sys`, Apple Silicon via `sysctl`; stdlib only, every probe fails soft), lists the models installed in your local Ollama, and suggests per route where a local model could take work from a paid cloud model — with the reasoning spelled out:

- **Memory budget:** dedicated VRAM minus ~10% on a discrete GPU; ~65-75% of unified memory on Apple Silicon; otherwise 70% of RAM while keeping at least 4 GB for the OS (integrated GPUs share that RAM, so it is counted once). A model needs roughly its file size +20% for context.
- **Cost first:** routes that start with a cloud model get a "put the local model first" suggestion (cloud stays as fallback); routes without a usable local model get the best one that fits, preferring models you already have (zero download). Configured models that won't fit are flagged with a smaller alternative.
- **Honest numbers:** catalog sizes are approximate (check with `ollama show`); cloud prices are quoted only if `~/.hlidskjalv/prices.json` exists.

It prints YAML to paste and never edits `config.yaml`. Hardware facts never leave the machine: the only calls are `GET /api/tags` to your local providers. `GET /v1/hardware` sits behind the same API key as every other endpoint.
