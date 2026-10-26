---
symptom: "Ollama /api/generate silently truncates a long prompt to the default context; large inputs are answered on a fragment with no error"
fix: "pass options.num_ctx explicitly (e.g. 16384) sized to prompt + model limit + VRAM; the default context does NOT scale to prompt length"
domain: "ollama"
---
## Symptom
A local Ollama call (POST /api/generate or /api/chat) sending a long prompt (a repo digest, a big document) returns a result that reads as if it only saw the first chunk: low/wrong scores, "part of a larger application" summaries, rankings INVERTED so the biggest inputs score lowest. No error, no warning.

## Cause
Ollama defaults `num_ctx` (context window) to a small value (~2k-4k) and SILENTLY truncates the prompt to fit before the model sees it. It does not scale to the prompt length or the model max. A 10k-token digest is cut to ~4k even though the model (e.g. qwen2.5-coder) supports 32k. Bumping the app-side input size does nothing while `num_ctx` stays at the default.

## Fix
Set `num_ctx` explicitly in request options, sized to prompt + model window + VRAM (KV cache grows with `num_ctx`):

```python
payload = {
    "model": m,
    "prompt": p,
    "stream": False,
    "options": {"temperature": 0.1, "num_ctx": 16384},
}
```

An A2000 12GB fits `num_ctx=16384` KV plus a 14b-q4 (~9GB).

## Verification
followme grading on gpu-ml (2026-09-20): with default `num_ctx`, the operator's largest repos scored 1/1 with fragment descriptions; `num_ctx=16384` flipped them to accurate scores (Echo 1/1→8.5/8.0, MoD 1/1→8.5/8.0, a false-positive security flag cleared). Re-runnable: grade a >4k-token digest with and without `num_ctx` set and diff the scores/description.

## Discovered
- 2026-09-20, followme eval (specialist); `knowledge/episodic/2026-09-20-followme-eval-scope-and-review.md` (RUN-3); fix diff `knowledge/episodic/2026-09-20-followme-grading-fix.diff`

## Keywords
ollama, num_ctx, context-window, truncation, silent-failure, local-inference, llm, digest, qwen, api/generate, options, prompt-too-long, gpu-ml, cua-s1
