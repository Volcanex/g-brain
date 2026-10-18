"""Text and provenance shared by the two todos-component stores."""
import re


def normalize_text(text: str | None, one_line: bool = False) -> str:
    """Decode the small set of escapes left by doubly encoded JSON strings."""
    escapes = {r"\r\n": "\n", r"\n": "\n", r"\t": "\t", r"\r": "\n", r'\"': '"'}
    text = re.sub(r'\\r\\n|\\[ntr"]', lambda m: escapes[m.group()], text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if text.endswith('"') and text.count('"') % 2:
        text = text[:-1].rstrip()
    return " ".join(text.split()) if one_line else text


def provenance_for(requester=None, composer=None, source=None) -> dict:
    return {k: str(v or "unknown") for k, v in
            (("requester", requester), ("composer", composer), ("source", source))}
