#!/usr/bin/env python3
"""A listening reel: the same stretches of picture played once per version of the sound, each labelled.

Usage:
  bin/tool ab_reel --picture r3/cut/review/<prefix>-film-mix-v9.mp4 --out r3/audio/ab/NAME.mp4
                   --span "IN-OUT|what it is" [--span ...] --version "LABEL=AUDIO" [--version ...] [--wip DIR] [--no-land]

Media root: --picture, --out, every --version's audio. For every span, every version plays in the order given: the
review copy's picture (clip names and timecode burned in) with that version's sound at the same film seconds, and a
label at the bottom: "<span>  ·  <LABEL>". 10 frames of black between pieces. Spans are cut to whole frames at 24 fps.
The sound is built as ONE 48 kHz PCM track, each piece cut at exact samples from its version (20 ms fade in, 50 ms
out), silence of exactly the gap's length between, and encoded once. Only the picture is cut in pieces (video only,
joined with -c copy). Two faults of the first version (2026-09-23) are why: ffmpeg 4.4 (the Mac's) cut each piece's
sound short when -frames:v ended the output, and every AAC piece brought its own ~21 ms of priming into the join, so
the sound drifted 21 ms further off with each piece. The finished reel is checked: every piece's sound is found by
cross-correlation against its version and must sit within 2 ms of its place, or the tool fails. The reel is landed
and, with --wip, copied there as <label_prefix>-<name>.
Ported from START's scripts/mix/ab_reel.py (2026-09-25): the same commands and the same check.
"""
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np
from scipy.io import wavfile
from scipy.signal import fftconvolve

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from lib import cli, fonts, landing, production, wip  # noqa: E402

SR, GAP = 48000, 10


def esc(s):
    return s.replace('\\', '\\\\').replace(':', '\\:').replace("'", "’").replace('%', '\\%')


def pcm(path):
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-f', 'f32le', '-ac', '2', '-ar', str(SR), '-'],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32).reshape(-1, 2)


def main(argv):
    ap = cli.parser('ab_reel', __doc__)
    ap.add_argument('--picture', required=True, metavar='FILE')
    ap.add_argument('--out', required=True, metavar='FILE')
    ap.add_argument('--span', action='append', required=True, metavar='"IN-OUT|what"')
    ap.add_argument('--version', action='append', required=True, metavar='LABEL=AUDIO')
    ap.add_argument('--wip', metavar='DIR')
    ap.add_argument('--no-land', action='store_true')
    a = ap.parse_args(argv)
    r = cli.roots(a)
    r.say('ab_reel')
    p = production.load(r.text, r.media)
    font = fonts.path('sans-bold')
    pic, out = r.m(a.picture), r.m(a.out)
    spans = [(s.split('|')[0], s.split('|', 1)[1]) for s in a.span]
    spans = [(round(float(x.split('-')[0]) * 24), round(float(x.split('-')[1]) * 24), w) for x, w in spans]
    vers = [v.split('=', 1) for v in a.version]
    audio = {lab: pcm(r.m(pth)) for lab, pth in vers}
    os.makedirs(os.path.dirname(out) or '.', exist_ok=True)
    tmp = tempfile.mkdtemp()
    parts, snd, where = [], [], []
    gap_n = GAP * SR // 24
    frame = 0
    for si, (f0, f1, what) in enumerate(spans):
        n = (f1 - f0) * SR // 24
        for vi, (lab, _) in enumerate(vers):
            pp = os.path.join(tmp, f'p{si:02d}{vi:02d}.mp4')
            txt = esc(f'{what}  ·  {lab}')
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', f'{f0 / 24:.6f}', '-i', pic, '-an',
                            '-vf', f"fps=24,trim=end_frame={f1 - f0},setpts=PTS-STARTPTS,drawtext=fontfile={font}:"
                                   f"text='{txt}':x=(w-tw)/2:y=h-th-40:fontsize=34:fontcolor=yellow:box=1:"
                                   f"boxcolor=black@0.7:boxborderw=12",
                            '-c:v', 'libx264', '-crf', '18', '-pix_fmt', 'yuv420p', '-r', '24', pp], check=True)
            parts.append(pp)
            x = audio[lab][f0 * SR // 24: f0 * SR // 24 + n].copy()
            fi, fo = int(0.02 * SR), int(0.05 * SR)
            x[:fi] *= np.linspace(0, 1, fi, dtype=np.float32)[:, None]
            x[-fo:] *= np.linspace(1, 0, fo, dtype=np.float32)[:, None]
            where.append((sum(len(s) for s in snd), lab, f0 * SR // 24, n))
            snd.append(x)
            g = os.path.join(tmp, f'g{si:02d}{vi:02d}.mp4')
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=black:s=1280x720:r=24',
                            '-vf', f'trim=end_frame={GAP}', '-c:v', 'libx264', '-crf', '18', '-pix_fmt', 'yuv420p', g], check=True)
            parts.append(g)
            snd.append(np.zeros((gap_n, 2), np.float32))
            frame += (f1 - f0) + GAP
    wav = os.path.join(tmp, 'sound.wav')
    wavfile.write(wav, SR, np.concatenate(snd))
    lst = os.path.join(tmp, 'list.txt')
    open(lst, 'w').write(''.join(f"file '{pp}'\n" for pp in parts))
    vid = os.path.join(tmp, 'picture.mp4')
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', lst, '-c', 'copy', vid], check=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', vid, '-i', wav, '-map', '0:v', '-map', '1:a', '-c:v', 'copy',
                    '-c:a', 'aac', '-b:a', '256k', '-movflags', '+faststart', out], check=True)
    # check: every piece's sound where it belongs, in the finished file
    rr = pcm(out).mean(axis=1)
    worst = 0.0
    for at, lab, src, n in where:
        seg = rr[at + SR // 10: at + n - SR // 10]
        m = audio[lab].mean(axis=1)
        W = SR // 10
        big = m[src + SR // 10 - W: src + SR // 10 - W + len(seg) + 2 * W]
        c = fftconvolve(big, seg[::-1], 'valid')
        lag = (np.argmax(c) - W) / SR * 1000
        worst = max(worst, abs(lag))
        if abs(lag) > 2:
            sys.exit(f'{out}: the {lab} piece at {at / SR:.2f} s sits {lag:+.1f} ms off')
    frames = int(subprocess.run(['ffprobe', '-v', 'error', '-count_frames', '-select_streams', 'v:0', '-show_entries',
                                 'stream=nb_read_frames', '-of', 'csv=p=0', out], capture_output=True, text=True).stdout)
    if frames != frame:
        sys.exit(f'{out}: {frames} frames, expected {frame}')
    shutil.rmtree(tmp)
    print(out, f'{len(spans)} spans x {len(vers)} versions, {frame} frames; every piece within {worst:.1f} ms of its place')
    if a.wip:
        os.makedirs(a.wip, exist_ok=True)
        dst = os.path.join(a.wip, f'{p.label_prefix}-{os.path.basename(out)}')
        wip.copy(out, dst)   # titled by its file name
        print('review copy', dst)
    if a.no_land:
        print('NOT LANDED (--no-land): in the Mac shell, run: bin/tool land ' + out)
        return 0
    return landing.land(r, [out], tool='ab_reel')


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
