"""Formatting-only text helpers: Document Intelligence markdown cleanup and comparison keys.

Nothing here changes meaning. It removes markup (markdown escapes, HTML tags, LaTeX
wrappers that Document Intelligence emits for formulas) and builds normalized keys
used to compare a quoted phrase with its source.
"""

from __future__ import annotations

import html
import re

_MARKDOWN_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|<>~])")
# Only the tags Document Intelligence writes into markdown. A bare "<" or ">" is protocol text
# ("platelets <100 × 10^9/L or hemoglobin >9 g/dL") and must survive.
_HTML_TAG = re.compile(
    r"</?(?:table|thead|tbody|tfoot|tr|td|th|caption|figure|figcaption|br|sup|sub|b|i|u|em|strong|"
    r"span|p|div|img|ul|ol|li|a|hr)\b[^<>]*>",
    re.I,
)
_DI_MARKERS = re.compile(r":(?:selected|unselected|formula|barcode):")
_MATH_SEGMENT = re.compile(r"\$\$(.+?)\$\$|\$(.+?)\$", re.S)
_MATH_GROUP = re.compile(r"\\(?:mathrm|text|mathbf|mathit|operatorname)\s*\{([^{}]*)\}")
_MATH_SYMBOLS = {
    r"\geq": "≥",
    r"\ge": "≥",
    r"\leq": "≤",
    r"\le": "≤",
    r"\neq": "≠",
    r"\pm": "±",
    r"\times": "×",
    r"\cdot": "·",
    r"\approx": "≈",
    r"\sim": "~",
    r"\mu": "µ",
    r"\alpha": "α",
    r"\beta": "β",
    r"\gamma": "γ",
    r"\delta": "δ",
    r"\kappa": "κ",
    r"\lambda": "λ",
    r"\circ": "°",
    r"\%": "%",
    r"\rightarrow": "→",
    r"\to": "→",
    r"\prime": "′",
    r"\,": " ",
    r"\;": " ",
    r"\quad": " ",
}
_SYMBOL_PATTERN = re.compile(
    "|".join(re.escape(key) for key in sorted(_MATH_SYMBOLS, key=len, reverse=True)) + r"(?![A-Za-z])"
)
_QUOTES = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u2032": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\u00a0": " ",
        "\u2009": " ",
        "\u202f": " ",
        "\u00ad": "",
        "\u0002": "-",
    }
)


def _join_single_characters(tokens: list[str]) -> str:
    """Join tokens that DI split into single characters ("1 8" -> "18", "o f" -> "of")."""
    pieces: list[str] = []
    run: list[str] = []
    for token in tokens:
        if len(token) == 1:
            run.append(token)
            continue
        if run:
            pieces.append("".join(run))
            run = []
        pieces.append(token)
    if run:
        pieces.append("".join(run))
    return " ".join(pieces)


def latex_to_text(expression: str) -> str:
    """Plain text for a LaTeX math expression emitted by Document Intelligence."""
    protected: list[str] = []

    def keep_group(match: re.Match[str]) -> str:
        inner = match.group(1).split()
        text = "".join(inner) if inner and all(len(t) == 1 for t in inner) else " ".join(inner)
        protected.append(text)
        return f" \x00{len(protected) - 1}\x00 "

    text = _MATH_GROUP.sub(keep_group, expression)
    # Superscripts stay marked ("10^{9}" -> "10^9"); subscripts join their base ("T_{max}" -> "Tmax").
    text = re.sub(r"\^\s*\{([^{}]*)\}", lambda m: "^" + "".join(m.group(1).split()), text)
    text = re.sub(r"_\s*\{([^{}]*)\}", lambda m: "".join(m.group(1).split()), text)
    text = re.sub(r"\s*\^\s*", "^", text)
    text = _SYMBOL_PATTERN.sub(lambda m: f" {_MATH_SYMBOLS[m.group(0)]} ", text)
    text = re.sub(r"\\[A-Za-z]+", " ", text)
    text = text.replace("{", " ").replace("}", " ").replace("_", " ")
    tokens = text.split()
    joined = _join_single_characters(tokens)
    joined = re.sub(r"\x00(\d+)\x00", lambda m: protected[int(m.group(1))], joined)
    joined = re.sub(r"([≥≤≠±<>])\s+(?=[\d.])", r"\1", joined)
    return " ".join(joined.split())


def clean_line(text: str) -> str:
    """Remove markup from one line of Document Intelligence markdown."""
    text = _MATH_SEGMENT.sub(lambda m: " " + latex_to_text(m.group(1) or m.group(2)) + " ", text)
    text = _DI_MARKERS.sub(" ", text)
    text = _HTML_TAG.sub(" ", text)
    text = html.unescape(text)
    text = _MARKDOWN_ESCAPE.sub(r"\1", text)
    text = text.replace("\u00ad", "")
    return " ".join(text.split())


def comparison_key(text: str) -> str:
    """Case-folded key with unified quotes, dashes and spaces, for substring checks. Table cell
    separators ("|") count as spaces, so words that run across cells of one row can be quoted."""
    folded = str(text).translate(_QUOTES).casefold().replace("|", " ")
    folded = re.sub(r"\s*-\s*\n?\s*", "-", folded)
    return " ".join(folded.split())


def contains_phrase(haystack: str, needle: str) -> bool:
    """True when the needle appears in the haystack after comparison normalization."""
    key = comparison_key(needle)
    return bool(key) and key in comparison_key(haystack)


def normalize_label(text: str) -> str:
    """Key for comparing registry labels written with different hyphens or spacing."""
    folded = comparison_key(text)
    return re.sub(r"[\s\-_/]+", "", folded)
