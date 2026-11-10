---
surface: routine
headline: Block announced Dessa (2026-10-23), a research-preview full-duplex speech-to-speech model trained on audio only, no weights/API/benchmarks; no Echo-affecting platform change found.
---

## Readout

Routine week. One notable announcement (Block Dessa), one new arXiv paper (DuplexAgent-RSI), small open-source assistant Show HNs. Nothing changes a prior verdict or affects Echo's dependencies. Not above-bar because Dessa is gated (no API, weights, or benchmarks) and all claims are first-party via secondary press.

Instruments tried: HN Algolia (4 queries, all HTTP 200); arXiv API (title lookup worked; a date-sorted multi-term query returned HTTP 301 and was not retried via the redirect, so the arXiv sweep is incomplete); WebSearch x2; GitHub topics page not fetched. Vendor blogs (OpenAI, Google, Amazon, Kyutai) not directly scanned this week: no negative is claimed for them.

## Block Dessa (2026-10-23) [secondary sources only]

- Block's AI research team announced Dessa, a full-duplex speech-to-speech model: audio in, audio out, listens while speaking. Reported as trained only on human conversation audio, no text, no pretrained models, on a single NVIDIA GB200 NVL72 system. Claimed behaviors: learned interruption and backchanneling, pauses, laughter, emphasis. (letsdatascience, techenclave, eesel, voiceaispace; all press/aggregator.)
- Access: small group of AI safety researchers plus public waitlist. No API, pricing, weights, parameter count, or standard benchmarks reported. Demos are first-party and unverified.
- Reported to run faster than real time on an M4 Max-class MacBook Pro [single-source, weak]. If true, it would matter for local stacks like Echo, but with no weights it is not actionable.
- Conflict: one German article says a 7B model derived from Kyutai Moshi; unclear if that refers to Dessa. Unconfirmed; contradicts the "from scratch" claim.
- Could not reach block.xyz primary page via search; verify before relying on any detail.

## arXiv

- DuplexAgent-RSI: Recursive Harness Improvement for Full-Duplex Voice Agent Collaboration, arXiv 2610.11299 (API shows published 2026-10-08; a search result dated it Oct 22, likely a later version/announcement). Abstract: "a full-duplex interaction model stays on the live channel as the entry to the conversation, while search, reasoning, and coding are handled through asynchronous delegation." Pattern is relevant to Echo (live voice front-end plus delegated work); synthesis, not a finding about Echo.

## Platform / ecosystem context (secondary, not new this week)

- OpenAI GPT-Live-1 for ChatGPT Voice (2026-07-08) and Gemini 3.1 Flash Live (reported March 2026) are the established full-duplex baselines per press; already covered in earlier reports. Dessa joins a trend of full-duplex plus async delegation.
- Familiar Labs (full-duplex model for outbound voice agents, BMW i Ventures investment) appears in search results [weak, vendor/investor PR].

## Open source / HN (window since 2026-10-25)

- Hey Spark: open-source laptop voice assistant, any LLM backend (Show HN, 1 point). Anna: machine-load-reporting voice assistant (1 point). Crulon: AI business phone (2 points). Hurston-1: open-source TTS for Southern American / Afro-English speech (68 points); TTS-only, relevant to voice accessibility/dialect coverage. All low-signal.
- "speech-to-speech" query returned zero hits.

## Echo relevance

None direct. Watch items: Dessa weights/API if ever released; the delegation pattern in DuplexAgent-RSI.

## Open questions

- Dessa primary announcement and whether Moshi-derived.
- Full arXiv sweep for 2026-10-26 to 11-02 not completed.

## Follow-ups

- Dessa weights watch: re-check Block's Dessa for weights/API/benchmarks next dispatch.

## Sources

- https://letsdatascience.com/news/block-announces-dessa-a-speech-to-speech-ai-model-trained-on-93e9a803 : aggregator news on Dessa
- https://techenclave.com/news/items/block-shows-dessa-a-voice-model-trained-entirely-on-human-conversation : press on Dessa
- https://www.eesel.ai/blog/block-dessa : vendor blog explainer
- https://www.voiceaispace.com/articles/block-launches-dessa-a-full-duplex-speech-model-trained-only-on-audio : trade press
- https://pasqualepillitteri.it/de/news/26879/dessa-block-sprach-ki-full-duplex : German blog, Moshi-derived claim [weak]
- https://arxiv.org/abs/2610.11299 : primary, DuplexAgent-RSI
- https://www.bmwiventures.com/news/voice-interactions-that-feel-familiar-why-we-invested-in-familiar-labs : investor post [weak]
- https://hn.algolia.com/api/v1/search : HN queries
- https://github.com/contextuai/hey-spark , https://huggingface.co/destined/hurston-1 : HN-linked projects
