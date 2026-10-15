"""Review copies: a copy meant for review is titled by its own file name.

A media server lists a file by its embedded title tag, not its file name, so a copy titled by a
bare take or cut ID hides among every other copy of the same ID. Every tool that writes a review
copy sets the title through here.
"""
import os
import subprocess


def title(path):
    """The container title of a review copy: its own file name without the extension."""
    return os.path.splitext(os.path.basename(path))[0]


def copy(src, dst):
    """Copy src to dst, streams untouched, titled by dst's file name; via .part, so a killed copy
    never sits under the real name."""
    part = dst + '.part.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', src, '-map', '0', '-c', 'copy',
                    '-metadata', f'title={title(dst)}', '-movflags', '+faststart', part], check=True)
    os.replace(part, dst)
