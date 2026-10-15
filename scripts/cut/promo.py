#!/usr/bin/env python3
"""Trailers, teasers and vertical cuts, cut from the film by film timecode.

Usage:
  bin/tool promo edl/promo/<id>.promo.tsv [--lang en|ru|both] [--clips DIR] [--wip DIR] [--keep] [--no-land]
                 [--config release/promo.yaml] [--mix FILE] [--score FILE] [--round rN]

The production's facts come from --config (default release/promo.yaml in the text root):
  film:      edl/<cut>.edl.tsv       the picture the promos are cut from (text root; a spec's `@ film` row overrides)
  clips:     r3/clips-720            the clip set the picture is rebuilt from (media root; --clips overrides)
  mix:       r3/audio/mix/<v>.flac   the finished mix, and score: its music stem (media root; --mix, --score override)
  subtitles: subtitles/<cut>         the film's <this>.<lang>.srt, timed to the film (text root)
  title:     START                   mp4 title metadata: "<title> - <promo id> (<LANG>)"
  languages: [en, ru]                the versions built (--lang narrows to one)
  lufs:      -14                     loudness target; a spec's `@ lufs` row overrides

Spec rows (tab-separated; '#' = comment):
  @       <key>   <value>        settings: format 16:9 | 9:16;  subs none | en | both (burned in; 'en' = the EN version
                                 only, 'both' = each version in its own language);  film <edl>;  lufs <target>
  FILM    <tc in> <tc out> [x] [note]
          film timecodes HH:MM:SS:FF (the film's own timeline, 24 fps). x (9:16 only) = left edge of the 405x720
          window in the 1280x720 frame, or x0>x1 to pan across the row; default centred.
  CARD    <frames> <music> <EN text> <RU text>
          a title card on black. Text lines split by ' / '; a line starting '^' is large (a title), '~' small.
          music = a film timecode to take the score stem from, '+' = carry on from where the row before ended, '-' = none.

Picture: each FILM row is mapped, frame for frame, to the film's source clips (the same frames the film shows) and
cut again by edl_cut, so --clips DIR and a future 4K clip set rebuild every promo. Sound: FILM rows take the
finished mix (dialogue, effects, score) at the same timecodes; CARD rows take the score stem. The score is on a
120 bpm grid (12 frames a beat, bar 1 = frame 0): in- and out-points with FF = 00 or 12 keep its pulse across
every cut, and rows off the grid are listed. 8 ms fades at each join, none where one row runs straight on into
the next (a split made only to change the 9:16 crop); the last 1.5 s fade out. Loudness to the target,
true peak -1.5 dBTP (4x oversampled limiter, as in mix). The picture is trimmed in the filter graph and the
finished file's sound must match its picture within 0.05 s (2026-09-20: -frames:v once cut every promo's
sound 2.17 s short).
Writes r<N>/promo/<id>-<lang>.mp4, r<N>/promo/<id>.en.srt and .ru.srt (timed to the promo, from the film's
subtitles), lands them (masters; --no-land in the Desktop VM, said out loud), and with --wip copies of all
three to DIR as <label_prefix>-<id>-<lang>.mp4 (+ .en.srt / .ru.srt beside each, for Plex).
Fonts by role (scripts/lib/fonts.py): the cards are set in C059; the burned-in subtitles name DejaVu Sans, and
the ass filter is pointed at the folder that holds it, so a Mac with no such system font still finds the file.
Ported from START's scripts/cut/promo.py (2026-09-25): the tables became the config;
the same ffmpeg commands.
"""
import math
import os
import re
import shutil
import subprocess
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import edl_cut  # noqa: E402
from lib import cli, fonts, landing, production, wip  # noqa: E402

FPS, SR = 24, 48000
MIX = SCORE = None          # the finished mix and its score stem, set from the config in main()
DEFAULTS = {'clips': 'r3/clips-720', 'languages': ['en', 'ru'], 'lufs': -14, 'title': ''}


def tcf(s):
    h, m, sec, f = map(int, s.split(':'))
    return ((h * 60 + m) * 60 + sec) * FPS + f


def ftc(n):
    return f'{n // 86400:02d}:{n // 1440 % 60:02d}:{n // 24 % 60:02d}:{n % 24:02d}'


def film_segments(edl):
    """The film as built: (frame0, n, src, first source frame, speed, extra) per segment, n rounded as edl_cut does."""
    out, f = [], 0
    for src, a, b, audio, extra in edl_cut.rows(edl):
        sp = extra[2] if len(extra) > 2 else 1.0
        n = int(round(round((b - a) / sp, 3) * FPS))
        out.append((f, n, src, edl_cut.first_frame(a) if src not in ('BLACK', 'CARD') else 0, sp, extra))
        f += n
    return out


def spec(path, film, lufs):
    cfg = {'format': '16:9', 'subs': 'none', 'film': film, 'lufs': str(lufs)}
    rows = []
    for line in open(path, encoding='utf-8'):
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        f = line.rstrip('\n').split('\t')
        if f[0] == '@':
            cfg[f[1]] = f[2]
        elif f[0] == 'FILM':
            rows.append({'kind': 'FILM', 'f0': tcf(f[1]), 'f1': tcf(f[2]), 'x': f[3] if len(f) > 3 else '',
                         'note': f[4] if len(f) > 4 else ''})
        elif f[0] == 'CARD':
            rows.append({'kind': 'CARD', 'n': int(f[1]), 'music': f[2], 'en': f[3], 'ru': f[4]})
        else:
            sys.exit(f'bad row: {line!r}')
    t = 0
    for r in rows:
        r['n'] = r['n'] if r['kind'] == 'CARD' else r['f1'] - r['f0']
        r['t0'] = t
        t += r['n']
    return cfg, rows, t


def check(rows, segs):
    """Off-grid rows, and row edges 1-3 frames past a film cut (a stray frame of the neighbouring shot)."""
    cuts = [s[0] for s in segs]
    notes = []
    for i, r in enumerate(rows):
        if r['kind'] != 'FILM':
            continue
        # an edge shared with the neighbouring row (the film runs on) is continuous: no grid needed there
        a_free = not (i > 0 and rows[i - 1]['kind'] == 'FILM' and rows[i - 1]['f1'] == r['f0'])
        b_free = not (i + 1 < len(rows) and rows[i + 1]['kind'] == 'FILM' and rows[i + 1]['f0'] == r['f1'])
        if (a_free and r['f0'] % 12) or (b_free and r['f1'] % 12):
            notes.append(f'row {i + 1} off the beat grid: {ftc(r["f0"])}-{ftc(r["f1"])}')
        for c in cuts:
            if 1 <= c - r['f0'] <= 3:
                notes.append(f'row {i + 1} STRAY: opens with {c - r["f0"]} frame(s) of the shot before the cut at {ftc(c)}')
            if 1 <= r['f1'] - c <= 3:
                notes.append(f'row {i + 1} STRAY: ends {r["f1"] - c} frame(s) into the shot starting {ftc(c)}')
    return notes


def picture_edl(rows, segs, path):
    """FILM rows -> the source-clip rows the film shows at those frames; CARD rows -> BLACK."""
    with open(path, 'w', encoding='utf-8') as fh:
        for r in rows:
            if r['kind'] == 'CARD':
                fh.write(f'BLACK\t0\t{r["n"] / FPS:.6f}\n')
                continue
            for f0, n, src, ff, sp, extra in segs:
                a, b = max(r['f0'], f0), min(r['f1'], f0 + n)
                if b <= a:
                    continue
                if src in ('BLACK', 'CARD'):
                    fh.write(f'{src}\t0\t{(b - a) / FPS:.6f}\tkeep\t\t{extra[0]}\t{extra[1]}\n')
                    continue
                if sp != 1.0:
                    sys.exit(f'{src}: speed {sp} inside a promo row is not supported')
                s0 = (ff + (a - f0)) / FPS
                fh.write(f'{src}\t{s0:.6f}\t{s0 + (b - a) / FPS:.6f}\n')


def pcm(path):
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-f', 'f32le', '-ac', '2', '-ar', str(SR), '-'],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32).reshape(-1, 2)


def sound(rows, total, out_wav, lufs):
    mix, score = pcm(MIX), pcm(SCORE)
    spf = SR // FPS
    y = np.zeros((total * spf, 2), np.float32)
    fade = int(0.008 * SR)
    prev_end = None
    for i, r in enumerate(rows):
        # no fade where the film simply runs on into the next row (a split made for a new crop)
        run_in = i > 0 and r['kind'] == 'FILM' and rows[i - 1]['kind'] == 'FILM' and rows[i - 1]['f1'] == r['f0']
        run_out = i + 1 < len(rows) and r['kind'] == 'FILM' and rows[i + 1]['kind'] == 'FILM' and rows[i + 1]['f0'] == r['f1']
        n = r['n'] * spf
        if r['kind'] == 'FILM':
            s = mix[r['f0'] * spf: r['f0'] * spf + n]
            prev_end = r['f1']
        elif r['music'] == '-':
            s = np.zeros((n, 2), np.float32)
            prev_end = None
        else:
            m0 = prev_end if r['music'] == '+' else tcf(r['music'])
            s = score[m0 * spf: m0 * spf + n]
            prev_end = m0 + r['n']
        s = np.array(s, np.float32)
        if len(s) < n:
            s = np.pad(s, ((0, n - len(s)), (0, 0)))
        env = np.ones(n, np.float32)
        if not run_in:
            env[:fade] = np.linspace(0, 1, fade)
        if not run_out:
            env[-fade:] = np.linspace(1, 0, fade)
        y[r['t0'] * spf: r['t0'] * spf + n] = s * env[:, None]
    k = int(1.5 * SR)
    y[-k:] *= np.linspace(1, 0, k, dtype=np.float32)[:, None]
    tmp = out_wav + '.raw.f32'
    y.tofile(tmp)

    def master(g_db, lim_db):
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'f32le', '-ar', str(SR), '-ac', '2', '-i', tmp, '-af',
                        f'volume={g_db:.3f}dB,aresample=192000,alimiter=limit={10 ** (lim_db / 20):.4f}:attack=1:release=80:level=disabled,aresample={SR}',
                        '-c:a', 'pcm_s24le', out_wav], check=True)
        e = subprocess.run(['ffmpeg', '-nostats', '-i', out_wav, '-af', 'ebur128=peak=true', '-f', 'null', '-'],
                           capture_output=True, text=True).stderr
        return float(e[e.rindex('I:'):].split()[1]), float(e[e.rindex('Peak:'):].split()[1])
    I0, _ = master(0.0, 0.0)
    g = lufs - I0
    lim = -1.8
    I, TP = master(g, lim)
    for _ in range(3):
        if abs(I - lufs) <= 0.1 and TP <= -1.5:
            break
        if TP > -1.5:
            lim -= TP + 1.5 + 0.1
        g += lufs - I
        I, TP = master(g, lim)
    os.remove(tmp)
    return I, TP


def card_png(text, W, H, path):
    lines = [l.strip() for l in text.split(' / ') if l.strip()]
    im = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    k = min(W, H) / 720
    fnts, sizes = [], []
    for l in lines:
        sz = (88 if l.startswith('^') else 30 if l.startswith('~') else 56) * k
        font = fonts.path('serif') if l.startswith('~') else fonts.path('serif-bold')
        while True:
            f = ImageFont.truetype(font, int(sz))
            if d.textlength(l.lstrip('^~'), font=f) <= 0.86 * W or sz < 12:
                break
            sz *= 0.95
        fnts.append(f)
        sizes.append(int(sz))
    gap = int(22 * k)
    total = sum(sizes) + gap * (len(lines) - 1)
    y = (H - total) / 2
    for l, f, sz in zip(lines, fnts, sizes):
        t = l.lstrip('^~')
        d.text(((W - d.textlength(t, font=f)) / 2, y), t, font=f, fill=(255, 255, 255, 255))
        y += sz + gap
    im.save(path)


def srt_read(path):
    blocks = open(path, encoding='utf-8').read().strip().split('\n\n')
    out = []
    for b in blocks:
        ls = b.strip().split('\n')
        if len(ls) < 3:
            continue
        m = re.match(r'(\d+):(\d+):(\d+),(\d+) --> (\d+):(\d+):(\d+),(\d+)', ls[1])
        g = list(map(int, m.groups()))
        out.append(((g[0] * 3600 + g[1] * 60 + g[2]) + g[3] / 1000, (g[4] * 3600 + g[5] * 60 + g[6]) + g[7] / 1000, '\n'.join(ls[2:])))
    return out


def srt_time(t):
    ms = int(round(t * 1000))
    return f'{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}'


def promo_subs(rows, film_srt):
    """The film's subtitles moved into the promo's timeline; fragments under 0.5 s that lost over 40 % are dropped."""
    out = []
    for r in rows:
        if r['kind'] != 'FILM':
            continue
        a, b, p = r['f0'] / FPS, r['f1'] / FPS, r['t0'] / FPS
        for s, e, txt in film_srt:
            s1, e1 = max(s, a), min(e, b)
            if e1 - s1 <= 0 or (e1 - s1 < 0.5 and (e1 - s1) < 0.6 * (e - s)):
                continue
            out.append((s1 - a + p, e1 - a + p, txt))
    return out


def write_srt(subs, path):
    with open(path, 'w', encoding='utf-8') as fh:
        for i, (s, e, t) in enumerate(subs, 1):
            fh.write(f'{i}\n{srt_time(s)} --> {srt_time(e)}\n{t}\n\n')


def write_ass(subs, W, H, path):
    vertical = H > W
    size, mv = (40, int(H * 0.22)) if vertical else (34, 34)
    head = (f'[Script Info]\nScriptType: v4.00+\nPlayResX: {W}\nPlayResY: {H}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n'
            '[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, '
            'Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n'
            f'Style: Default,DejaVu Sans,{size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,2.6,0,2,40,40,{mv},1\n\n'
            '[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n')

    def at(t):
        cs = int(round(t * 100))
        return f'{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}'
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(head)
        for s, e, t in subs:
            t = re.sub(r'</?i>', lambda m: '{\\i1}' if m.group(0) == '<i>' else '{\\i0}', t).replace('\n', '\\N')
            fh.write(f'Dialogue: 0,{at(s)},{at(e)},Default,,0,0,0,,{t}\n')


def ass_filter(ass):
    """The ass filter with the fonts folder named, so 'DejaVu Sans' resolves without a system install.
    Both options by name: after a positional filename ffmpeg refuses a named option ("No option name near")."""
    return f"ass=filename='{ass}':fontsdir='{os.path.dirname(fonts.path('sans-bold'))}'"


def main(argv):
    ap = cli.parser('promo', __doc__)
    ap.add_argument('spec', help='edl/promo/<id>.promo.tsv, in the text root')
    ap.add_argument('--lang', choices=['en', 'ru', 'both'], help='one version, or both (default: the config\'s languages)')
    ap.add_argument('--clips', metavar='DIR', help='the clip set, relative to the media root (default: the config\'s)')
    ap.add_argument('--wip', metavar='DIR', help='copy the finished cuts and their subtitles there for review')
    ap.add_argument('--keep', action='store_true', help='keep the temp folder')
    ap.add_argument('--config', metavar='FILE', help='the facts (default: release/promo.yaml in the text root)')
    ap.add_argument('--mix', metavar='FILE', help='the finished mix (default: the config\'s)')
    ap.add_argument('--score', metavar='FILE', help='its music stem (default: the config\'s)')
    ap.add_argument('--round', metavar='rN', help='whose promo/ and clips/ (default: the highest r<N>)')
    ap.add_argument('--no-land', action='store_true', help='build without landing the outputs')
    a = ap.parse_args(argv)
    r = cli.roots(a)
    r.say('promo')
    p = production.load(r.text, r.media)
    cfgp = r.t(a.config or os.path.join('release', 'promo.yaml'))
    if not os.path.isfile(cfgp):
        sys.exit(f'promo: no config at {cfgp} — release/promo.yaml names the film, the clips, the mix, the score and '
                 'the subtitles (the template has the shape)')
    cfg0 = dict(DEFAULTS, **(production.load_yaml(cfgp) or {}))
    for k in ('film', 'mix', 'score', 'subtitles'):
        if not cfg0.get(k):
            sys.exit(f'promo: {cfgp} lacks `{k}`')
    global MIX, SCORE
    MIX, SCORE = r.m(a.mix or cfg0['mix']), r.m(a.score or cfg0['score'])
    rnd = a.round or cli.latest_round(r.media)
    if not rnd:
        sys.exit(f'promo: no r<N> folder in {r.media}: pass --round')
    clips = a.clips or cfg0['clips']
    edl_cut.TEXT, edl_cut.ROUND, edl_cut.CLIPS = r.text, rnd, clips
    os.chdir(r.media)
    spec_path = r.t(a.spec)
    pid = os.path.basename(spec_path).replace('.promo.tsv', '')
    langs = list(cfg0['languages']) if not a.lang or a.lang == 'both' else [a.lang]
    cfg, rows, total = spec(spec_path, cfg0['film'], cfg0['lufs'])
    segs = film_segments(r.t(cfg['film']))
    notes = check(rows, segs)
    for n in notes:
        print('  !', n)
    if any('STRAY' in n for n in notes):
        sys.exit('fix the stray frames first')
    out_dir = os.path.join(rnd, 'promo')
    tmp = os.path.join(out_dir, '.tmp', pid)
    os.makedirs(tmp, exist_ok=True)
    # picture, once for both languages
    pedl = os.path.abspath(os.path.join(tmp, pid + '.edl.tsv'))
    picture_edl(rows, segs, pedl)
    # the same stray-frame check the film gets, on the source-clip rows (catches cuts inside multi-shot clips)
    st = subprocess.run([sys.executable, os.path.join(HERE, 'strays.py'), '--text', r.text, '--media', r.media, pedl,
                         '--round', rnd], capture_output=True, text=True)
    print('  strays:', st.stdout.strip().splitlines()[-1] if st.stdout.strip() else st.stderr.strip()[-200:])
    if st.returncode:
        print(st.stdout)
        sys.exit('fix the stray frames first')
    pic = os.path.join(tmp, 'picture.mp4')
    edl_cut.build(pedl, pic, False, pid)
    # sound, once
    wav = os.path.join(tmp, 'sound.wav')
    I, TP = sound(rows, total, wav, float(cfg['lufs']))
    # subtitles, both languages, timed to the promo
    subs, made = {}, []
    for lg in ('en', 'ru'):
        srt = r.t(f"{cfg0['subtitles']}.{lg}.srt")
        if not os.path.isfile(srt):
            sys.exit(f'promo: no subtitles at {srt}')
        subs[lg] = promo_subs(rows, srt_read(srt))
        write_srt(subs[lg], os.path.join(out_dir, f'{pid}.{lg}.srt'))
        made.append(os.path.join(out_dir, f'{pid}.{lg}.srt'))
    vertical = cfg['format'] == '9:16'
    W, H = (720, 1280) if vertical else (1280, 720)
    for lg in langs:
        inputs, fc, last = ['-i', pic, '-i', wav], [], '[0:v]'
        if vertical:
            xs = []
            for rw in rows:
                if rw['kind'] == 'CARD' or not rw['x']:
                    x0 = x1 = (1280 - 405) / 2
                else:
                    x0, x1 = (float(v) for v in rw['x'].split('>')) if '>' in rw['x'] else (float(rw['x']),) * 2
                xs.append((rw['t0'] / FPS, (rw['t0'] + rw['n']) / FPS, x0, x1))
            expr = '0'
            for t0, t1, x0, x1 in reversed(xs):
                expr = f'if(lt(t,{t1:.4f}),{x0:.1f}+({x1 - x0:.1f})*(t-{t0:.4f})/{t1 - t0:.4f},{expr})'
            fc.append(f"{last}crop=405:720:x='{expr}':y=0,scale={W}:{H}:flags=lanczos,setsar=1[vc]")
            last = '[vc]'
        k = 2
        for i, rw in enumerate(rows):
            if rw['kind'] != 'CARD':
                continue
            png = os.path.join(tmp, f'card{i}-{lg}-{W}x{H}.png')
            card_png(rw[lg], W, H, png)
            d, t0 = rw['n'] / FPS, rw['t0'] / FPS
            inputs += ['-loop', '1', '-t', f'{d:.4f}', '-framerate', str(FPS), '-i', png]
            fc.append(f'[{k}:v]format=rgba,fade=t=in:st=0:d=0.5:alpha=1,fade=t=out:st={max(0, d - 0.5):.4f}:d=0.5:alpha=1,'
                      f'setpts=PTS-STARTPTS+{t0:.4f}/TB[c{i}]')
            fc.append(f'{last}[c{i}]overlay=0:0:eof_action=pass[v{i}]')
            last = f'[v{i}]'
            k += 1
        burn = cfg['subs'] == 'both' or (cfg['subs'] == 'en' and lg == 'en')
        if burn:
            ass = os.path.join(tmp, f'{lg}-{W}x{H}.ass')
            write_ass(subs[lg], W, H, ass)
            fc.append(f'{last}{ass_filter(ass)}[vs]')
            last = '[vs]'
        fc.append(f'{last}trim=end_frame={total},format=yuv420p[vout]')
        out = os.path.join(out_dir, f'{pid}-{lg}.mp4')
        title = f"{cfg0['title']} - {pid} ({lg.upper()})" if cfg0['title'] else f'{pid} ({lg.upper()})'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', *inputs, '-filter_complex', ';'.join(fc), '-map', '[vout]', '-map', '1:a',
                        '-c:v', 'libx264', '-crf', '16', '-preset', 'medium', '-c:a', 'aac', '-b:a', '256k',
                        '-metadata', f'title={title}', '-metadata:s:a:0', 'language=rus', '-movflags', '+faststart',
                        out + '.part.mp4'], check=True)
        os.replace(out + '.part.mp4', out)
        nf = int(subprocess.run(['ffprobe', '-v', 'error', '-count_frames', '-select_streams', 'v:0', '-show_entries',
                                 'stream=nb_read_frames', '-of', 'csv=p=0', out], capture_output=True, text=True).stdout)
        ad = float(subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_entries', 'stream=duration',
                                   '-of', 'csv=p=0', out], capture_output=True, text=True).stdout)
        if abs(ad - total / FPS) > 0.05 or nf != total:
            sys.exit(f'{out}: picture {nf} frames / sound {ad:.3f} s - expected {total} frames and {total / FPS:.3f} s')
        print(f'{out}: {W}x{H}, {nf}/{total} frames ({total / FPS:.2f} s), sound {ad:.3f} s, {I} LUFS, {TP} dBTP, '
              f'subtitles {"burned " + lg.upper() if burn else "none burned"}')
        made.append(out)
        if a.wip:
            os.makedirs(a.wip, exist_ok=True)
            wip.copy(out, os.path.join(a.wip, f'{p.label_prefix}-{pid}-{lg}.mp4'))   # titled by its file name
    if a.wip:
        for lg in langs:
            for sl in ('en', 'ru'):   # Plex pairs <video>.<lang>.srt with the video
                shutil.copyfile(os.path.join(out_dir, f'{pid}.{sl}.srt'), os.path.join(a.wip, f'{p.label_prefix}-{pid}-{lg}.{sl}.srt'))
    if not a.keep:
        shutil.rmtree(tmp)
    if a.no_land:
        print('NOT LANDED (--no-land): in the Mac shell, run: bin/tool land ' + ' '.join(made))
        return 0
    return landing.land(r, made, tool='promo')


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
