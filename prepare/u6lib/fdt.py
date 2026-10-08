"""Minimal flattened-device-tree (DTB / FIT image) parser and serializer. Standard library only."""
import struct

FDT_MAGIC = 0xD00DFEED
BEGIN_NODE, END_NODE, PROP, NOP, END = 1, 2, 3, 4, 9


class Node:
    def __init__(self, name):
        self.name = name
        self.props = []      # list of [name, bytes]
        self.children = []

    def child(self, name):
        for c in self.children:
            if c.name == name:
                return c
        return None

    def prop(self, name):
        for k, v in self.props:
            if k == name:
                return v
        return None

    def set(self, name, value):
        for p in self.props:
            if p[0] == name:
                p[1] = value
                return
        self.props.append([name, value])

    def remove(self, *names):
        self.props = [p for p in self.props if p[0] not in names]


def parse(blob):
    """Parse a DTB/FIT. Returns (root Node, header dict)."""
    (magic, totalsize, off_struct, off_strings, _off_rsvmap, version, _last_comp,
     boot_cpuid, size_strings, _size_struct) = struct.unpack_from('>10I', blob, 0)
    if magic != FDT_MAGIC:
        raise ValueError('not a device tree (magic %08x)' % magic)
    strings = blob[off_strings:off_strings + size_strings]

    def getstr(off):
        return strings[off:strings.index(b'\0', off)].decode()

    pos = off_struct
    stack, root = [], None
    while True:
        tag, = struct.unpack_from('>I', blob, pos)
        pos += 4
        if tag == BEGIN_NODE:
            end = blob.index(b'\0', pos)
            node = Node(blob[pos:end].decode())
            pos = (end + 1 + 3) & ~3
            if stack:
                stack[-1].children.append(node)
            else:
                root = node
            stack.append(node)
        elif tag == END_NODE:
            stack.pop()
        elif tag == PROP:
            ln, nameoff = struct.unpack_from('>II', blob, pos)
            pos += 8
            stack[-1].props.append([getstr(nameoff), bytes(blob[pos:pos + ln])])
            pos = (pos + ln + 3) & ~3
        elif tag == NOP:
            pass
        elif tag == END:
            break
        else:
            raise ValueError('bad FDT tag %d at %#x' % (tag, pos - 4))
    return root, {'totalsize': totalsize, 'version': version, 'boot_cpuid': boot_cpuid}


def serialize(root, boot_cpuid=0):
    """Serialize a Node tree to a version-17 DTB."""
    strtab, stroff = bytearray(), {}

    def soff(name):
        if name not in stroff:
            stroff[name] = len(strtab)
            strtab.extend(name.encode() + b'\0')
        return stroff[name]

    st = bytearray()

    def pad():
        while len(st) % 4:
            st.append(0)

    def emit(n):
        st.extend(struct.pack('>I', BEGIN_NODE))
        st.extend(n.name.encode() + b'\0')
        pad()
        for k, v in n.props:
            st.extend(struct.pack('>III', PROP, len(v), soff(k)))
            st.extend(v)
            pad()
        for c in n.children:
            emit(c)
        st.extend(struct.pack('>I', END_NODE))

    emit(root)
    st.extend(struct.pack('>I', END))
    rsvmap = b'\0' * 16
    off_struct = 40 + len(rsvmap)
    off_strings = off_struct + len(st)
    total = off_strings + len(strtab)
    hdr = struct.pack('>10I', FDT_MAGIC, total, off_struct, off_strings, 40, 17, 16, boot_cpuid, len(strtab), len(st))
    return hdr + rsvmap + bytes(st) + bytes(strtab)


def find(root, path):
    node = root
    for part in [p for p in path.split('/') if p]:
        node = node.child(part)
        if node is None:
            raise KeyError(path)
    return node
