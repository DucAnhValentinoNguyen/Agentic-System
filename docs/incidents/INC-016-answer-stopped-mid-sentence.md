# INC-016: answers that stopped in the middle of a sentence (17 Oct 2026)

**Found by.** The deploy gate, twice in one day, on different questions: "Is he still a student, and when will he finish?" was answered "Yes, Duc",
and "What recovery time and recovery point objectives does his pilot propose?" was answered "The Velocity Racing pilot proposes a Recovery". Both
passed when the identical code was re-run, so they looked like noise. The second one is stored in the turn log, so it is real traffic-shaped data:
2.8 s, one provider, no fallback, no error.

**What the logs say.** The model call for that turn finished without error: 78 output tokens (most of them the model's hidden reasoning, about 10 visible),
1.45 s. No `answer_truncated` entry exists for the period, so the stream did not end with a `length` finish reason. The router only treats
`finish_reason == "length"` as a cut-off; a stream that simply ends is treated as a complete answer.

**Cause.** Not established. The stream ends early without any signal we check. I did not find out whether Vertex's OpenAI-compatible endpoint
ends a streamed reply early under some condition or whether a chunk is lost.

**Fix (a defence, not a cure).** After streaming, the answer step checks whether the text ends with closing punctuation or a citation marker. If it
does not and no length cut-off was reported, the answer is retracted and generated once more, and a warning `answer_incomplete` is logged. A second
unfinished answer is shown as it is (better than looping). Test: a model that stops after "proposes a Recovery" and then answers fully.

**What I do not know.** How often it happens (two of about 250 gate answers that day, none seen in visitor traffic, but nothing counted it before).
The new `answer_incomplete` log line makes it measurable: if it fires often, the next step is to find the cause on the provider side.
