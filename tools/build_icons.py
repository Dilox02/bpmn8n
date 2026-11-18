"""Builds the n8n node icons from the official BPMN 2.0 glyphs of bpmn.io's bpmn-font
(SIL OFL 1.1, see n8n-nodes-bpmn8n/nodes/icons/LICENSE-bpmn-font.txt), the same notation as
the BPMN poster. Each glyph is cropped to its drawn area so it fills n8n's icon box, in a light
(black, as on the poster) and a dark (white) variant. Every other symbol of the notation (events,
gateways, markers, data, sub-processes...) goes to icons/bpmn/<group>/<name>.svg, cropped the same way
and black only: the canvas theme (n8n-editor/bpmn.js) draws n8n's own nodes with them and inverts
them in the dark theme.

    .venv/bin/python tools/build_icons.py
"""
import json
import re
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

SOURCE = "https://raw.githubusercontent.com/bpmn-io/bpmn-font/main/"
OUT = Path(__file__).resolve().parent.parent / "n8n-nodes-bpmn8n" / "nodes" / "icons"
GLYPHS = {  # n8n icon name -> bpmn-font glyph
    "serviceTask": "source/symbols/activities/type/service.svg",
    "scriptTask": "source/symbols/activities/type/script.svg",
    "businessRuleTask": "source/symbols/activities/type/business-rule.svg",
    "sendTask": "source/symbols/activities/type/send.svg",
    "receiveTask": "source/symbols/activities/type/receive.svg",
    "userTask": "source/symbols/activities/type/user.svg",
    "manualTask": "source/symbols/activities/type/manual.svg",
    "exclusiveGateway": "source/symbols/gateways/gateway-xor.svg",
    "inclusiveGateway": "source/symbols/gateways/gateway-or.svg",
    "parallelGateway": "source/symbols/gateways/gateway-parallel.svg",
    "timerEvent": "source/symbols/events/intermediate-event-catch-timer.svg",
    "eventBasedGateway": "source/symbols/gateways/gateway-eventbased.svg",
    "errorEndEvent": "source/symbols/events/end-event-error.svg",
    "callActivity": "source/symbols/activities/call-activity.svg",
    "globalVariable": "source/symbols/data/data-store.svg",
}
TREE = "https://api.github.com/repos/bpmn-io/bpmn-font/git/trees/main?recursive=1"
NOT_NOTATION = ("source/symbols/tools/", "source/symbols/bpmn-io.svg")  # the modeler's tools and logo
THEMES = {"": "#000000", ".dark": "#ffffff"}
PADDING = 0.04  # of the glyph's larger side


def clean(svg):
    """Drops Inkscape metadata and the fixed size; the viewBox is set after measuring."""
    svg = re.sub(r"<\?xml.*?\?>|<metadata.*?</metadata>|<defs[^>]*/>", "", svg, flags=re.S)
    return re.sub(r'\s(width|height|viewBox)="[^"]*"', "", svg, count=3)


def cropped(page, path):
    """The glyph with a square viewBox around what it draws."""
    svg = clean(urllib.request.urlopen(SOURCE + path).read().decode())
    page.set_content(svg.replace("<svg", '<svg width="4000" height="4000"', 1))
    x, y, w, h = page.evaluate("() => { const b = document.querySelector('svg').getBBox();"
                               " return [b.x, b.y, b.width, b.height]; }")
    side = max(w, h) * (1 + 2 * PADDING)  # square, centred
    view_box = f"{x + w / 2 - side / 2:.1f} {y + h / 2 - side / 2:.1f} {side:.1f} {side:.1f}"
    return re.sub(r"\n\s*\n", "\n", svg.replace("<svg", f'<svg viewBox="{view_box}"', 1))


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(SOURCE + "LICENSE", OUT / "LICENSE-bpmn-font.txt")
    symbols = [t["path"] for t in json.load(urllib.request.urlopen(TREE))["tree"]
               if t["path"].startswith("source/symbols/") and t["path"].endswith(".svg")
               and not t["path"].startswith(NOT_NOTATION)]
    with sync_playwright() as p:
        page = p.chromium.launch().new_page()
        for name, path in GLYPHS.items():
            svg = cropped(page, path)
            for suffix, color in THEMES.items():
                (OUT / f"{name}{suffix}.svg").write_text(svg.replace("#000000", color))
            print(f"{name:18s} <- {path.rsplit('/', 1)[1]}")
        for path in symbols:
            out = OUT / "bpmn" / path.removeprefix("source/symbols/")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(cropped(page, path))
        print(f"{len(symbols)} symbols -> {(OUT / 'bpmn').relative_to(OUT.parent.parent)}")


if __name__ == "__main__":
    main()
