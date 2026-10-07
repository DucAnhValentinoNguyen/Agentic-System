# Twin: system card

A model card describes what a model is for, what it was built from, how it was evaluated and where it fails. Twin does not
train a model, so this is a card for the *system* (prompts, retrieval, routing, tools) around third-party models.

## Intended use
Answer questions about Duc-Anh Nguyen from the text of his portfolio site, with a link to the source section; book or extend
a call on his calendar; pass on a message. Public, anonymous, English and German.

**Not for:** decisions about people, advice, anything not on the site (it is built to say so), handling sensitive personal data.

## Models (none trained or fine-tuned by this project)
| Role | Model | Where |
|---|---|---|
| Classify, plan, extract fields | `gemini-2.5-flash-lite` | Vertex AI, EU |
| Write the answer | `gemini-2.5-flash` | Vertex AI, EU |
| Embeddings for retrieval | `text-embedding-005` | Vertex AI, EU |
| Fallback when Vertex throttles | `gpt-oss-20b` / `gpt-oss-120b` | Groq |
| Judge for offline and online scoring | `gpt-oss-120b` (a different family from the answerer) | Groq |
| Voice transcription | Gemini (audio input) | Vertex AI, EU |

## Data
The only knowledge source is `ingest/corpus.jsonl`, built from the site's own text. No visitor data is used to train anything.
Messages are logged to Firestore (turns, audit, counters) with a 30-day retention; voice recordings are held in memory for the
request only and never stored.

## Evaluation (details in `docs/experiment.md`)
- Held-out factual questions, plain RAG vs RAG plus claim verification: no detectable benefit from verification, so it is off.
- Research mode (plan, several searches, reflect): recall gain +0.125 in one run and +0.049 in another; used for complex questions only.
- 35-case adversarial suite (injection, prompt extraction, fabrication traps, off-topic, on-topic-without-a-name): run as a gate on every deploy.
- Judge versus 47 human labels: 94% raw agreement, Cohen's kappa about 0, both judge flags false alarms; recall not yet measurable.
- Voice: 20 synthetic spoken questions, word error rate 11.5% after the final prompt (synthetic speech, an upper bound).

## Known limitations
- Abstention is not perfect: a question that shares a word with unrelated content can get a related fact instead of "not on the site"
  (about 1 in 20 on one tested question).
- The judge is not yet validated for recall; its absolute rates should not be quoted.
- Sample sizes are small (24 held-out questions, one corpus); intervals are wide.
- Conversation state lives in memory, so the service runs as a single instance.
- Voice is push-to-talk (about 2 s), not a live call. German and product names are transcribed less accurately.
- Text from visitors reaches the calendar and the owner's inbox unverified (labelled as such).

## Safety and privacy
Tool arguments are validated field by field; the model never chooses tools freely; every booking needs an explicit confirmation turn.
Prompt-injection cases are tested on every deploy. The assistant states that it is an AI and that messages are logged.
