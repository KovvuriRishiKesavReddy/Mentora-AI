# Known limitations (stated honestly)

* **No real accounts.** Each browser gets a random id that separates its data from other browsers. Anyone who can copy that id from the browser can see that data. Add proper login before exposing the server publicly.
* **Scanned / image-only PDFs** contain no text; they are rejected with a clear message. OCR is not included.
* **Retrieval quality depends on the embedder.** With the real MiniLM model the thresholds (0.25 weak / 0.40 strong) are untuned defaults — calibrate with `scripts/calibrate_threshold.py` and the evaluation harness on your own notes. The offline fallback (used when the model cannot be downloaded) is lexical only.
* **Summaries and flashcards are model-written.** They are instructed to use only your text and are built chapter by chapter, but a model can still compress or mis-phrase; the mind map and click-to-open sources let you check against the original.
* **Free-tier limits.** Summarising a long document makes many model calls (bounded by `SUMMARY_MAX_CALLS`, default 40). It is generated once and cached.
* **Voice mood features** (pitch/pace/pauses) are rough signals, not emotion recognition.
* **Mood detection** is keyword- and similarity-based; it is shown with its confidence and never changes facts.
* **Single-server design.** SQLite and local files; not built for many simultaneous users.
