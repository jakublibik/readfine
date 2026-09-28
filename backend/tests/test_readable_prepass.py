"""What the page goes through before trafilatura reads it.

A <div> of plain text next to a <p> reaches the extracted article.

Trafilatura drops such a div's text. Bon Appétit writes each ingredient as
`<p>3</p><div>large bunches kale</div>`, so the recipe came out as bare quantities.
Layout whitespace and padded class names are made plain, which Barron's and RT need.
"""
import trafilatura

from app.services.readable_service import (
    _extract_with_trafilatura, _normalize_markup_whitespace, _paragraph_text_divs,
)

_INTRO = "<p>" + "A long paragraph about pasta, kale and garlic in olive oil. " * 8 + "</p>"


def _page(block: str) -> str:
    return (f"<html><body><article><h1>Spaghetti</h1>{_INTRO}"
            f"<h2>Ingredients</h2>{block}<h2>Preparation</h2>{_INTRO}</article></body></html>")


def _section(html: str) -> str:
    out = _extract_with_trafilatura(html, "https://ex.invalid/r") or ""
    return out[out.find("Ingredients"):out.find("Preparation")]


def test_recipe_ingredients_keep_their_descriptions():
    section = _section(_page(
        '<div class="List"><p class="Amount">3</p><div class="Description">large bunches kale</div>'
        '<p class="Amount"></p><div class="Description">Kosher salt</div>'
        '<p class="Amount">5</p><div class="Description">garlic cloves, <em>crushed</em></div></div>'
    ))
    # Each amount on the line of its ingredient, as the site's own CSS shows it.
    for line in ("<p>3 large bunches kale</p>", "<p>Kosher salt</p>", "<p>5 garlic cloves, "):
        assert line in section


def test_only_a_bare_amount_is_merged():
    tree = trafilatura.load_html(
        '<html><body><div id="box"><p>1½</p><div>cups flour</div>'
        '<p>Step one</p><div>Mix well</div></div></body></html>')
    _paragraph_text_divs(tree)
    box = tree.xpath("//*[@id='box']")[0]
    assert [(c.tag, c.text_content()) for c in box] == [
        ("p", "1½ cups flour"), ("p", "Step one"), ("p", "Mix well"),
    ]


def _tags_after(html: str) -> list[str]:
    tree = trafilatura.load_html(f"<html><body>{html}</body></html>")
    _paragraph_text_divs(tree)
    box = tree.xpath("//*[@id='box']")[0]
    return [child.tag for child in box]


def test_only_a_text_div_next_to_a_p_is_converted():
    assert _tags_after('<div id="box"><p>a</p><div>text</div></div>') == ["p", "p"]
    # No <p> beside it: left as it is, the rest of the page's divs are not ours.
    assert _tags_after('<div id="box"><div>text</div><div>more</div></div>') == ["div", "div"]


def test_a_container_or_an_empty_div_stays_a_div():
    assert _tags_after(
        '<div id="box"><p>a</p><div><p>inner</p></div><div>  </div>'
        '<div><img src="x.png"></div></div>'
    ) == ["p", "div", "div", "div"]


# ── _normalize_markup_whitespace ──────────────────────────────────────────────



def _normalized(body: str):
    tree = trafilatura.load_html(f"<html><body>{body}</body></html>")
    _normalize_markup_whitespace(tree)
    return tree


def test_layout_whitespace_is_collapsed_and_classes_are_trimmed():
    tree = _normalized('<div id="box" class="  nav__item   media ">\n\n      <p>a</p>\n\n\n    </div>')
    box = tree.xpath("//*[@id='box']")[0]
    assert box.get("class") == "nav__item media"
    assert box.text == "\n" and box[0].tail == "\n"
    assert box[0].text == "a"


def test_whitespace_in_code_and_a_nbsp_are_kept():
    tree = _normalized('<pre id="pre"><span>if</span>    <span>x</span>\n    </pre>'
                       '<p id="p"><b>a</b>\u00a0<b>b</b></p>')
    pre = tree.xpath("//*[@id='pre']")[0]
    assert pre[0].tail == "    " and pre[1].tail == "\n    "
    assert tree.xpath("//*[@id='p']")[0][0].tail == "\u00a0"
