"""Jarvis's icon: the glowing orb from the HUD design.

Used for the HUD window's taskbar icon and the Start-with-Windows shortcut.
Want your own? Put any square picture named icon.png in this folder and delete jarvis.ico;
Jarvis uses yours instead (restart Jarvis to see it).
"""

from pathlib import Path

HERE = Path(__file__).parent
PNG = HERE / "icon.png"
ICO = HERE / "jarvis.ico"


def _draw(size=512):
    from PIL import Image, ImageDraw, ImageFilter

    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = s / 2

    def circle(r, **kw):
        d.ellipse((c - r, c - r, c + r, c + r), **kw)

    circle(s * .48, fill=(7, 11, 16, 255))                                  # dark ground
    circle(s * .44, outline=(28, 42, 54, 255), width=max(2, s // 128))      # faint outer line
    # broken cyan ring (like the HUD's spinning ring)
    for start in range(-80, 280, 90):
        d.arc((c - s * .38, c - s * .38, c + s * .38, c + s * .38), start, start + 58,
              fill=(63, 212, 255, 255), width=max(3, s // 26))
    circle(s * .27, fill=(14, 38, 51, 255))
    circle(s * .2, fill=(18, 59, 77, 255))
    # glowing core
    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((c - s * .16, c - s * .16, c + s * .16, c + s * .16), fill=(63, 212, 255, 170))
    img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(s * .04)))
    ImageDraw.Draw(img).ellipse((c - s * .11, c - s * .11, c + s * .11, c + s * .11), fill=(63, 212, 255, 255))
    return img


def ensure() -> Path | None:
    """Make icon.png and jarvis.ico if they're missing. Returns the .ico path (or None without Pillow)."""
    try:
        from PIL import Image
    except ImportError:
        return None
    if not PNG.exists():
        _draw().save(PNG)
    if not ICO.exists():
        Image.open(PNG).convert("RGBA").save(ICO, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                                                         (128, 128), (256, 256)])
    return ICO


if __name__ == "__main__":
    print(ensure())
