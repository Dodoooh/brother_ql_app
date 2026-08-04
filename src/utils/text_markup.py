"""Bold and italic runs inside a line of label text.

Why this is a module and not three lines in the renderer
--------------------------------------------------------
The interface has promised ``**bold**`` and ``*italic*`` since 3.0.0 and the
printer never delivered it: the label renderer parses exactly one tag, ``<br>``,
so the asterisks were printed as asterisks. The live preview in the browser
*did* honour them, which made it worse than a missing feature -- the preview
showed something the label would not.

Delivering it is not a matter of choosing a second font at the moment of
drawing. Everything the renderer decides about a label is decided from measured
text: auto-fit searches for a font size by measuring and re-wrapping, wrapping
compares a candidate line against the printable width, round labels compute each
line's chord from its own width, and vertical alignment stacks measured line
heights. Once a line can be set in more than one face, every one of those
measurements has to walk the line piece by piece. So the piece -- the *run* --
is the thing that has to exist first, and measuring, wrapping and drawing are
defined here once, against it.

The base weight
---------------
This app has always drawn labels in DejaVu Sans **Bold**, which is the right
default on a 300 dpi thermal printer: thin strokes at 12 mm are hard to read.
That leaves nothing heavier for ``**bold**`` to mean, so markup can only work
when the base drops to the regular face. That changes how every label looks,
which is why it hangs off a setting (``text_markup``) that is off by default:
with it off the renderer sets one run per line in the bold face, exactly as
before, and asterisks stay literal.
"""

import os
import re
from typing import Any, Dict, List, NamedTuple, Optional

import structlog

logger = structlog.get_logger()


class Run(NamedTuple):
    """A stretch of text in one face."""

    text: str
    bold: bool = False
    italic: bool = False


# Longest marker first, or ``***both***`` would be read as bold followed by a
# stray asterisk. Non-greedy and anchored to a non-space, so "2 * 3 * 4" is
# arithmetic rather than an italic 3, and an unclosed marker stays literal
# because the pattern simply does not match.
_BOTH = re.compile(r"\*\*\*(?=\S)(.+?)(?<=\S)\*\*\*", re.S)
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.S)
_ITALIC = re.compile(r"(?<!\*)\*(?=\S)([^*]+?)(?<=\S)\*(?!\*)", re.S)

_PLACEHOLDER = "\x00"  # cannot appear in label text; marks an extracted run


def parse_runs(line: str, enabled: bool = True) -> List[Run]:
    """Split one line into runs.

    Args:
        line: The line's text, with any ``<br>`` already dealt with.
        enabled: When False the line comes back as a single plain run and the
            markers stay in the text, which is what every label did before this
            existed.

    Returns:
        Runs in reading order. Always at least one, so callers never have to
        handle an empty line specially.
    """
    if not enabled or not line:
        return [Run(line or "")]

    # Extracted spans are lifted out and replaced by a placeholder, so the
    # italic pass cannot look inside a bold span's text and re-mark it.
    spans: List[Run] = []

    def take(match: "re.Match[str]", bold: bool, italic: bool) -> str:
        spans.append(Run(match.group(1), bold, italic))
        return f"{_PLACEHOLDER}{len(spans) - 1}{_PLACEHOLDER}"

    remainder = _BOTH.sub(lambda m: take(m, True, True), line)
    remainder = _BOLD.sub(lambda m: take(m, True, False), remainder)
    remainder = _ITALIC.sub(lambda m: take(m, False, True), remainder)

    runs: List[Run] = []
    for piece in re.split(f"{_PLACEHOLDER}(\\d+){_PLACEHOLDER}", remainder):
        if piece is None or piece == "":
            continue
        if piece.isdigit() and int(piece) < len(spans):
            runs.append(spans[int(piece)])
        else:
            runs.append(Run(piece))
    return runs or [Run("")]


def runs_text(runs: List[Run]) -> str:
    """The line as plain text, markers already removed."""
    return "".join(run.text for run in runs)


class FontSet:
    """The four faces one font size needs, and the fallbacks when it has fewer.

    Built from the base font's path by name: DejaVu ships ``DejaVuSans.ttf``,
    ``-Bold``, ``-Oblique`` and ``-BoldOblique`` side by side, and that is what
    the image carries. A font without those siblings -- the matplotlib fallback,
    say -- yields a set where every face is the base one, so markup silently
    renders plain rather than failing a print over typography.
    """

    def __init__(self, base_path: Optional[str], size: int, markup: bool):
        from PIL import ImageFont

        self.size = size
        self.markup = markup
        self._base_path = base_path

        if not base_path:
            # No font at all: Pillow's built-in bitmap face, as before.
            plain = ImageFont.load_default()
            self.regular = self.bold = self.italic = self.bold_italic = plain
            return

        stem = self._stem(base_path)
        # With markup off the base stays what it has always been -- whatever
        # path the service resolved, bold included -- so nothing about an
        # existing label changes.
        regular_path = self._variant(stem, "") if markup else base_path
        self.regular = self._load(regular_path or base_path, size)
        self.bold = self._load(self._variant(stem, "-Bold"), size) or self.regular
        self.italic = self._load(self._variant(stem, "-Oblique")
                                 or self._variant(stem, "-Italic"), size) or self.regular
        self.bold_italic = (self._load(self._variant(stem, "-BoldOblique")
                                       or self._variant(stem, "-BoldItalic"), size)
                            or self.bold)

    @staticmethod
    def _stem(path: str) -> str:
        """The path with any known face suffix removed."""
        base, _ = os.path.splitext(path)
        for suffix in ("-BoldOblique", "-BoldItalic", "-Oblique", "-Italic", "-Bold"):
            if base.endswith(suffix):
                return base[: -len(suffix)]
        return base

    @staticmethod
    def _variant(stem: str, suffix: str) -> Optional[str]:
        candidate = f"{stem}{suffix}.ttf"
        return candidate if os.path.exists(candidate) else None

    @staticmethod
    def _load(path: Optional[str], size: int):
        if not path:
            return None
        try:
            from PIL import ImageFont
            return ImageFont.truetype(path, size)
        except Exception as e:  # noqa: BLE001 - a missing face is not a failed print
            logger.debug("Could not load font face", path=path, error=str(e))
            return None

    def for_run(self, run: Run):
        """The face a run is set in."""
        if run.bold and run.italic:
            return self.bold_italic
        if run.bold:
            return self.bold
        if run.italic:
            return self.italic
        return self.regular

    def at_size(self, size: int) -> "FontSet":
        """The same set at another size, for auto-fit's search."""
        return FontSet(self._base_path, size, self.markup)

    def getmetrics(self):
        """Ascent and descent of the base face, as ImageFont exposes them."""
        return self.regular.getmetrics()


def measure_runs(draw, runs: List[Run], fonts: FontSet) -> float:
    """How wide a line of runs sets.

    Summed per run rather than measured once: two faces have different advance
    widths, and the difference is exactly what wrapping and auto-fit get wrong
    if the line is measured in a single face.
    """
    return sum(draw.textlength(run.text, font=fonts.for_run(run))
               for run in runs if run.text)


def draw_runs(draw, xy, runs: List[Run], fonts: FontSet, fill="black") -> float:
    """Draw a line of runs left to right from ``xy``; return the width used."""
    x, y = xy
    start = x
    for run in runs:
        if not run.text:
            continue
        font = fonts.for_run(run)
        draw.text((x, y), run.text, font=font, fill=fill)
        x += draw.textlength(run.text, font=font)
    return x - start


def wrap_runs(draw, runs: List[Run], fonts: FontSet, max_width: float) -> List[List[Run]]:
    """Break a line of runs into lines that each fit ``max_width``.

    Breaks at spaces, and inside a word only when the word alone does not fit --
    the same rule the plain-text wrapper follows. A break may fall in the middle
    of a run, in which case the run is split and both halves keep their face.
    """
    if max_width <= 0:
        return [runs]

    # Flatten to (word, run) pairs so a break can fall anywhere, then rebuild.
    words: List[Run] = []
    for run in runs:
        if not run.text:
            continue
        for index, piece in enumerate(re.split(r"(\s+)", run.text)):
            if piece:
                words.append(Run(piece, run.bold, run.italic))

    lines: List[List[Run]] = []
    current: List[Run] = []
    for word in words:
        candidate = current + [word]
        # A leading space on a fresh line is dropped, as a wrap point rather
        # than as content.
        if not current and word.text.isspace():
            continue
        if measure_runs(draw, candidate, fonts) <= max_width or not current:
            current = candidate
            continue
        lines.append(current)
        current = [] if word.text.isspace() else [word]
    if current:
        lines.append(current)

    # A single word wider than the label still has to go somewhere: break it by
    # character, which is what the plain wrapper does too.
    result: List[List[Run]] = []
    for line in lines:
        if measure_runs(draw, line, fonts) <= max_width or len(line) > 1:
            result.append(_strip_trailing_space(line))
            continue
        result.extend(_break_word(draw, line[0], fonts, max_width))
    return result or [[Run("")]]


def _strip_trailing_space(line: List[Run]) -> List[Run]:
    """Drop the space a line broke at.

    It is a wrap point, not content: left on, it shifts a centred line off
    centre and pushes a right-aligned one past the margin by a space width.
    """
    trimmed = list(line)
    while trimmed and trimmed[-1].text.isspace():
        trimmed.pop()
    if trimmed:
        last = trimmed[-1]
        stripped = last.text.rstrip()
        if stripped != last.text:
            trimmed[-1] = Run(stripped, last.bold, last.italic)
    return trimmed or [Run("")]


def _break_word(draw, word: Run, fonts: FontSet, max_width: float) -> List[List[Run]]:
    """Split one over-wide run character by character."""
    font = fonts.for_run(word)
    lines: List[List[Run]] = []
    current = ""
    for char in word.text:
        if current and draw.textlength(current + char, font=font) > max_width:
            lines.append([Run(current, word.bold, word.italic)])
            current = char
        else:
            current += char
    if current:
        lines.append([Run(current, word.bold, word.italic)])
    return lines or [[word]]


def widest_word(draw, lines: List[List[Run]], fonts: FontSet) -> float:
    """The widest single word across every line, each measured in its own face.

    Auto-fit uses this to decide whether a font size is small enough that no
    word has to be hard-broken. A bold word is wider than the same word plain,
    so measuring them all in one face picks a size that then breaks the bold
    ones.
    """
    widest = 0.0
    for runs in lines:
        for run in runs:
            for word in run.text.split():
                widest = max(widest, draw.textlength(word, font=fonts.for_run(run)))
    return widest


def markup_enabled(settings: Dict[str, Any]) -> bool:
    """Whether this request wants markup honoured.

    Off unless asked for: turning it on changes the weight every label is set
    in, which is not something to inherit by surprise.
    """
    value = settings.get("text_markup", False)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)
