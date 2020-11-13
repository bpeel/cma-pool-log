#!/usr/bin/python3

import re
import sys
import collections
import gi
gi.require_version('Rsvg', '2.0')
from gi.repository import Rsvg
gi.require_version('Pango', '1.0')
from gi.repository import Pango
gi.require_version('PangoCairo', '1.0')
from gi.repository import PangoCairo
import cairo
import subprocess
import math
import json
import os.path


LINE_RE = re.compile(r'^\[ *([0-9]+)\.([0-9]{6}) *] +@@@ +([a-z_]+) '
                     r'([0-9a-f]{8,16})(?: +(.*))?')
POOL_COLOR = 'e9c6afff'
UNMOVEABLE_COLOR = '2b0000ff'
IN_USE_COLOR = '800000ff'
BUFFER_COLOR = 'ffd42aff'
TEST_DRM_MM_EXECUTABLE = 'test-drm-mm/build/test-drm-mm'


Command = collections.namedtuple('Command',
                                 ['timestamp', 'cmd', 'buf_id', 'args'])


class Buffer:
    def __init__(self, size, unmoveable=False):
        self.size = size
        self.unmoveable = unmoveable
        self.offset = None
        self.in_use = False
        self.madv = 'willneed'


class Pool:
    SIZE = 128 * 1024 * 1024

    def __init__(self, name, compact=False):
        self.name = name
        self.buffers = {}
        self.mru_list = []
        self.offset_list = []
        self.compact = compact
        self.overflow = 0

    def paged_in_buffers(self):
        return self.offset_list

    def process_command(self, command):
        Pool.COMMANDS[command.cmd](self, command)

    def _remove_buffer_from_pool(self, buf):
        self.offset_list.remove(buf)
        self.mru_list.remove(buf)
        buf.offset = None
        self.overflow += buf.size

    def _page_out_lru_buffer(self):
        for pos in range(len(self.mru_list) - 1, -1, -1):
            buf = self.mru_list[pos]

            if buf.in_use or buf.unmoveable:
                continue

            self._remove_buffer_from_pool(buf)

            return True

        return False

    def _purge_userspace_cache(self):
        i = 0
        while i < len(self.offset_list):
            buf = self.offset_list[i]

            if buf.madv == 'dontneed' and not buf.in_use and not buf.unmoveable:
                self._remove_buffer_from_pool(buf)
            else:
                i += 1

    def _insert_buffer_at(self, offset, offset_list_pos, buf):
        buf.offset = offset
        self.offset_list.insert(offset_list_pos, buf)
        self.mru_list.insert(0, buf)
        self.overflow -= buf.size

    def _insert_buffer(self, buf):
        last_offset = 0

        for pos, other_buf in enumerate(self.offset_list):
            if other_buf.offset - last_offset >= buf.size:
                self._insert_buffer_at(last_offset, pos, buf)
                return True

            last_offset = other_buf.offset + other_buf.size

        if last_offset + buf.size <= Pool.SIZE:
            self._insert_buffer_at(last_offset, len(self.offset_list), buf)
            return True

        return False

    def _page_in_buffer(self, buf):
        if buf.offset is not None:
            return

        # Check if there is already a slot free for the buffer
        if self._insert_buffer(buf):
            return

        # Get rid of all of the purgable buffers
        self._purge_userspace_cache()

        # Try again in a loop while paging out the least recently used
        # buffer
        while not self._insert_buffer(buf):
            if not self._page_out_lru_buffer():
                raise Exception("Couldn’t make space for buffer of size {}".
                                format(buf.size))

    def _compact_buffer(self, buf):
        prev_offset = 0

        for other_buf in self.offset_list:
            if other_buf.offset >= buf.offset:
                break

            prev_offset = other_buf.offset + other_buf.size

        if prev_offset < buf.offset:
            buf.offset = prev_offset

    def _buf_destroy(self, command):
        assert(command.args is None)

        buf = self.buffers[command.buf_id]

        if buf.offset is None:
            self.overflow -= buf.size
        else:
            self.mru_list.remove(buf)
            self.offset_list.remove(buf)

        del self.buffers[command.buf_id]

    def _buf_add_usecnt(self, command):
        assert(command.args is None)

        buf = self.buffers[command.buf_id]
        assert(not buf.in_use)
        buf.in_use = True

    def _buf_remove_usecnt(self, command):
        assert(command.args is None)

        buf = self.buffers[command.buf_id]
        assert(buf.in_use)
        buf.in_use = False

        if self.compact:
            self._compact_buffer(buf)

    def _buf_create(self, command):
        assert(command.buf_id not in self.buffers)

        buf = Buffer(int(command.args[0]), len(command.args) > 1)
        self.buffers[command.buf_id] = buf
        self.overflow += buf.size

        self._page_in_buffer(buf)

    def _buf_use(self, command):
        assert(command.args is None)

        buf = self.buffers[command.buf_id]

        assert(buf.madv == 'willneed')

        self._page_in_buffer(buf)

        assert(buf.offset is not None)

        self.mru_list.remove(buf)
        self.mru_list.insert(0, buf)

    def _buf_madv(self, command):
        self.buffers[command.buf_id].madv = command.args[0]

    COMMANDS = {
        'destroy' : _buf_destroy,
        'add_usecnt' : _buf_add_usecnt,
        'remove_usecnt' : _buf_remove_usecnt,
        'create' : _buf_create,
        'use' : _buf_use,
        'madv' : _buf_madv,
    }


class ProcPool:
    def __init__(self, name):
        self.name = name
        self.overflow = 0
        self.proc = subprocess.Popen([TEST_DRM_MM_EXECUTABLE],
                                     stdin = subprocess.PIPE,
                                     stdout = subprocess.PIPE,
                                     encoding = 'utf-8')
        self._paged_in_buffers = None

    def _make_buffer(data):
        buf = Buffer(data['size'], data['unmoveable'])
        buf.offset = data['offset']
        buf.in_use = data['in_use']
        buf.madv = data['madv']
        return buf

    def paged_in_buffers(self):
        if self._paged_in_buffers is None:
            print('dump', file=self.proc.stdin)
            self.proc.stdin.flush()
            parts = []
            for line in self.proc.stdout:
                parts.append(line)
                if line.startswith('}'):
                    break
            state = json.loads(''.join(parts))

            self.overflow = state['overflow']
            self._paged_in_buffers = [ProcPool._make_buffer(b)
                                      for b in state['buffers']]

        return self._paged_in_buffers

    def process_command(self, command):
        self._paged_in_buffers = None

        print("[{}] @@@ {} {:08x}{}".format(command.timestamp,
                                            command.cmd,
                                            command.buf_id,
                                            ' ' + ' '.join(command.args)
                                            if command.args
                                            else ''),
              file=self.proc.stdin)


class Video:
    IMAGE_WIDTH = 1280
    IMAGE_HEIGHT = 720
    FRAME_RATE = 30

    ARGS = ['-f', 'rawvideo',
            '-pixel_format', 'bgra',
            '-video_size', '{}x{}'.format(IMAGE_WIDTH,
                                          IMAGE_HEIGHT),
            '-framerate', str(FRAME_RATE),
            '-i', '-',
            '-c:v', 'libvpx',
            '-b:v', '3M',
            '-y',
            '-vf', 'format=yuv420p']

    def __init__(self, filename):
        self.out = subprocess.Popen(['ffmpeg'] + Video.ARGS + [filename],
                                    stdin = subprocess.PIPE)
        self.surface = cairo.ImageSurface(cairo.FORMAT_RGB24,
                                          Video.IMAGE_WIDTH,
                                          Video.IMAGE_HEIGHT)
        self.cr = cairo.Context(self.surface)
        self.n_frames = 0

    def begin_frame(self):
        return self.cr

    def duplicate_frame(self):
        self.out.stdin.write(self.surface.get_data())
        self.n_frames += 1

    def end_frame(self):
        self.surface.flush()
        self.duplicate_frame()

    def finish(self):
        self.out.stdin.close()
        if self.out.wait() != 0:
            raise Exception("ffmpeg failed")

    def timestamp(self):
        return self.n_frames * 1_000_000 // Video.FRAME_RATE


def set_source_color(cr, color):
    parts = [int(x.group(0), 16) / 255 for x in re.finditer(r'..', color)]

    if len(parts) > 3:
        cr.set_source_rgba(*parts)
    else:
        cr.set_source_rgb(*parts)


def draw_legend(cr):
    colors = (('Unmoveable', UNMOVEABLE_COLOR),
              ('In-use', IN_USE_COLOR),
              ('Buffer', BUFFER_COLOR))
    cr.save()
    cr.set_font_size(Video.IMAGE_WIDTH / 70)
    widths = [cr.text_extents(color[0]).width for color in colors]
    gap = cr.text_extents("w").width
    box_width = gap * 3
    box_height = gap
    total_width = ((len(widths) + 1) * gap +
                   len(widths) * (box_width + gap / 2) +
                   sum(widths))
    total_height = box_height + gap * 2
    line_width = Video.IMAGE_HEIGHT / 200
    x = Video.IMAGE_WIDTH - total_width - line_width / 2
    y = Video.IMAGE_HEIGHT - total_height - line_width / 2

    cr.rectangle(x, y, total_width, total_height)
    cr.set_line_width(line_width)
    cr.set_source_rgb(0, 0, 0)
    cr.stroke()

    for label, color in colors:
        x += gap
        set_source_color(cr, color)
        cr.rectangle(x, y + gap, box_width, box_height)
        cr.fill()
        x += box_width + gap / 2
        extents = cr.text_extents(label)
        cr.move_to(x, y + total_height / 2 + extents.height / 2)
        cr.show_text(label)
        x += extents.width

    cr.restore()


def draw_label(cr, pool_x, pool_y, pool_height, label):
    extents = cr.text_extents(label)

    cr.save()
    cr.move_to(pool_x - extents.height,
               pool_y + pool_height // 2 + extents.width // 2)
    cr.rotate(-math.pi / 2.0)
    set_source_color(cr, POOL_COLOR)
    cr.show_text(label)
    cr.restore()


def draw_pool(cr, pool, width, height):
    side_border = width // 30
    pool_width = width - side_border * 2
    pool_height = (height - side_border * 3) // 2
    pool_x = side_border
    pool_y = side_border

    cr.set_source_rgb(0, 0, 0)
    cr.set_font_size(side_border / 2)
    cr.move_to(side_border, side_border * 5 / 8)
    cr.show_text(pool.name)

    cr.rectangle(pool_x, pool_y, pool_width, pool_height)
    set_source_color(cr, POOL_COLOR)
    cr.fill()

    draw_label(cr, pool_x, pool_y, pool_height, "Pool")

    for buf in pool.paged_in_buffers():
        if buf.unmoveable:
            set_source_color(cr, UNMOVEABLE_COLOR)
        elif buf.in_use:
            set_source_color(cr, IN_USE_COLOR)
        else:
            set_source_color(cr, BUFFER_COLOR)

        cr.rectangle(pool_x + buf.offset * pool_width / Pool.SIZE,
                     pool_y + pool_height / 10,
                     buf.size * pool_width / Pool.SIZE,
                     pool_height * 8 / 10)
        cr.fill()

    pool_y += side_border + pool_height

    draw_label(cr, pool_x, pool_y, pool_height, "Overflow")

    pattern = cairo.LinearGradient(pool_x, pool_y, pool_x + pool_width, pool_y)
    pattern.add_color_stop_rgba(0.0, 1.0, 0.666, 0.666, 1.0)
    pattern.add_color_stop_rgba(1.0, 1.0, 0.0, 0.0, 1.0)

    cr.rectangle(pool_x, pool_y,
                 pool.overflow * pool_width / Pool.SIZE,
                 pool_height)
    cr.set_source(pattern)
    cr.fill()


def draw_frame(video, pools):
    n_pools = len(pools)

    cr = video.begin_frame()

    set_source_color(cr, '429bdb')
    cr.paint()

    for i in range(n_pools - 1):
        cr.save()
        cr.set_source_rgb(0, 0, 0)
        cr.set_line_width(Video.IMAGE_HEIGHT / 200)
        cr.move_to(0, (i + 1) * Video.IMAGE_HEIGHT / n_pools)
        cr.rel_line_to(Video.IMAGE_WIDTH, 0)
        cr.stroke()
        cr.restore()

    draw_legend(cr)

    for i, pool in enumerate(pools):
        cr.save()
        cr.translate(0, Video.IMAGE_HEIGHT * i // n_pools)

        draw_pool(cr,
                  pool,
                  Video.IMAGE_WIDTH, Video.IMAGE_HEIGHT // n_pools)

        cr.restore()

    video.end_frame()


def ensure_test_drm_mm():
    if os.path.exists(TEST_DRM_MM_EXECUTABLE):
        return

    subprocess.run(['bash', '-c',
                    'cd test-drm-mm && meson build && ninja -C build'],
                   check=True)


def main():
    ensure_test_drm_mm()

    pools = [Pool("No compaction", compact=False),
             Pool("With compaction", compact=True),
             ProcPool("drm_mm")]
    video = Video('cma-pool-log.webm')
    first_timestamp = None

    for line in sys.stdin:
        md = LINE_RE.match(line)
        if md is None:
            continue

        timestamp = int(md.group(1)) * 1_000_000 + int(md.group(2))
        cmd = md.group(3)
        buf_id = int(md.group(4), 16)
        args = md.group(5)

        if args is not None:
            args = args.split(' ')

        if first_timestamp is None:
            first_timestamp = timestamp - 1_000_000

        timestamp -= first_timestamp

        if video.timestamp() < timestamp:
            draw_frame(video, pools)

            while video.timestamp() < timestamp:
                video.duplicate_frame()

        command = Command(timestamp, cmd, buf_id, args)
        for pool in pools:
            pool.process_command(command)

    draw_frame(video, pools)
    for _ in range(Video.FRAME_RATE - 1):
        video.duplicate_frame()

    video.finish()


if __name__ == '__main__':
    main()
