"""Build the caption font set in modal/assets/fonts/ (run once; outputs are committed).

Downloads OFL fonts from github.com/google/fonts, pins variable fonts to one
static weight (libass renders a variable font at its default instance, which is
often Thin), and writes metrics.json: per-font advance widths that
modal/captions.py uses to lay out word positions without a font library.

    /usr/bin/python3 scripts/build_caption_fonts.py   # needs fonttools
"""

from __future__ import annotations

import json
import string
import subprocess
import tempfile
import urllib.request
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

G = "https://raw.githubusercontent.com/google/fonts/main/ofl"
OUT = Path(__file__).resolve().parent.parent / "modal" / "assets" / "fonts"

# file in OUT -> (source path under ofl/, axis pins or None for a static font)
FILES = {
    "InstrumentSerif-Regular.ttf": ("instrumentserif/InstrumentSerif-Regular.ttf", None),
    "InstrumentSerif-Italic.ttf": ("instrumentserif/InstrumentSerif-Italic.ttf", None),
    "PlayfairDisplay-SemiBold.ttf": ("playfairdisplay/PlayfairDisplay[wght].ttf", {"wght": 600}),
    "PlayfairDisplay-SemiBoldItalic.ttf": ("playfairdisplay/PlayfairDisplay-Italic[wght].ttf", {"wght": 600}),
    "CormorantGaramond-SemiBold.ttf": ("cormorantgaramond/CormorantGaramond[wght].ttf", {"wght": 600}),
    "CormorantGaramond-SemiBoldItalic.ttf": ("cormorantgaramond/CormorantGaramond-Italic[wght].ttf", {"wght": 600}),
    "DMSerifDisplay-Regular.ttf": ("dmserifdisplay/DMSerifDisplay-Regular.ttf", None),
    "DMSerifDisplay-Italic.ttf": ("dmserifdisplay/DMSerifDisplay-Italic.ttf", None),
    "Manrope-SemiBold.ttf": ("manrope/Manrope[wght].ttf", {"wght": 600}),
    "DMSans-Medium.ttf": ("dmsans/DMSans[opsz,wght].ttf", {"wght": 500, "opsz": 14}),
    "Montserrat-SemiBold.ttf": ("montserrat/Montserrat[wght].ttf", {"wght": 600}),
}
LICENSES = ["instrumentserif", "playfairdisplay", "cormorantgaramond",
            "dmserifdisplay", "manrope", "dmsans", "montserrat"]

# Characters lyrics plausibly use. Anything else is measured as the average.
CHARS = (string.ascii_letters + string.digits + string.punctuation + " "
         + "‘’“”–—…àáâäãåçèéêëìíîïñòóôöõùúûüýÿÀÁÂÄÇÈÉÊËÌÍÎÏÑÒÓÔÖÙÚÛÜ¿¡")


def fetch(path: str, dest: Path) -> None:
    url = f"{G}/{path}".replace("[", "%5B").replace("]", "%5D")
    with urllib.request.urlopen(url) as r:
        dest.write_bytes(r.read())


def metrics(path: Path) -> dict:
    f = TTFont(path)
    os2 = f["OS/2"]
    asc, desc = os2.usWinAscent, os2.usWinDescent
    if not asc + desc:
        asc, desc = f["hhea"].ascent, -f["hhea"].descent
    cmap = f.getBestCmap()
    hmtx = f["hmtx"]
    adv = {c: hmtx[cmap[ord(c)]][0] for c in CHARS if ord(c) in cmap}
    names = f["name"]
    family = (names.getDebugName(16) or names.getDebugName(1))
    return {
        "family": family,
        "full_name": names.getDebugName(4),
        "units_per_em": f["head"].unitsPerEm,
        # libass sizes a face so that winAscent + winDescent == font size (px).
        "em_box": asc + desc,
        "avg": round(sum(adv.values()) / len(adv)),
        "adv": adv,
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    out_metrics = {}
    with tempfile.TemporaryDirectory() as tmp:
        for name, (src, pins) in FILES.items():
            raw = Path(tmp) / Path(src).name
            fetch(src, raw)
            dest = OUT / name
            if pins:
                font = TTFont(raw)
                inst = instancer.instantiateVariableFont(
                    font, pins, updateFontNames=True
                )
                inst.save(dest)
            else:
                dest.write_bytes(raw.read_bytes())
            out_metrics[name] = metrics(dest)
            print(name, out_metrics[name]["family"], "|", out_metrics[name]["full_name"])
    for fam in LICENSES:
        fetch(f"{fam}/OFL.txt", OUT / f"OFL-{fam}.txt")
    (OUT / "metrics.json").write_text(json.dumps(out_metrics, ensure_ascii=False, indent=1) + "\n")
    subprocess.run(["fc-scan", "--format", "%{file}: %{family} / %{style}\n", str(OUT)], check=False)


if __name__ == "__main__":
    main()
