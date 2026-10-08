"""Reader for 'newc' cpio archives (as embedded in a Linux kernel Image's initramfs)."""


def parse(buf, start):
    """Yield (header_offset, header dict, name, data_offset, data_size) up to TRAILER!!!."""
    o = start
    while True:
        if buf[o:o + 6] != b'070701':
            raise ValueError('bad cpio magic at %#x' % o)
        f = [int(buf[o + 6 + 8 * i:o + 14 + 8 * i], 16) for i in range(13)]
        h = dict(zip(['ino', 'mode', 'uid', 'gid', 'nlink', 'mtime', 'filesize', 'devmajor', 'devminor',
                      'rdevmajor', 'rdevminor', 'namesize', 'check'], f))
        name = buf[o + 110:o + 110 + h['namesize'] - 1].decode('utf-8', 'replace')
        d = (o + 110 + h['namesize'] + 3) & ~3
        yield o, h, name, d, h['filesize']
        if name == 'TRAILER!!!':
            return
        o = (d + h['filesize'] + 3) & ~3


def find_archive(buf, must_contain):
    """Return the offset of the newc archive in buf that contains the file `must_contain`."""
    pos = buf.find(b'070701')
    while pos >= 0:
        try:
            names = {name for _, _, name, _, _ in parse(buf, pos)}
            if must_contain in names:
                return pos
        except (ValueError, IndexError, UnicodeDecodeError):
            pass
        pos = buf.find(b'070701', pos + 1)
    raise ValueError('no cpio archive containing %s found' % must_contain)
