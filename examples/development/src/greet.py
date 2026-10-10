"""Tiny target program for the development sample task (DEV-0001)."""


def greet(name, lang="en"):
    """Return a greeting for ``name`` in the language ``lang``.

    Supported languages:
      - "en" (default): "Hello, <name>!"
      - "ja": "こんにちは、<name>さん"

    Any other ``lang`` raises ValueError; there is no silent fallback to English.
    """
    if lang == "en":
        return f"Hello, {name}!"
    if lang == "ja":
        return f"こんにちは、{name}さん"
    raise ValueError(f"unsupported lang: {lang!r}")
