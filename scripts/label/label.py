#!/usr/bin/env python3
"""Burn a take ID into review copies. Clean masters are never touched.

    bin/tool label map.tsv OUTDIR

map.tsv: <source path> TAB <id>   (id like 05-v2-t1 or 08-door-v1-t3; '#' lines skipped)
The file name prefix is production.yaml's `label_prefix` (default: the production's slug),
so every review copy reads <label_prefix>-<id>:
  Stills -> OUTDIR/<label_prefix>-<id>.jpg : image untouched, a strip added below with the ID
            (four stills of one shot also make OUTDIR/<label_prefix>-<shot>-sheet.jpg)
  Videos (.mp4 .mov .m4v .mkv .webm) -> OUTDIR/<label_prefix>-<id>.mp4 : ID in the top-left corner for the whole clip,
            audio copied, container title set to the file name (Plex shows it).
Paths: map.tsv relative to the text root; sources and OUTDIR relative to the media root.
Review copies of clips go to production.yaml's `wip` (/wip takes clips and cuts
only); stills are reviewed in Flow.
Needs ffmpeg (videos) and Pillow (stills); the font is DejaVu Sans Bold, found by
scripts/lib/fonts.py (scripts/fonts/fetch-fonts.sh brings it to a Mac).
Ported from START's label.py (2026-09-23); same pixels, same names.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from lib import cli, fonts, production, wip  # noqa: E402


def pil():
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        raise SystemExit("label: stills need Pillow: pip3 install --user pillow")
    return Image, ImageDraw, ImageFont


def still(src, tid, out):
    Image, ImageDraw, ImageFont = pil()
    im = Image.open(src).convert('RGB')
    w, h = im.size
    strip = max(44, h // 17)
    c = Image.new('RGB', (w, h + strip), (0, 0, 0))
    c.paste(im, (0, 0))
    d = ImageDraw.Draw(c)
    f = ImageFont.truetype(fonts.path('sans-bold'), int(strip * 0.62))
    d.text((14, h + strip // 2), tid, font=f, fill=(255, 255, 255), anchor='lm')
    c.save(out, quality=95, subsampling=0)


# A Topaz master is a ProRes .mov (2026-09-26): only .mp4 counted, and the .mov went to Pillow.
VIDEO = ('.mp4', '.mov', '.m4v', '.mkv', '.webm')


def video(src, tid, out):
    txt = tid.replace(':', r'\:')
    vf = (f"drawtext=fontfile={fonts.path('sans-bold')}:text='{txt}':x=10:y=10:fontsize=h/18:"
          "fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=6")
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', src, '-vf', vf,
                    '-c:v', 'libx264', '-crf', '16', '-preset', 'slow', '-pix_fmt', 'yuv420p',
                    '-c:a', 'copy', '-metadata', f'title={wip.title(out)}', '-movflags', '+faststart', out], check=True)


def sheet(srcs, ids, out):
    Image, ImageDraw, ImageFont = pil()
    ims = [Image.open(s).convert('RGB') for s in srcs]
    w, h = ims[0].size
    s = Image.new('RGB', (w * 2, h * 2))
    d = ImageDraw.Draw(s)
    f = ImageFont.truetype(fonts.path('sans-bold'), h // 14)
    for k, (im, tid) in enumerate(zip(ims, ids)):
        x, y = (k % 2) * w, (k // 2) * h
        s.paste(im, (x, y))
        bb = d.textbbox((x + 16, y + 16), tid, font=f)
        d.rectangle([bb[0] - 8, bb[1] - 8, bb[2] + 8, bb[3] + 8], fill=(0, 0, 0))
        d.text((x + 16, y + 16), tid, font=f, fill=(255, 255, 255))
    s.thumbnail((1800, 1800))
    s.save(out, quality=90)


def main(argv):
    ap = cli.parser("label", __doc__)
    ap.add_argument("map", help="TSV: <source path> TAB <take id>")
    ap.add_argument("outdir", help="where the labelled review copies go")
    a = ap.parse_args(argv)
    r = cli.roots(a)
    r.say("label")
    p = production.load(r.text, r.media)
    if p.note():
        print(f"label: {p.note()}", file=sys.stderr)
    prefix = p.label_prefix
    rows = [l.rstrip('\n').split('\t') for l in open(r.t(a.map)) if l.strip() and not l.startswith('#')]
    outdir = r.m(a.outdir)
    if not cli.mounted(outdir):
        sys.exit(f"label: {outdir} is on a volume that is not mounted — mount it; nothing is made on the local disk in its place")
    os.makedirs(outdir, exist_ok=True)
    groups = {}
    for src, tid in rows:
        src = r.m(src)
        is_video = os.path.splitext(src)[1].lower() in VIDEO
        out = os.path.join(outdir, f'{prefix}-{tid}{".mp4" if is_video else ".jpg"}')
        (video if is_video else still)(src, tid, out)
        print(out)
        if not is_video:
            groups.setdefault(tid.rsplit('-t', 1)[0], []).append((src, tid))
    for g, items in groups.items():
        if len(items) == 4:
            out = os.path.join(outdir, f'{prefix}-{g}-sheet.jpg')
            sheet([x for x, _ in items], [y for _, y in items], out)
            print(out)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
