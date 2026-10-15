#!/usr/bin/env python3
"""Build a cut from an edit list, and a review copy with every segment's clip name burned in.

Usage:
  bin/tool edl_cut edl/<cut-id>.edl.tsv [--wip DIR] [--no-clean] [--round rN] [--no-land]
Edit lists live in edl/ under the text root (tracked; a worktree cuts from its own EDLs);
media in r<N>/ under the media root (not tracked). The tool runs in the media root, so the
clip paths in an EDL (r3/clips/…) and card images (@assets/…) resolve there; a nested
.edl.tsv source resolves against the text root first. --round (default: the highest r<N> in
the media root; r3 for START) is the round whose clips/, cut/ and audio/ are meant wherever
this help says r3. The review copy's file name starts with production.yaml's label_prefix
(default: the production's slug). A clean full cut is a master: it ends by `bin/tool land`ing it
(--no-land skips that, e.g. in the Desktop VM, and says so). Fonts by role through
scripts/lib/fonts.py. Ported from START's edl_cut.py
(2026-09-23): the same ffmpeg commands.

EDL rows (tab-separated, '#' = comment):  source  in  out  [audio]  [note]
  source : path to a clip, another .edl.tsv (its rows are inlined, so sequences stay traceable),
           or BLACK (a black credit slot of out-in seconds, silent; the note is shown in the review copy)
           or CARD  (a title card on black: col 6 = text, lines split by ' / ', a line starting with '~' is
                     set small, '^' large (a title), and '+' fades in 2 s into the card (or '+N' at N s) even on a cut card
                     (prefixes combine, e.g. '+~'); a line '@<image>' or '@<image>|<height px at 720>' puts a picture
                     (a QR code) under the text, centred, fading with the card; col 7 = fade (0.6 s in and out) |
                     cut (hard in, hard out))
  in/out : seconds in the source
  col 8  : speed (optional, e.g. 1.5 = 1.5x faster: picture and sound; out-in is source time, the cut gets (out-in)/speed)
  audio  : keep (default) | from:<clip>@<in> (an L/J-cut: this picture with that clip's sound from <in>)
           | voice-out  (voice-out = the segment's own audio low-passed at 150 Hz, 8 poles,
           -8 dB: room tone without words; a review stopgap, not a final mix)
           | mute  (the segment carries no sound of its own: silence here, and mix.py leaves all three stems
           empty over it, so only a bed fills it)
Options:
  --clips DIR   take each r3/clips/<name>.mp4 (picture and from: sound) from DIR/<name>.mp4 when that file exists, e.g.
                r3/clips-720 (Flow's 720p upscales; derived clips rebuilt by scripts/cut/derive.py). The cut id gets
                '-<DIR name without clips->' (film-rough-v31 -> film-rough-v31-720); sources not found in DIR are listed.
  --size WxH    output size (default 1280x720); cards, labels and timecode scale with the height.
  --rows i:j    render only top-level rows i..j of the edit list (1-based, inclusive), to
                r3/cut/<cut-id>-rows<i>-<j>.mp4. For shells with a time limit: render the film in two or three
                pieces and join them with `ffmpeg -f concat -c copy` (same encoder settings, so the join is
                exact). Frame counts add up to the whole cut's; check them before joining.
  --audio-only  render only the sound, to r3/audio/<cut-id>-production.wav (48 kHz, 24-bit PCM): the same graph,
                the same joins, no picture. This is what the mix separates and conforms to; a sound-only recut
                (a changed audio flag) does not need the picture rebuilt.
  --prores      the clean cut as r3/cut/<cut-id>.mov, ProRes 422 HQ 10-bit, picture only unless --audio: a segment whose
                source is ProRes at --size and plays at speed 1 is copied packet for packet (checked frame by frame
                against its source), cards, black and re-timed segments are encoded as the same ProRes; no H.264 generation
                between Topaz and the upload (2026-09-27). E.g. --clips r3/clips-4k --size 3840x2160 --prores.
  --audio FILE  with --prores: a finished mix (r3/audio/mix/film-mix-v7.flac) muxed as 24-bit PCM; without it, no sound
                (the production sound is --audio-only's).
  --cards FILE  translate every card line through FILE (tab-separated: English line, other language), e.g.
                edl/cards-ru.tsv for the Russian version; the cut id gets '-<lang>' from the file name
                (cards-ru.tsv -> -ru). A card line missing from FILE stops the build. A key can also be a
                whole card (lines joined by ' / '), for grammar that spans lines.
Writes r3/cut/<cut-id>.mp4 unless --no-clean (clean, 1280x720, 24 fps, 40 ms audio fades at every join) and, with --wip,
DIR/<label_prefix>-<cut-id>.mp4 (the review copy) with:
  top-right    a running HH:MM:SS:FF timecode at 24 fps, from 00:00:00:00 (call feedback off this)
  top-left     "<clip file>  <in>–<out> s" on every segment  (+ " · voice out", " · own sound muted" or " · sound <file>")
  bottom-right "<cut id>"
and DIR/<label_prefix>-<cut-id>-index.md: one row per segment with its start timecode, clip file and in/out.
Each segment keeps its own audio at its own offset - never another take's sound under a picture.
"""
import sys, os, subprocess, re, math, shutil, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from lib import cli, fonts, landing, production, wip  # noqa: E402
AUDIO_ONLY = False
PRORES = False     # --prores: build() encodes its picture as ProRes 422 HQ (one segment at a time, from build_prores)
ROUND = 'r3'      # --round; default: the highest r<N> in the media root
TEXT = '.'         # --text; a nested .edl.tsv source resolves here first

def tc(sec):
    f = int(round(sec * 24))
    return f'{f // 86400:02d}:{f // 1440 % 60:02d}:{f // 24 % 60:02d}:{f % 24:02d}'

def edl_path(path):
    # an edit list is text: the text root's copy when it has one (a worktree edits its own), else as given
    t = os.path.join(TEXT, path)
    return t if not os.path.isabs(path) and os.path.exists(t) else path

def rows(path, depth=0):
    for line in open(edl_path(path)):
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        f = line.rstrip('\n').split('\t')
        src, a, b = f[0], float(f[1]), float(f[2])
        audio = f[3] if len(f) > 3 and f[3] else 'keep'
        extra = (f[5] if len(f) > 5 else '', f[6] if len(f) > 6 and f[6] else 'fade',
                 float(f[7]) if len(f) > 7 and f[7] else 1.0)
        if src.endswith('.edl.tsv'):
            # inline a sub-edit, clipped to [a, b] of that sub-cut's timeline
            t = 0.0
            for r in rows(src, depth + 1):
                sp = r[4][2] if len(r[4]) > 2 else 1.0
                d = (r[2] - r[1]) / sp          # timeline length of the sub-row
                s0, s1 = max(a, t), min(b, t + d)
                if s1 - s0 > 0.02:
                    yield (r[0], r[1] + (s0 - t) * sp, r[1] + (s1 - t) * sp, r[3] if audio == 'keep' else audio, r[4])
                t += d
        else:
            yield (src, a, b, audio, extra)

def first_frame(a):
    # the source frame a segment starts on: an in-point within 2 ms past a frame time means that frame
    # (in-points are written to 4 decimals, e.g. 1.2917 for frame 31 = 1.291667)
    return math.ceil(a * 24 - 0.05)

def snap(a):
    # seek to half a millisecond before that frame, so ffmpeg (which starts at the first frame with
    # time >= -ss) takes exactly it. Before 2026-09-19 the in-point went out as f'{a:.3f}': 1.2917
    # became 1.292 and 4.1666 became 4.167, both past their frame, so every such segment started a
    # frame late and ended on the first frame of the next shot - the recurring stray frame.
    return max(0.0, first_frame(a) / 24 - 0.0005)

def esc(s):
    return s.replace('\\', r'\\').replace(':', r'\:').replace("'", r"\'")

W, H = 1280, 720   # --size
CLIPS = None       # --clips
CARDS = None       # --cards: {English line: translated line}

def load_cards(path):
    t = {}
    for line in open(path, encoding='utf-8'):
        if line.strip() and not line.startswith('#'):
            en, tr = line.rstrip('\n').split('\t')
            t[en] = tr
    return t

def translate(text):
    # each ' / ' line keeps its prefixes (+N fade-in, ~ small, ^ large); only the words are swapped.
    # A whole card (its lines' words joined by ' / ') found in --cards is translated as one, line for line,
    # so grammar that spans lines (a plural verb over two names) comes out right.
    ls = [l.strip() for l in text.split(' / ') if l.strip()]
    parts = [re.match(r'^(\+(?:\d+(?:\.\d+)?)?)?([~^]?)(.*)$', l) for l in ls if not l.startswith('@')]
    whole = ' / '.join(m.group(3) for m in parts)
    if len(parts) > 1 and whole in CARDS:
        tr = CARDS[whole].split(' / ')
        if len(tr) != len(parts):
            sys.exit(f'--cards: {whole!r} has {len(parts)} lines, its translation {len(tr)}')
        it = iter(tr)
        return ' / '.join(l if l.startswith('@') else (lambda m: (m.group(1) or '') + m.group(2) + next(it))(
            re.match(r'^(\+(?:\d+(?:\.\d+)?)?)?([~^]?)(.*)$', l)) for l in ls)
    out = []
    for l in ls:
        if l.startswith('@'):            # an image line is a path, not words: never translated
            out.append(l); continue
        m = re.match(r'^(\+(?:\d+(?:\.\d+)?)?)?([~^]?)(.*)$', l)
        pre, style, words = m.group(1) or '', m.group(2), m.group(3)
        if words not in CARDS:
            sys.exit(f'card line not in --cards: {words!r}')
        out.append(pre + style + CARDS[words])
    return ' / '.join(out)

def sub(path):
    # r3/clips/<name>.mp4 -> CLIPS/<name>.mp4 when --clips is given and that file exists
    # (.mov too: Topaz exports ProRes); r3 is --round
    if CLIPS and path.startswith(f'{ROUND}/clips/'):
        stem = os.path.splitext(os.path.basename(path))[0]
        for ext in ('.mp4', '.mov'):
            alt = os.path.join(CLIPS, stem + ext)
            if os.path.exists(alt):
                return alt
    return path

def build(edl, out, labels, cut_id):
    segs = []
    for src, a, b, audio, extra in rows(edl):
        if audio.startswith('from:'):
            asrc, ain = audio[5:].rsplit('@', 1)
            audio = f'from:{sub(asrc)}@{ain}'
        segs.append((sub(src), a, b, audio, extra))
    k_ = H / 720          # type and box sizes scale with the frame height
    ins, fc, cat, t = [], [], [], 0.0
    index = []
    k = 0
    for i, (src, a, b, audio, extra) in enumerate(segs):
        sp = extra[2] if len(extra) > 2 else 1.0
        card_img, ii = None, None
        d = round((b - a) / sp, 3)      # output length (source length / speed)
        dsrc = round(b - a, 3)
        if src in ('BLACK', 'CARD'):
            ins += ['-f', 'lavfi', '-t', f'{d:.3f}', '-i', f'color=black:s={W}x{H}:r=24',
                    '-f', 'lavfi', '-t', f'{d:.3f}', '-i', 'anullsrc=r=48000:cl=stereo']
            vi, ai = k, k + 1; k += 2
            if src == 'CARD':
                img = [l.strip()[1:] for l in extra[0].split(' / ') if l.strip().startswith('@')]
                if img:
                    ipath, _, ih = img[0].partition('|')
                    card_img = (ipath, round(k_ * (float(ih) if ih else 150)))
                    ins += ['-loop', '1', '-framerate', '24', '-t', f'{d:.3f}', '-i', ipath]
                    ii = k; k += 1
        else:
            ins += ['-ss', f'{snap(a):.6f}', '-t', f'{dsrc:.3f}', '-i', src]
            vi = ai = k; k += 1
        if audio.startswith('from:'):
            # L/J-cut: this picture, another clip's sound (from:<clip>@<in seconds>), same length
            asrc, ain = audio[5:].rsplit('@', 1)
            ins += ['-ss', f'{float(ain):.3f}', '-t', f'{dsrc:.3f}', '-i', asrc]
            ai = k; k += 1
        n = int(round(d * 24))  # every segment is exactly n frames and n/24 s of audio, so joins never drift
        v = (f'[{vi}:v]setpts=(PTS-STARTPTS)/{sp},fps=24,tpad=stop_mode=clone:stop={n},trim=end_frame={n},setpts=PTS-STARTPTS,'
             f'scale={W}:{H}:flags=lanczos,setsar=1')
        if src == 'CARD':
            lines = [l.strip() for l in (translate(extra[0]) if CARDS else extra[0]).split(' / ') if l.strip()]
            lines = [l for l in lines if not l.startswith('@')]
            # '+' = fade in 2 s into the card; '+N' = fade in N s into the card (e.g. '+3.33~featuring ...')
            fin = [float(m.group(1)) if (m := re.match(r'^\+(\d+(?:\.\d+)?)', l)) else (2.0 if l.startswith('+') else None) for l in lines]
            core = [re.sub(r'^\+(\d+(?:\.\d+)?)?', '', l) for l in lines]
            sizes = [round(k_ * (30 if c.startswith('~') else 88 if c.startswith('^') else 56)) for c in core]
            gap = round(22 * k_)
            total = sum(sizes) + gap * (len(lines) - 1) + (gap * 2 + card_img[1] if card_img else 0)
            y = H / 2 - total / 2
            fade = extra[1] != 'cut'
            alpha = f"if(lt(t,0.6),t/0.6,if(gt(t,{d - 0.6:.3f}),({d:.3f}-t)/0.6,1))" if fade else '1'
            for l, c, sz, f0 in zip(lines, core, sizes, fin):
                txt = esc(c.lstrip('~^'))
                font = fonts.path('serif') if c.startswith('~') else fonts.path('serif-bold')
                la = f"if(lt(t,{f0}),0,if(lt(t,{f0 + 0.8}),(t-{f0})/0.8,1))" if f0 is not None else alpha
                v += f",drawtext=text='{txt}':fontfile={font}:fontsize={sz}:fontcolor=white:x=(w-tw)/2:y={y:.0f}:alpha='{la}'"
                y += sz + gap
        if labels:
            take = os.path.basename(src)
            l1 = (f'CREDIT SLOT  {d:.2f} s' if src == 'BLACK' else f'CARD ({extra[1]})  {d:.2f} s' if src == 'CARD' else f'{take}  {a:.2f}–{b:.2f} s' + (f'  x{sp:g}' if sp != 1.0 else '')) + ('  · voice out' if audio == 'voice-out' else '  · own sound muted' if audio == 'mute' else f'  · sound {os.path.basename(audio[5:])}' if audio.startswith('from:') else '')
            box = f'box=1:boxcolor=black@0.6:boxborderw={round(8 * k_)}:fontcolor=white:fontsize={round(26 * k_)}:fontfile=' + fonts.path('sans-bold')
            v += f",drawtext=text='{esc(l1)}':x={round(16 * k_)}:y={round(16 * k_)}:{box}"
            index.append((t, l1))
        if not AUDIO_ONLY:
            if card_img:
                iy = round(y + gap)                      # y is just past the last text line
                fc.append(v + f'[c{i}]')
                fade_in = 'fade=t=in:st=0:d=0.6:alpha=1,' if extra[1] != 'cut' else ''
                fc.append(f'[{ii}:v]format=rgba,scale=-1:{card_img[1]}:flags=lanczos,'
                          f'{fade_in}fade=t=out:st={max(0, d - 0.6):.3f}:d=0.6:alpha=1[q{i}]')
                fc.append(f'[c{i}][q{i}]overlay=x=(main_w-overlay_w)/2:y={iy}:format=auto[v{i}]')
            else:
                fc.append(v + f'[v{i}]')
        af = f'[{ai}:a]asetpts=PTS-STARTPTS,' + (f'atempo={sp},' if sp != 1.0 else '') + 'aresample=48000,'
        af += f'aformat=channel_layouts=stereo,apad=whole_dur={n / 24:.6f},atrim=end={n / 24:.6f},asetpts=PTS-STARTPTS'
        if audio == 'voice-out':
            af += ',lowpass=f=150,lowpass=f=150,lowpass=f=150,lowpass=f=150,volume=-8dB'
        elif audio == 'mute':
            af += ',volume=0'
        af += f',afade=t=in:d=0.04,afade=t=out:st={max(0, n / 24 - 0.04):.3f}:d=0.04[a{i}]'
        fc.append(af)
        cat.append(f'[a{i}]' if AUDIO_ONLY else f'[v{i}][a{i}]')
        t += n / 24
    if AUDIO_ONLY:
        fc.append(''.join(cat) + f'concat=n={len(segs)}:v=0:a=1[a]')
        subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', *ins, '-filter_complex', ';'.join(fc),
                        '-map', '[a]', '-c:a', 'pcm_s24le', '-ar', '48000', out + '.part.wav'], check=True)
        os.replace(out + '.part.wav', out)
        return segs, t
    if labels:
        fc.append(''.join(cat) + f'concat=n={len(segs)}:v=1:a=1[vc][a]')
        m, b8, b6 = round(16 * k_), round(8 * k_), round(6 * k_)
        fc.append(f"[vc]drawtext=fontfile={fonts.path('mono-bold')}:timecode='00\\:00\\:00\\:00':rate=24:x=w-tw-{m}:y={m}:fontsize={round(30 * k_)}:"
                  f"fontcolor=white:box=1:boxcolor=black@0.6:boxborderw={b8},"
                  f"drawtext=text='{esc(cut_id)}':fontfile={fonts.path('sans-bold')}:fontsize={round(20 * k_)}:fontcolor=white@0.8:x=w-tw-{m}:y=h-th-{m}:"
                  f"box=1:boxcolor=black@0.45:boxborderw={b6}[v]")
        with open(out[:-4] + '-index.md', 'w') as fh:
            fh.write(f'# {cut_id} — review index\n\nTimecodes are the burned-in HH:MM:SS:FF at 24 fps (top right). '
                     f'Total {tc(t)}, {len(segs)} segments.\n\n| Start | Clip  in–out |\n|---|---|\n')
            for t_, l_ in index:
                fh.write(f'| {tc(t_)} | {l_} |\n')
    else:
        fc.append(''.join(cat) + f'concat=n={len(segs)}:v=1:a=1[v][a]')
    if PRORES:  # one card or re-timed segment for build_prores; its sound is thrown away there
        subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', *ins, '-filter_complex', ';'.join(fc), '-map', '[v]', '-map', '[a]',
                        *PRORES_ENC, '-c:a', 'pcm_s16le', out], check=True)
        return segs, t
    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', *ins, '-filter_complex', ';'.join(fc), '-map', '[v]', '-map', '[a]',
                    '-c:v', 'libx264', '-crf', '23' if labels else '17', *(['-preset', 'veryfast'] if labels else []), '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k',
                    # the labelled copy is the review copy: titled by its file name (lib/wip.py)
                    '-metadata', f'title={wip.title(out) if labels else cut_id}', out + '.part.mp4'], check=True)
    os.replace(out + '.part.mp4', out)  # a killed build never leaves a half-written cut under the real name
    return segs, t

# --prores (2026-09-27: no re-encode, and no quality lost from the upscaled masters). ProRes is intra-only, so a segment of a ProRes source at the cut's own
# size and speed is its packets, copied; only cards, black and re-timed segments are encoded, as the same
# ProRes 422 HQ 10-bit Topaz writes, and the parts are joined with the concat demuxer, -c copy.
PRORES_ENC = ['-c:v', 'prores_ks', '-profile:v', '3', '-pix_fmt', 'yuv422p10le', '-vendor', 'apl0',
              '-video_track_timescale', '12288']

def probe_v(path, entries):
    # by name: ffprobe prints fields in its own order, not the order asked for
    r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', f'stream={entries}',
                        '-of', 'json', path], capture_output=True, text=True)
    return (json.loads(r.stdout or '{}').get('streams') or [{}])[0]

def decoded_md5(path, vf):
    # one md5 per decoded frame (the picture, not the packet), so a copy is checked against its source
    r = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-map', '0:v:0', '-vf', vf, '-f', 'framemd5', '-'],
                       capture_output=True, text=True, check=True)
    return [l.split(',')[-1].strip() for l in r.stdout.splitlines() if l and not l.startswith('#')]

def build_prores(edl, out, cut_id, audio=None):
    """Picture as ProRes 422 HQ in out (.mov), plus `audio` (a finished mix) as 24-bit PCM when given.
    Every copied segment is checked frame by frame against its source, and every part's frame count
    against the edit list; any mismatch stops the build."""
    global PRORES
    tmp = os.path.join(os.path.dirname(out), '.' + os.path.basename(out) + '.parts')  # dot: land skips it
    os.makedirs(tmp, exist_ok=True)
    parts, t, copied, encoded = [], 0.0, 0, 0
    for i, (src, a, b, _audio, extra) in enumerate(rows(edl)):
        src = sub(src)
        sp = extra[2] if len(extra) > 2 else 1.0
        n = int(round(round((b - a) / sp, 3) * 24))
        f0 = first_frame(a)
        part = os.path.join(tmp, f'{i:03d}.mov')
        # only what PRORES_ENC writes is copied: concat -c copy would label a 4444 or LT packet as the
        # first part's profile without a word, and a 23.976 source would drift (review, 2026-09-27)
        v = probe_v(src, 'codec_name,profile,pix_fmt,width,height,r_frame_rate,nb_frames') \
            if src not in ('BLACK', 'CARD') else {}
        if (v.get('codec_name'), v.get('profile'), v.get('pix_fmt'), v.get('width'), v.get('height'),
                v.get('r_frame_rate')) == ('prores', 'HQ', 'yuv422p10le', W, H, '24/1') \
                and sp == 1.0 and int(v.get('nb_frames', 0)) > f0:
            # output-side -ss drops packets before the in-frame; a quarter frame early keeps rounding off it.
            # An out-point past the source's last frame is build()'s tpad clone: here the last packet, repeated.
            k = min(n, int(v['nb_frames']) - f0)
            body = part if k == n else part + '.body.mov'
            subprocess.run(['ffmpeg', '-y', '-v', 'error', '-i', src, '-ss', f'{(f0 - 0.25) / 24:.6f}', '-frames:v', str(k),
                            '-map', '0:v:0', '-c:v', 'copy', '-an', '-video_track_timescale', '12288', body], check=True)
            want = decoded_md5(src, f'trim=start_frame={f0}:end_frame={f0 + k}')
            if k < n:
                last = part + '.last.mov'
                subprocess.run(['ffmpeg', '-y', '-v', 'error', '-i', src, '-ss', f'{(f0 + k - 1.25) / 24:.6f}', '-frames:v', '1',
                                '-map', '0:v:0', '-c:v', 'copy', '-an', '-video_track_timescale', '12288', last], check=True)
                with open(part + '.txt', 'w') as fh:
                    fh.writelines(f"file '{os.path.abspath(p)}'\n" for p in [body] + [last] * (n - k))
                subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'concat', '-safe', '0', '-i', part + '.txt', '-c', 'copy', part],
                               check=True)
                want += want[-1:] * (n - k)
            got = decoded_md5(part, 'null')
            if len(got) != n or got != want:
                sys.exit(f'--prores: segment {i + 1} ({src} from frame {f0}) is not its source\'s frames')
            copied += 1
        else:
            one = os.path.join(tmp, f'{i:03d}.edl.tsv')
            with open(one, 'w') as fh:
                fh.write('\t'.join([src, repr(a), repr(b), 'keep', '', extra[0], extra[1], repr(sp)]) + '\n')
            PRORES = True
            try:
                build(one, part, False, cut_id)
            finally:
                PRORES = False
            encoded += 1
        got = int(subprocess.run(['ffprobe', '-v', 'error', '-count_packets', '-select_streams', 'v:0', '-show_entries',
                                  'stream=nb_read_packets', '-of', 'csv=p=0', part], capture_output=True, text=True).stdout)
        if got != n:
            sys.exit(f'--prores: segment {i + 1} ({src}) has {got} frames, the edit list wants {n}')
        parts.append(part)
        t += n / 24
    lst = os.path.join(tmp, 'parts.txt')
    with open(lst, 'w') as fh:
        fh.writelines(f"file '{os.path.abspath(p)}'\n" for p in parts)
    au = ['-i', audio, '-map', '1:a:0', '-c:a', 'pcm_s24le', '-ar', '48000'] if audio else []
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'concat', '-safe', '0', '-i', lst, *au[:2], '-map', '0:v:0',
                    '-c:v', 'copy', *au[2:], '-metadata', f'title={cut_id}', out + '.part.mov'], check=True)
    os.replace(out + '.part.mov', out)
    shutil.rmtree(tmp)  # the parts are the cut again, 16 GB for START at 4K
    print(f'--prores: {copied} segment(s) copied from their ProRes source, {encoded} encoded (cards, black, re-timed, other sources)')
    return parts, t

if __name__ == '__main__':
    ns, a = cli.split_roots(sys.argv[1:])
    if not a or a[0].startswith('-'):
        print(__doc__.strip())
        sys.exit(0 if a and a[0] in ('-h', '--help') else 2)   # the edit list comes first
    R = cli.roots(ns)
    R.say('edl_cut')
    TEXT = R.text
    os.chdir(R.media)          # EDL clip paths (r3/clips/...) and card images are media-relative
    if '--round' in a:
        ROUND = a[a.index('--round') + 1]
    else:
        ROUND = cli.latest_round(R.media) or sys.exit(f'no r<N> folder in {R.media}: pass --round')
    PREFIX = production.load(R.text, R.media).label_prefix
    edl = a[0]
    cut_id = os.path.basename(edl)[:-len('.edl.tsv')]
    edl = edl_path(edl)
    if '--size' in a:
        W, H = map(int, a[a.index('--size') + 1].lower().split('x'))
    if '--cards' in a:
        cf = edl_path(a[a.index('--cards') + 1])
        CARDS = load_cards(cf)
        lang = os.path.basename(cf)[:-len('.tsv')].split('-')[-1]
    if '--clips' in a:
        CLIPS = a[a.index('--clips') + 1].rstrip('/')
        cut_id += '-' + os.path.basename(CLIPS).replace('clips-', '')
        miss = sorted({s for s, *_ in rows(edl) if s.startswith(f'{ROUND}/clips/') and sub(s) == s})
        print(f'--clips {CLIPS}: {len(miss)} source(s) not there, used as they are:', *miss, sep='\n  ')
    if '--cards' in a:
        cut_id += '-' + lang
    if '--rows' in a:
        i0, j0 = (int(x) for x in a[a.index('--rows') + 1].split(':'))
        keep = [l for l in open(edl) if l.strip() and not l.lstrip().startswith('#')][i0 - 1:j0]
        edl = os.path.join(ROUND, 'cut', f'.{cut_id}-rows{i0}-{j0}.edl.tsv')
        os.makedirs(f'{ROUND}/cut', exist_ok=True); open(edl, 'w').writelines(keep)
        cut_id += f'-rows{i0}-{j0}'
    if '--audio-only' in a:
        AUDIO_ONLY = True
        segs, dur = build(edl, os.path.join(ROUND, 'audio', cut_id + '-production.wav'), False, cut_id)
        print(f'{cut_id}: {len(segs)} segments, {dur:.2f} s (sound only)'); sys.exit(0)
    clean = None
    if '--prores' in a:
        clean = os.path.join(ROUND, 'cut', cut_id + '.mov')
        mix = a[a.index('--audio') + 1] if '--audio' in a else None
        segs, dur = build_prores(edl, clean, cut_id, mix)
    elif '--no-clean' not in a:
        clean = os.path.join(ROUND, 'cut', cut_id + '.mp4')
        segs, dur = build(edl, clean, False, cut_id)
    if '--wip' in a:
        segs, dur = build(edl, os.path.join(a[a.index('--wip') + 1], f'{PREFIX}-{cut_id}.mp4'), True, cut_id)
    print(f'{cut_id}: {len(segs)} segments, {dur:.2f} s')
    # a clean full cut is a master (a --rows piece is not: the joined file is)
    if clean and '--rows' not in a:
        if '--no-land' in a:
            print(f'NOT LANDED (--no-land): in the Mac shell, run: bin/tool land {clean}')
        else:
            sys.exit(landing.land(R, [clean], tool='edl_cut'))
