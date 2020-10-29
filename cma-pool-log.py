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


LINE_RE = re.compile(r'^\[ *([0-9]+)\.([0-9]{6}) *] +@@@ +([a-z_]+) '
                     r'([0-9a-f]{8,16})(?: +(.*))?')


Command = collections.namedtuple('Command',
                                 ['timestamp', 'cmd', 'buf_id', 'args'])


class Buffer:
    def __init__(self, size, unmoveable=False):
        self.size = size
        self.unmoveable = unmoveable
        self.offset = None
        self.in_use = False


class Pool:
    SIZE = 128 * 1024 * 1024

    def __init__(self, compact=False):
        self.buffers = {}
        self.mru_list = []
        self.offset_list = []
        self.compact = compact

    def process_command(self, command):

        Pool.COMMANDS[command.cmd](self, command)

    def _page_out_lru_buffer(self):
        for pos in range(len(self.mru_list) - 1, -1, -1):
            buf = self.mru_list[pos]

            if buf.in_use or buf.unmoveable:
                continue

            self.offset_list.remove(buf)
            del self.mru_list[pos]
            buf.offset = None

            return True

        return False

    def _insert_buffer(self, buf):
        last_offset = 0

        for pos, other_buf in enumerate(self.offset_list):
            if other_buf.offset - last_offset >= buf.size:
                buf.offset = last_offset
                self.offset_list.insert(pos, buf)
                self.mru_list.insert(0, buf)
                return True

            last_offset = other_buf.offset + buf.size

        if last_offset + buf.size <= Pool.SIZE:
            buf.offset = last_offset
            self.offset_list.append(buf)
            self.mru_list.insert(0, buf)
            return True

        return False

    def _page_in_buffer(self, buf):
        if buf.offset is not None:
            return

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

        if buf.offset is not None:
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

        self._page_in_buffer(buf)

    def _buf_use(self, command):
        assert(command.args is None)

        buf = self.buffers[command.buf_id]
        self._page_in_buffer(buf)

        assert(buf.offset is not None)

        self.mru_list.remove(buf)
        self.mru_list.insert(0, buf)

    COMMANDS = {
        'destroy' : _buf_destroy,
        'add_usecnt' : _buf_add_usecnt,
        'remove_usecnt' : _buf_remove_usecnt,
        'create' : _buf_create,
        'use' : _buf_use,
    }


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


def draw_pool(cr, pool, width, height):
    side_border = width // 30
    pool_width = width - side_border * 2
    pool_height = (height - side_border * 3) // 2
    pool_x = side_border
    pool_y = side_border

    cr.rectangle(pool_x, pool_y, pool_width, pool_height)
    set_source_color(cr, 'e9c6afff')
    cr.fill()

    for buf in pool.offset_list:
        if buf.unmoveable:
            set_source_color(cr, '2b0000ff')
        elif buf.in_use:
            set_source_color(cr, '800000ff')
        else:
            set_source_color(cr, 'ffd42aff')

        cr.rectangle(pool_x + buf.offset * pool_width / Pool.SIZE,
                     pool_y + pool_height / 10,
                     buf.size * pool_width / Pool.SIZE,
                     pool_height * 8 / 10)
        cr.fill()


def draw_frame(cr, pool_non_compact, pool_compact):
    set_source_color(cr, '429bdb')
    cr.paint()

    draw_pool(cr, pool_non_compact, Video.IMAGE_WIDTH, Video.IMAGE_HEIGHT // 2)
    cr.save()
    cr.translate(0, Video.IMAGE_HEIGHT // 2)
    draw_pool(cr, pool_compact, Video.IMAGE_WIDTH, Video.IMAGE_HEIGHT // 2)
    cr.restore()


def main():
    pool_non_compact = Pool(compact=False)
    pool_compact = Pool(compact=True)
    video = Video('cma-pool-log.webm')

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

        if video.timestamp() < timestamp:
            cr = video.begin_frame()
            draw_frame(cr, pool_non_compact, pool_compact)
            video.end_frame()

            while video.timestamp() < timestamp:
                video.duplicate_frame()

        command = Command(timestamp, cmd, buf_id, args)
        pool_non_compact.process_command(command)
        pool_compact.process_command(command)

    video.finish()


if __name__ == '__main__':
    main()
