import tiktoken

# cl100k_base covers GPT-4, GPT-3.5-turbo, DeepSeek, and is a reasonable
# approximation for Gemini (which uses SentencePiece but with similar density).
_fallback_encoding = tiktoken.get_encoding("cl100k_base")

def _get_encoding(model: str):
    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        return _fallback_encoding


def count_tokens(text: str, model: str = "gpt-4o-mini") -> int:
    enc = _get_encoding(model)
    return len(enc.encode(text))
