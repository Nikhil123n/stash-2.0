from __future__ import annotations

import re


def sanitize_for_prompt(text: str, max_chars: int = 8000) -> str:
    text = text[:max_chars]
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    text = text.replace('```', '~~~')
    return text
