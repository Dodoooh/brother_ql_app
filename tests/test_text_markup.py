"""Bold and italic in label text, and the promise that they change nothing else.

The interface has offered ``**bold**`` and ``*italic*`` since 3.0.0 and the
printer ignored both: the label renderer parses one tag, ``<br>``, so the
asterisks were printed as asterisks while the browser preview showed them as
emphasis. Delivering it needs the base weight to drop from bold to regular --
there is nothing heavier than bold in the family the labels are set in -- and
that changes how every label looks. So it hangs off ``text_markup``, off by
default, and the most important tests here are the ones that prove "off" is
still exactly what it was.
"""

import pytest

from src.utils.text_markup import (
    FontSet,
    Run,
    markup_enabled,
    measure_runs,
    parse_runs,
    runs_text,
    widest_word,
    wrap_runs,
)

PIL = pytest.importorskip("PIL")
DEJAVU_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _draw():
    from PIL import Image, ImageDraw
    return ImageDraw.Draw(Image.new("RGB", (10, 10), "white"))


def _fonts(markup=True, size=40):
    import os
    if not os.path.exists(DEJAVU_BOLD):
        pytest.skip("no DejaVu on this host")
    return FontSet(DEJAVU_BOLD, size, markup)


# --------------------------------------------------------------------------- #
# Reading the markers
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text, expected", [
    ("plain", [("plain", False, False)]),
    ("Regal **A-12** hier", [("Regal ", False, False), ("A-12", True, False),
                             (" hier", False, False)]),
    ("**alles**", [("alles", True, False)]),
    ("ein *wort* kursiv", [("ein ", False, False), ("wort", False, True),
                           (" kursiv", False, False)]),
    ("***beides***", [("beides", True, True)]),
    ("a**b**c", [("a", False, False), ("b", True, False), ("c", False, False)]),
])
def test_markers_become_runs(text, expected):
    assert [(r.text, r.bold, r.italic) for r in parse_runs(text)] == expected


@pytest.mark.parametrize("text", [
    "2 * 3 * 4",            # arithmetic, not an italic 3
    "unclosed **bold",      # a marker without its partner is text
    "* leading star",
    "trailing star *",
    "**",
])
def test_text_that_only_looks_like_markup_is_left_alone(text):
    """A label that says "2 * 3" must print "2 * 3"."""
    assert runs_text(parse_runs(text)) == text
    assert not any(r.bold or r.italic for r in parse_runs(text))


def test_nothing_is_read_when_markup_is_off():
    """The markers stay in the text, which is what has always been printed."""
    runs = parse_runs("Regal **A-12**", enabled=False)
    assert runs == [Run("Regal **A-12**")]


def test_the_plain_text_survives_parsing():
    """Whatever the faces, the words are the words."""
    assert runs_text(parse_runs("**a** b *c*")) == "a b c"


# --------------------------------------------------------------------------- #
# The faces
# --------------------------------------------------------------------------- #

def test_the_four_faces_are_found_next_to_the_base_font():
    fonts = _fonts(markup=True)
    paths = [f.path for f in (fonts.regular, fonts.bold, fonts.italic, fonts.bold_italic)]
    assert paths[0].endswith("DejaVuSans.ttf"), "base did not drop to the regular weight"
    assert paths[1].endswith("DejaVuSans-Bold.ttf")
    assert paths[2].endswith("DejaVuSans-Oblique.ttf")
    assert paths[3].endswith("DejaVuSans-BoldOblique.ttf")


def test_with_markup_off_the_base_is_the_font_the_service_chose():
    """No setting, no change: labels stay in the weight they were always in."""
    assert _fonts(markup=False).regular.path.endswith("DejaVuSans-Bold.ttf")


def test_a_font_without_siblings_renders_plain_rather_than_failing(tmp_path):
    """Typography is not worth a failed print.

    A font resolved from somewhere without -Bold/-Oblique next to it (the
    matplotlib fallback, say) yields a set where every face is the base one.
    """
    import os
    import shutil

    if not os.path.exists(DEJAVU_BOLD):
        pytest.skip("no DejaVu on this host")
    lonely = tmp_path / "Lonely.ttf"
    shutil.copy(DEJAVU_BOLD, lonely)
    fonts = FontSet(str(lonely), 40, markup=True)
    assert fonts.bold is fonts.regular
    assert fonts.italic is fonts.regular
    assert fonts.bold_italic is fonts.regular


# --------------------------------------------------------------------------- #
# Measuring, which is what everything else is built on
# --------------------------------------------------------------------------- #

def test_a_bold_run_measures_wider_than_the_same_text_plain():
    """The reason measurement had to become run-aware at all."""
    draw, fonts = _draw(), _fonts()
    plain = measure_runs(draw, [Run("Lagerplatz")], fonts)
    bold = measure_runs(draw, [Run("Lagerplatz", bold=True)], fonts)
    assert bold > plain


def test_a_line_measures_as_the_sum_of_its_runs():
    draw, fonts = _draw(), _fonts()
    runs = [Run("Regal "), Run("A-12", bold=True), Run(" hier")]
    parts = sum(measure_runs(draw, [r], fonts) for r in runs)
    assert measure_runs(draw, runs, fonts) == pytest.approx(parts)


def test_the_widest_word_is_measured_in_its_own_face():
    """Auto-fit shrinks until no word has to be broken; a bold word is wider."""
    draw, fonts = _draw(), _fonts()
    plain = widest_word(draw, [[Run("Lagerplatz")]], fonts)
    bold = widest_word(draw, [[Run("Lagerplatz", bold=True)]], fonts)
    assert bold > plain


# --------------------------------------------------------------------------- #
# Wrapping
# --------------------------------------------------------------------------- #

def test_wrapping_keeps_every_word_and_its_face():
    draw, fonts = _draw(), _fonts()
    runs = parse_runs("Der **Lagerplatz** ist hier und dieser Text ist deutlich zu lang")
    lines = wrap_runs(draw, runs, fonts, 400)

    assert len(lines) > 1, "nothing wrapped"
    assert " ".join(runs_text(line) for line in lines).split() == runs_text(runs).split()
    bold_words = [r.text for line in lines for r in line if r.bold]
    assert bold_words == ["Lagerplatz"], "a word lost its face at the line break"


def test_every_wrapped_line_fits():
    draw, fonts = _draw(), _fonts()
    runs = parse_runs("**Sehr** lange Beschriftung fuer ein schmales Etikett hier")
    for line in wrap_runs(draw, runs, fonts, 300):
        assert measure_runs(draw, line, fonts) <= 300 or len(line) == 1


def test_a_line_does_not_end_in_the_space_it_broke_at():
    """A trailing space would push a centred line off centre."""
    draw, fonts = _draw(), _fonts()
    lines = wrap_runs(draw, parse_runs("eins zwei drei vier fuenf sechs sieben"), fonts, 200)
    for line in lines:
        assert not runs_text(line).endswith(" ")


def test_a_word_wider_than_the_label_is_broken_rather_than_lost():
    draw, fonts = _draw(), _fonts()
    lines = wrap_runs(draw, parse_runs("Donaudampfschifffahrtsgesellschaft"), fonts, 150)
    assert len(lines) > 1
    assert "".join(runs_text(line) for line in lines) == "Donaudampfschifffahrtsgesellschaft"


# --------------------------------------------------------------------------- #
# The switch
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value, expected", [
    (True, True), (False, False), (None, False),
    ("true", True), ("True", True), ("1", True), ("yes", True), ("on", True),
    ("false", False), ("", False), ("nonsense", False),
])
def test_the_setting_is_read_loosely_but_defaults_to_off(value, expected):
    settings = {} if value is None else {"text_markup": value}
    assert markup_enabled(settings) is expected


def test_an_absent_setting_means_off():
    assert markup_enabled({}) is False


# --------------------------------------------------------------------------- #
# Through the real renderer
# --------------------------------------------------------------------------- #

@pytest.fixture
def service(tmp_path):
    """A PrinterService writing into a temp dir, with a font that has siblings."""
    import os

    from src.services.printer_service import PrinterService

    if not os.path.exists(DEJAVU_BOLD):
        pytest.skip("no DejaVu on this host")
    svc = PrinterService(upload_folder=str(tmp_path))
    svc.font_path = DEJAVU_BOLD
    return svc


@pytest.mark.parametrize("orientation", ["across", "lengthwise"])
@pytest.mark.parametrize("label_size", ["12", "62"])
def test_a_marked_up_label_renders_in_every_orientation(service, orientation, label_size):
    """The whole chain, because the pieces passing is not the same as it working.

    Lengthwise is the case that broke first: it sizes its canvas from the
    measured line width, and a measurement in fractional pixels is not
    something Image.new accepts.
    """
    from PIL import Image

    path = service._create_text_label("test **bold** hier", {
        "label_size": label_size,
        "font_size": 50,
        "orientation": orientation,
        "text_markup": True,
    })
    with Image.open(path) as img:
        assert img.width > 0 and img.height > 0


def test_the_markers_are_gone_from_a_marked_up_label(service):
    """Ink where the words are, and less of it than the markers would make.

    Rendering the same text with and without markup: the marked-up label sets
    the asterisks as nothing at all, so it carries strictly less ink.
    """
    from PIL import Image, ImageOps

    def ink(settings):
        path = service._create_text_label("test **bold** hier", settings)
        with Image.open(path) as img:
            return ImageOps.invert(img.convert("L")).getbbox()

    base = {"label_size": "62", "font_size": 50, "auto_fit": False}
    plain = ink(dict(base))
    marked = ink(dict(base, text_markup=True))
    assert plain is not None and marked is not None
    assert (marked[2] - marked[0]) < (plain[2] - plain[0]), (
        "the marked-up label is not narrower, so the asterisks are still being set")
