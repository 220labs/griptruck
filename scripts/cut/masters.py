#!/usr/bin/env python3
"""The four deliverables of a master number, from a finished picture and a finished mix, the same way every time.

Usage:
  bin/tool masters --cut film-rough-v37-720 --mix r3/audio/mix/film-mix-v7.flac --master film-mix-v10
                   [--only en,ru,screener1,screener2,screener,review] [--review DIR] [--srt FILE] [--round rN] [--no-land]

Writes, in r<N>/cut/ (the media root):
  <master>-720.mp4      EN master: the cut's picture stream-copied, the mix as AAC 192k
  <master>-720-ru.mp4   RU master: the same from <cut>-ru.mp4
  <master>-720-en.mp4   EN screener: the film's English subtitles (--srt, default release/promo.yaml's
                        `subtitles` + .en.srt) burned in with promo's style (DejaVu Sans Bold 34 px at 720,
                        outline 2.6), CRF 17, the mix as AAC 192k
  review/<label_prefix>-<master>.mp4 (+ -index.md)   the review copy (clip names and timecode burned in),
                        picture stream-copied from --review DIR's <label_prefix>-<cut>.mp4 (edl_cut --wip DIR
                        --no-clean), the mix as AAC 192k
and checks each: the cut's own frame count, and the sound within one frame of the picture. The three masters
are landed (--no-land in the Desktop VM); the review copy is for /wip and is not.
The screener's picture is encoded in halves (--only screener1, then screener2, then screener) because a shell
with a 3-minute limit stops a call; the halves share every setting, so they join with -c copy.
Until 2026-09-23 these were built by hand; the commands were in RENDER-LOG and nowhere else.
Ported from START's scripts/cut/masters.py (2026-09-25): the same ffmpeg commands.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import promo  # noqa: E402
from lib import cli, landing, production, wip  # noqa: E402


def frames(p):
    return int(subprocess.run(['ffprobe', '-v', 'error', '-count_frames', '-select_streams', 'v:0', '-show_entries',
                               'stream=nb_read_frames', '-of', 'csv=p=0', p], capture_output=True, text=True).stdout)


def adur(p):
    return float(subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_entries', 'stream=duration',
                                 '-of', 'csv=p=0', p], capture_output=True, text=True).stdout)


def mux(pic, mix, out, vf=None, title=None):
    v = ['-c:v', 'copy'] if not vf else ['-vf', vf, '-c:v', 'libx264', '-crf', '17', '-preset', 'medium', '-pix_fmt', 'yuv420p']
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', pic, '-i', mix, '-map', '0:v:0', '-map', '1:a:0', *v,
                    '-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-movflags', '+faststart',
                    '-metadata:s:a:0', 'language=rus', *(['-metadata', f'title={title}'] if title else []),
                    out + '.part.mp4'], check=True)
    os.replace(out + '.part.mp4', out)
    n, want = frames(out), frames(pic)
    d = adur(out)
    ok = n == want and abs(d - n / 24) <= 1 / 24
    print(f'{"OK  " if ok else "FAIL"} {out}: {n}/{want} frames, sound {d:.3f} s', flush=True)
    if not ok:
        sys.exit(1)


def main(argv):
    ap = cli.parser('masters', __doc__)
    ap.add_argument('--cut', required=True, help='the finished picture, r<N>/cut/<cut>.mp4 (and <cut>-ru.mp4)')
    ap.add_argument('--mix', required=True, help='the finished mix (media root)')
    ap.add_argument('--master', required=True, help='the master number, e.g. film-mix-v10')
    ap.add_argument('--only', default='en,ru,screener1,screener2,screener,review', help='which of the four (comma-separated)')
    ap.add_argument('--review', metavar='DIR', help='the review copy of the cut and its index (edl_cut --wip DIR --no-clean)')
    ap.add_argument('--srt', metavar='FILE', help='the English subtitles to burn into the screener (text root)')
    ap.add_argument('--round', metavar='rN', help='whose cut/ (default: the highest r<N>)')
    ap.add_argument('--no-land', action='store_true', help='build without landing the masters')
    a = ap.parse_args(argv)
    r = cli.roots(a)
    r.say('masters')
    p = production.load(r.text, r.media)
    rnd = a.round or cli.latest_round(r.media)
    if not rnd:
        sys.exit(f'masters: no r<N> folder in {r.media}: pass --round')
    srt = a.srt
    if not srt:
        cfgp = r.t(os.path.join('release', 'promo.yaml'))
        cfg = production.load_yaml(cfgp) if os.path.isfile(cfgp) else {}
        if not (cfg or {}).get('subtitles'):
            sys.exit('masters: --srt FILE, or `subtitles:` in release/promo.yaml, names the English subtitles')
        srt = f"{cfg['subtitles']}.en.srt"
    srt = r.t(srt)
    os.chdir(r.media)
    cut, mix, m = a.cut, r.m(a.mix), a.master
    only = a.only.split(',')
    cd = os.path.join(rnd, 'cut')
    made = []
    if 'en' in only:
        mux(f'{cd}/{cut}.mp4', mix, f'{cd}/{m}-720.mp4')
        made.append(f'{cd}/{m}-720.mp4')
    if 'ru' in only:
        mux(f'{cd}/{cut}-ru.mp4', mix, f'{cd}/{m}-720-ru.mp4')
        made.append(f'{cd}/{m}-720-ru.mp4')
    half = frames(f'{cd}/{cut}.mp4') // 2 if any(o.startswith('screener') for o in only) else 0
    for i, (s0, s1) in ((1, (0, half)), (2, (half, None))):
        if f'screener{i}' not in only:
            continue
        ass = f'{cd}/.{m}-en.ass'
        promo.write_ass(promo.srt_read(srt), 1280, 720, ass)
        trim = f'trim=start_frame={s0}' + (f':end_frame={s1}' if s1 else '') + ',setpts=PTS-STARTPTS'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', f'{cd}/{cut}.mp4', '-an', '-vf', f"{promo.ass_filter(ass)},{trim}",
                        '-c:v', 'libx264', '-crf', '17', '-preset', 'medium', '-pix_fmt', 'yuv420p',
                        f'{cd}/.{m}-en-half{i}.mp4'], check=True)
        print(f'screener half {i}: {frames(f"{cd}/.{m}-en-half{i}.mp4")} frames', flush=True)
    if 'screener' in only:
        lst = f'{cd}/.{m}-en-halves.txt'
        open(lst, 'w').write(''.join(f"file '{os.path.abspath(f'{cd}/.{m}-en-half{i}.mp4')}'\n" for i in (1, 2)))
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', lst, '-c', 'copy',
                        f'{cd}/.{m}-en-picture.mp4'], check=True)
        mux(f'{cd}/.{m}-en-picture.mp4', mix, f'{cd}/{m}-720-en.mp4')
        made.append(f'{cd}/{m}-720-en.mp4')
    if a.review and 'review' in only:
        os.makedirs(f'{cd}/review', exist_ok=True)
        pre = p.label_prefix
        rv = f'{cd}/review/{pre}-{m}.mp4'   # titled by its own name, not the cut's it was muxed from (lib/wip.py)
        mux(os.path.join(a.review, f'{pre}-{cut}.mp4'), mix, rv, title=wip.title(rv))
        idx = open(os.path.join(a.review, f'{pre}-{cut}-index.md'), encoding='utf-8').read()
        head, _, rest = idx.partition('\n')
        open(f'{cd}/review/{pre}-{m}-index.md', 'w', encoding='utf-8').write(
            f'{head.split(" — ")[0]} — review index ({os.path.splitext(os.path.basename(mix))[0]} audio: master {m})\n{rest}')
    if not made:
        return 0
    if a.no_land:
        print('NOT LANDED (--no-land): in the Mac shell, run: bin/tool land ' + ' '.join(made))
        return 0
    return landing.land(r, made, tool='masters')


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
