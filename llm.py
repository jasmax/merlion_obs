import os

from dotenv import load_dotenv

load_dotenv()  # reads .env in this folder into os.environ

from tracer import record_llm


def ask(span, instructions, text, model):
    """Call `model`, record tokens and cost on `span`, return the reply text.
    With no OPENAI_API_KEY it returns a canned reply and *estimated* tokens, so you can build offline."""
    span["llm.input_preview"] = text[:200]       # what the model was sent, to prove it was masked
    if not os.environ.get("OPENAI_API_KEY"):
        record_llm(span, model, (len(instructions) + len(text)) // 4, 2, estimated=True)
        return "investigate" if "one word" in instructions else "not_received"
    from openai import OpenAI
    try:
        response = OpenAI().responses.create(model=model, instructions=instructions, input=text)
        u = response.usage
        record_llm(span, model, u.input_tokens, u.output_tokens,
                   cached=u.input_tokens_details.cached_tokens, resolved=response.model)
        return response.output_text
    except Exception as e:
        # Keep the pipeline alive when the model API is unavailable or slow.
        span["llm.error"] = type(e).__name__
        record_llm(span, model, (len(instructions) + len(text)) // 4, 2, estimated=True)
        return "investigate" if "one word" in instructions else "not_received"