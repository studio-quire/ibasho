"""Draw Ibasho's mark and write every branded image the Windows viewer build uses.

    python make_art.py VIEWER_SOURCE_TREE [--font PATH_TO_DejaVuSans-Bold.ttf] [--preview OUT.png]

The mark: a lit, arched doorway on a dark tile, light spilling onto the floor. Drawn from
scratch with Pillow at 1024 px and downsampled; the smallest sizes get a simpler drawing (no
spill, thicker shapes) so they stay legible. Writes over Linden Lab's files in place, keeping
their names and sizes, so no build files change:

  indra/newview/icons/test/            secondlife.ico/.icns, secondlife_*.png, secondlife_256.BMP
  indra/newview/res/                   icon1.ico, ll_icon_small.ico
  indra/newview/installers/windows/    install_icon.*, uninstall_icon.*
  indra/newview/skins/default/textures/windows/login_sl_logo*.png   (mark + "Ibasho")
  indra/newview/skins/default/textures/icons/SL_Logo.png           (a neutral "system" badge)
  indra/newview/skins/default/textures/icons/hand.png              (the mark, beside place links)
  indra/newview/skins/default/textures/icons/Inv_SysClosed.png, Inv_SysOpen.png   (system folders)
  indra/newview/skins/default/textures/icons/map_placeholder.png   (a place without a picture)
  indra/newview/skins/default/textures/taskpanel/TabIcon_Home_Selected.png   (a white doorway)

Written by Claude Opus 5.5 for Ibasho, 2026-10-22. The images it makes are original work, released
with the viewer under CC BY-SA 3.0 like the artwork they replace (see doc/LICENSE-logos.txt).
"""
import argparse
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

TILE_TOP = (36, 62, 78)
TILE_BOTTOM = (20, 36, 46)
LIGHT_TOP = (250, 220, 150)
LIGHT_BOTTOM = (233, 164, 72)
TEXT = (243, 233, 216)
S = 1024


def vgradient(size, top, bottom):
    w, h = size
    g = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(h - 1, 1)
        g.putpixel((0, y), tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)))
    return g.resize((w, h))


def arch_mask(size, box):
    """An arch (rectangle with a half-disc on top) inside box = (x0, y0, x1, y1)."""
    m = Image.new("L", size, 0)
    d = ImageDraw.Draw(m)
    x0, y0, x1, y1 = box
    r = (x1 - x0) / 2
    d.ellipse((x0, y0, x1, y0 + 2 * r), fill=255)
    d.rectangle((x0, y0 + r, x1, y1), fill=255)
    return m


def mark(px, simple=False):
    """The mark at px x px, drawn at 1024 and downsampled."""
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    tile = Image.new("L", (S, S), 0)
    inset = 0 if simple else 24
    ImageDraw.Draw(tile).rounded_rectangle((inset, inset, S - 1 - inset, S - 1 - inset),
                                           radius=int(S * (0.2 if simple else 0.22)), fill=255)
    img.paste(vgradient((S, S), TILE_TOP, TILE_BOTTOM), (0, 0), tile)

    # the doorway
    dw = 0.46 if simple else 0.38
    dh = 0.62 if simple else 0.54
    floor = 0.84 if simple else 0.76
    box = (int(S * (0.5 - dw / 2)), int(S * (floor - dh)), int(S * (0.5 + dw / 2)), int(S * floor))
    if not simple:
        # light spilling out onto the floor, fading towards the viewer
        spill = Image.new("L", (S, S), 0)
        sd = ImageDraw.Draw(spill)
        x0, _, x1, y1 = box
        sd.polygon([(x0, y1), (x1, y1), (x1 + 120, S - inset), (x0 - 120, S - inset)], fill=255)
        fade = Image.new("L", (1, S))
        for y in range(S):
            fade.putpixel((0, y), 0 if y < y1 else int(150 * (1 - (y - y1) / (S - y1))))
        spill = Image.composite(fade.resize((S, S)), Image.new("L", (S, S), 0), spill)
        spill = Image.composite(spill, Image.new("L", (S, S), 0), tile)
        img.paste(Image.new("RGB", (S, S), LIGHT_BOTTOM), (0, 0), spill)
        # a soft glow round the door
        glow = arch_mask((S, S), (box[0] - 30, box[1] - 30, box[2] + 30, box[3])).filter(ImageFilter.GaussianBlur(40))
        glow = glow.point(lambda v: v * 90 // 255)
        glow = Image.composite(glow, Image.new("L", (S, S), 0), tile)
        img.paste(Image.new("RGB", (S, S), LIGHT_TOP), (0, 0), glow)
    door = arch_mask((S, S), box)
    img.paste(vgradient((S, S), LIGHT_TOP, LIGHT_BOTTOM), (0, 0), door)
    if not simple:
        # threshold
        ImageDraw.Draw(img).rectangle((box[0] - 40, box[3], box[2] + 40, box[3] + 22), fill=TEXT + (255,))
    return img.resize((px, px), Image.LANCZOS)


def icon_image(px):
    return mark(px, simple=px <= 32)


def save_ico(path, sizes):
    imgs = [icon_image(s) for s in sizes]
    big = imgs[-1]
    big.save(path, format="ICO", sizes=[(s, s) for s in sizes], append_images=imgs[:-1])


def save_bmp(path, px, bg=(255, 255, 255)):
    im = Image.new("RGB", (px, px), bg)
    m = icon_image(px)
    im.paste(m, (0, 0), m)
    im.save(path, format="BMP")


def logo(size, font_path, scale=1.0):
    """Mark plus the word "Ibasho", on transparent, for the login panel."""
    w, h = size
    ss = 4
    img = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
    m = int(min(h * 0.72, w * 0.3) * ss * scale)
    img.paste(mark(m), (int(h * 0.09 * ss), (h * ss - m) // 2), mark(m))
    d = ImageDraw.Draw(img)
    x = int(h * 0.09 * ss) + m + int(m * 0.18)
    size = int(m * 0.6)
    while True:  # as large as fits, up to 0.6 of the mark's height
        font = ImageFont.truetype(font_path, size)
        bbox = d.textbbox((0, 0), "Ibasho", font=font)
        if x + bbox[2] <= w * ss - int(h * 0.09 * ss):
            break
        size -= ss
    y = (h * ss - (bbox[3] - bbox[1])) // 2 - bbox[1]
    d.text((x, y), "Ibasho", font=font, fill=TEXT + (255,))
    return img.resize((w, h), Image.LANCZOS)


def system_badge(px=25):
    """Neutral badge for system messages (where the stock viewer shows its own logo)."""
    ss = 8
    img = Image.new("RGBA", (px * ss, px * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((ss, ss, px * ss - ss, px * ss - ss), fill=(120, 132, 140, 255))
    c = px * ss / 2
    d.ellipse((c - 1.6 * ss, 5 * ss, c + 1.6 * ss, 8.2 * ss), fill=(245, 245, 245, 255))
    d.rounded_rectangle((c - 1.5 * ss, 10 * ss, c + 1.5 * ss, 19.5 * ss), radius=ss, fill=(245, 245, 245, 255))
    return img.resize((px, px), Image.LANCZOS)


FOLDER_BACK = (220, 197, 143)
FOLDER_FRONT = (255, 228, 184)


def system_folder(open_, px=16):
    """An inventory system folder: a plain folder carrying a small mark (where the stock viewer puts its logo)."""
    ss = 8
    S2 = px * ss
    img = Image.new("RGBA", (S2, S2), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    u = ss  # one final pixel
    if open_:
        d.rounded_rectangle((1 * u, 1 * u, 6 * u, 4 * u), radius=u // 2, fill=FOLDER_BACK + (255,))
        d.rectangle((1 * u, 3 * u, 13 * u, 15 * u), fill=FOLDER_BACK + (255,))
        d.polygon([(4 * u, 6 * u), (16 * u, 6 * u), (13 * u, 15 * u), (1 * u, 15 * u)], fill=FOLDER_FRONT + (255,))
    else:
        d.rounded_rectangle((2 * u, 1 * u, 8 * u, 5 * u), radius=u // 2, fill=FOLDER_BACK + (255,))
        d.rectangle((0, 4 * u, S2 - 1, 6 * u), fill=FOLDER_BACK + (255,))
        d.rectangle((0, 6 * u, S2 - 1, S2 - 1), fill=FOLDER_FRONT + (255,))
    m = mark(10 * u, simple=True)
    img.alpha_composite(m, (4 * u, 6 * u))
    return img.resize((px, px), Image.LANCZOS)


def home_glyph(px=24):
    """A white doorway on transparent, in the style of the stock viewer's white tab icons."""
    ss = 8
    S2 = px * ss
    m = arch_mask((S2, S2), (int(S2 * 0.24), int(S2 * 0.10), int(S2 * 0.76), int(S2 * 0.88)))
    inner = arch_mask((S2, S2), (int(S2 * 0.35), int(S2 * 0.21), int(S2 * 0.65), int(S2 * 0.88)))
    m = Image.composite(Image.new("L", (S2, S2), 0), m, inner)
    ImageDraw.Draw(m).rectangle((int(S2 * 0.10), int(S2 * 0.80), int(S2 * 0.90), int(S2 * 0.90)), fill=255)
    img = Image.new("RGBA", (S2, S2), (255, 255, 255, 0))
    img.putalpha(m)
    return img.resize((px, px), Image.LANCZOS)


def place_placeholder(px=128):
    """Stand-in picture for a place without one: the mark on a quiet light ground."""
    img = Image.new("RGBA", (px, px), (232, 229, 222, 255))
    m = mark(int(px * 0.62))
    img.alpha_composite(m, ((px - m.width) // 2, (px - m.height) // 2))
    return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tree")
    ap.add_argument("--font", default=os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts", "DejaVuSans-Bold.ttf"))
    ap.add_argument("--preview")
    a = ap.parse_args()
    nv = Path(a.tree) / "indra" / "newview"

    icons = nv / "icons" / "test"
    ico_sizes = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]
    save_ico(icons / "secondlife.ico", ico_sizes)
    for s in (16, 32, 48, 128, 256, 512):
        icon_image(s).save(icons / f"secondlife_{s}.png")
    save_bmp(icons / "secondlife_256.BMP", 256)
    mark(1024).save(icons / "secondlife.icns", format="ICNS")

    save_ico(nv / "res" / "icon1.ico", [32])
    save_ico(nv / "res" / "ll_icon_small.ico", [16, 48, 128, 256])

    inst = nv / "installers" / "windows"
    save_ico(inst / "install_icon.ico", [16, 32, 48, 256])
    save_ico(inst / "uninstall_icon.ico", [16, 32, 48, 256])
    save_bmp(inst / "install_icon.BMP", 256, bg=(0, 0, 0))
    save_bmp(inst / "uninstall_icon.BMP", 256, bg=(0, 0, 0))

    tex = nv / "skins" / "default" / "textures"
    logo((225, 94), a.font).save(tex / "windows" / "login_sl_logo.png")
    logo((160, 77), a.font).save(tex / "windows" / "login_sl_logo_horizontal.png")
    logo((165, 73), a.font).save(tex / "windows" / "login_sl_logo_small.png")
    system_badge().save(tex / "icons" / "SL_Logo.png")
    icon_image(15).save(tex / "icons" / "hand.png")
    system_folder(False).save(tex / "icons" / "Inv_SysClosed.png")
    system_folder(True).save(tex / "icons" / "Inv_SysOpen.png")
    place_placeholder().save(tex / "icons" / "map_placeholder.png")
    home_glyph().save(tex / "taskpanel" / "TabIcon_Home_Selected.png")

    if a.preview:
        sheet = Image.new("RGBA", (900, 390), (40, 40, 40, 255))
        x = 10
        for s in (256, 128, 48, 32, 16):
            sheet.paste(icon_image(s), (x, 10), icon_image(s))
            x += s + 10
        dark = Image.new("RGBA", (400, 100), (32, 35, 38, 255))
        lg = logo((160, 77), a.font)
        dark.paste(lg, (10, 10), lg)
        b = system_badge()
        dark.paste(b, (200, 30), b)
        x = 240
        for small in (icon_image(15), system_folder(False), system_folder(True), home_glyph()):
            dark.paste(small, (x, 34), small)
            x += 30
        sheet.paste(place_placeholder(), (420, 270))
        sheet.paste(dark, (10, 280))
        sheet.save(a.preview)


if __name__ == "__main__":
    main()
