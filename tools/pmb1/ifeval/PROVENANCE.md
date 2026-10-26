# Vendored IFEval checker: provenance (T-2518 Track B)

Original work: google-research/instruction_following_eval (Copyright 2023 The Google Research Authors,
Apache License 2.0, `LICENSE-Apache-2.0`). This copy is taken from EleutherAI's lm-evaluation-harness port,
**lm_eval 0.4.8** wheel (`lm_eval-0.4.8-py3-none-any.whl`, sha256
`2ba377e067affcfcd27b73d6c045f8ce9ff88accd56176d9a86af3c12b0e01f6`; the wheel is MIT, `LICENSE-MIT-lm_eval`),
path `lm_eval/tasks/ifeval/`. Downloaded 2026-10-17.

| File here | Original sha256 (as extracted from the wheel) | License |
|---|---|---|
| `instructions.py` | `2d9670e538e6201665363b9db156fb2c27016b9287eb9790ea55d3520036bb00` | Apache-2.0 (Google header kept) |
| `instructions_registry.py` | `9db1c062cdb70a91420d3789a588235a1064c2b9a1a66e09e5fb9df9e7584a6c` | Apache-2.0 (Google header kept) |
| `instructions_util.py` | `e8c4d9187bac1482d93941fb46469609c3ae78d896195bfcb300a0707d492567` | Apache-2.0 (Google header kept) |
| `utils.py` | `1ab8f14808c826f93f2364883487ed63cf4267980bf4761fda8053899c013632` | MIT (EleutherAI), adapting Google's Apache-2.0 `evaluation_lib.py` |
| `ifeval_input_data.jsonl` | `6a85310ca8ce15eff755aa08a3a4ff931c7e273e7515ebb3c492ea85fd8288f2` | huggingface.co/datasets/google/IFEval (dataset card states Apache-2.0; not re-checked offline); 541 records, unmodified |

## Every edit (the rest is byte-identical to the original)

1. `instructions.py:27`, `instructions_registry.py:17`, `utils.py` (import line): `from lm_eval.tasks.ifeval import X`
   became `from . import X`, so the package imports without lm_eval.
2. `instructions_util.py`: upstream `download_nltk_resources()` and its import-time call (original lines 36-52) called
   `nltk.download("punkt_tab")` when punkt_tab was missing. Replaced by `require_nltk_resources()`, which keeps the
   nltk >= 3.9.1 version assert and raises `NltkResourceMissing` (a new class) when `tokenizers/punkt_tab/english/`
   is not installed. It NEVER touches the network. The now-unused `RANK` constant is kept as upstream had it.
3. `utils.py`: a 4-line comment header naming source and licenses (upstream had none).
4. Added: `__init__.py`, this file, `LICENSE-Apache-2.0` (Debian's `/usr/share/common-licenses/Apache-2.0`),
   `LICENSE-MIT-lm_eval` (the wheel's `LICENSE.md`).

## Runtime notes (not edits)

* Deps: nltk >= 3.9.1 with punkt_tab, langdetect, immutabledict, packaging. Nothing is downloaded at import time.
  `count_sentences` loads `nltk:tokenizers/punkt/english.pickle`, which nltk 3.9.1 serves from punkt_tab.
* `tools/pmb1/trackb.py` seeds `langdetect.DetectorFactory.seed = 0` before scoring (langdetect is otherwise
  nondeterministic) and disables logging while scoring, because `instructions.py` logs a response's text at ERROR
  level when language detection fails.
