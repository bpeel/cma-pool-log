#!/usr/bin/python3

import re
import sys
import collections


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


def main():
    pool = Pool()

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

        pool.process_command(Command(timestamp, cmd, buf_id, args))


if __name__ == '__main__':
    main()
