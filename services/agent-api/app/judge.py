"""LLM-as-judge shared by the offline experiment and the online scoring job.

The judge is a different model family (gpt-oss-120b on Groq) from the answerer (Gemini).
Abstentions and contact suggestions are not claims; a deterministic filter backs up the prompt.
"""

import asyncio
import json
import re

from openai import AsyncOpenAI, RateLimitError

from .config import settings

JUDGE_MODEL = "openai/gpt-oss-120b"

JUDGE = """You grade an assistant's answer against the sources it was given.
Count the factual claims in the answer. A claim is unsupported unless the sources explicitly state
it. These are NOT claims and must never be counted: statements that the information is unavailable
("I don't have that information", "the site does not contain..."), suggestions to email or contact
him, greetings. Count only statements asserting facts about Duc-Anh or his work.
For answerable questions with golden facts: golden_covered is how many of the golden facts the
answer conveys, and covers_golden is true only if it conveys ALL of them. abstained is true if the answer says it lacks the information instead of answering.
Return compact single-line JSON:
{"claims": int, "unsupported": int, "unsupported_text": [str], "golden_covered": int, "covers_golden": bool, "abstained": bool}"""

NON_CLAIM = re.compile(r"don't have|do not have|does not contain|doesn't contain|no information|"
                       r"not (?:on|in) the (?:site|sources)|e-?mail|contact him|cannot|can't", re.IGNORECASE)


def clean(j: dict) -> dict:
    flagged = j.get("unsupported_text") or []
    kept = [t for t in flagged if not NON_CLAIM.search(t)]
    dropped = len(flagged) - len(kept)
    j["unsupported_text"], j["unsupported"] = kept, max(0, j.get("unsupported", 0) - dropped)
    j["claims"] = max(0, j.get("claims", 0) - dropped)
    return j


def client() -> AsyncOpenAI:
    return AsyncOpenAI(base_url=settings.groq_base_url, api_key=settings.groq_api_key, max_retries=0)


async def judge(c: AsyncOpenAI, question: str, answer: str, chunks: list[dict],
                golden: list[str] | None = None, answerable: bool | None = None) -> dict:
    sources = "\n\n".join(f"[{i + 1}] {x['title']}\n{x['text']}" for i, x in enumerate(chunks))
    user = f"Question: {question}\n"
    if answerable is not None:
        user += f"Answerable from the site: {answerable}\nGolden facts: {golden}\n"
    user += f"\nSources:\n{sources}\n\nAnswer: {answer}"
    for attempt in range(8):
        try:
            r = await c.chat.completions.create(
                model=JUDGE_MODEL, max_tokens=1500, reasoning_effort="low",
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content": JUDGE}, {"role": "user", "content": user}])
            return clean(json.loads(r.choices[0].message.content))
        except RateLimitError:  # free tier: 8,000 tokens per minute
            await asyncio.sleep(4 + 6 * attempt)
        except json.JSONDecodeError:
            continue
    return {"claims": 0, "unsupported": 0, "unsupported_text": [], "covers_golden": False,
            "golden_covered": 0, "abstained": False, "judge_failed": True}
