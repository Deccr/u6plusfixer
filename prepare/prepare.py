#!/usr/bin/env python3
"""
prepare.py - download the official firmware and build the U6+ recovery images.

The repository cannot redistribute Ubiquiti firmware, so every image that contains it is built here,
on your own PC, from the official files downloaded straight from Ubiquiti and OpenWrt. Each download is
checked against a pinned SHA-256 before it is used.

Builds (into ../images):
  unifi-6.7.54-kernel0-original.itb       UniFi 6.7.54 kernel, untouched (written to eMMC kernel0)
  u6plus-stock-fip-6.7.54.bin             UniFi's BL31 + U-Boot, extracted from the firmware
  unifi-6.7.54-1bit-noupdate.itb          UniFi kernel: eMMC 1-bit @ 25 MHz, firmware updates blocked
  unifi-6.7.54-1bit-5mhz-noupdate.itb     same, eMMC 1-bit @ 5 MHz (for weaker units)
  openwrt-ram-1bit.itb                    OpenWrt RAM recovery system: eMMC 1-bit @ 25 MHz, full SPI NOR (read-only)
  openwrt-ram-1bit-5mhz.itb               same @ 5 MHz
  bl2-*.bin / bl2-*.img                   1-bit eMMC boot loaders (copied from ../firmware/bl2)
and ../bin/mtk_uartboot.exe, then ../SHA256SUMS.txt for the recovery tool's integrity check.

Usage:  python prepare.py              (downloads ~25 MB the first time; cached in ../downloads)
        python prepare.py --offline    (use only files already in the cache)
Standard library only. Python 3.8+.
"""
import argparse
import hashlib
import lzma
import os
import shutil
import struct
import sys
import urllib.request
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from u6lib import cpio, fdt  # noqa: E402

DOWNLOADS = {
    'unifi': {
        'url': 'https://dl.ui.com/unifi/firmware/UAPL6/6.7.54.15663/BZ.MT7981_6.7.54+15663.260513.1738.bin',
        'file': 'BZ.MT7981_6.7.54+15663.260513.1738.bin',
        'sha256': '7211a694fa8c23998a551b99dc073e729b3067d94295de6728f7019178b7d560',
    },
    'openwrt': {
        'url': 'https://downloads.openwrt.org/releases/25.12.5/targets/mediatek/filogic/'
               'openwrt-25.12.5-mediatek-filogic-ubnt_unifi-6-plus-initramfs-kernel.bin',
        'file': 'openwrt-25.12.5-mediatek-filogic-ubnt_unifi-6-plus-initramfs-kernel.bin',
        'sha256': '6b9b4e1386e62f06dbab71c294c3013e362b1185805071e2e0bfffb66bc886e6',
    },
    'mtk_uartboot': {
        'url': 'https://github.com/981213/mtk_uartboot/releases/download/v0.1.1/mtk_uartboot-v0.1.1-x86_64-pc-windows-msvc.zip',
        'file': 'mtk_uartboot-v0.1.1-x86_64-pc-windows-msvc.zip',
        'sha256': None,   # GitHub publishes no checksum; the exe inside is verified instead (MTK_UARTBOOT_EXE_SHA256),
    },
}
MTK_UARTBOOT_EXE_SHA256 = 'e9d6b2b3ffef420170589931c6232139f70dfdc11706521f08333e889ad9cb69'

# Builds known to work on real units. Other Python/liblzma versions may compress the patched
# kernel slightly differently; such builds are verified structurally instead.
REFERENCE = {
    'u6plus-stock-fip-6.7.54.bin': '3f71d2467acfd7ec543dae2085bac07c8f75f32dd2f1745c4f8847d41042780d',
    'unifi-6.7.54-kernel0-original.itb': 'b95237b1c479bafd1be7b3233d3e6cc15160a83a502b21f4db88237c9ca730d7',
    'unifi-6.7.54-1bit-noupdate.itb': '88fbd743c211d5d805ec69bbe6621d5c09cefd1b2ded6c649b3ee80d305beb5a',
    'unifi-6.7.54-1bit-5mhz-noupdate.itb': 'c749fee4c84a4830282e6fd17332bb81ee8b6f836e616380428c8e5fb4e5c2d6',
    'openwrt-ram-1bit.itb': '2ab00e75e62a91d1e61e9520aaa6ec25d1c4d610ae307ced783f6b9290dcd8e9',
    'openwrt-ram-1bit-5mhz.itb': '1ebbeb4ad1bd3d251b442bc5a5b6aee6cd35abe859dfd49234e06ffe5fab4523',
}

UNIFI_MMC = '/mmc@11230000'
OPENWRT_MMC = '/soc/mmc@11230000'
OPENWRT_NOR_PARTS = '/soc/spi@11009000/flash@0/partitions'
HS_PROPS = ('cap-mmc-highspeed', 'cap-mmc-hw-reset', 'mmc-hs200-1_8v', 'mmc-hs400-1_8v', 'mmc-ddr-1_8v')

UPDATE_BLOCKER = (b'#!/bin/sh\n'
                  b'# Firmware updates are disabled on this unit.\n'
                  b'# Its worn eMMC only works in 1-bit mode via a custom BL2 + a kernel in SPI NOR;\n'
                  b'# a stock update would rewrite the BL2 and brick it. Replaces fwupdate and fwupdate.real.\n'
                  b'logger -t fwupdate -s "Firmware update refused: updates are disabled on this unit ($0 $*)"\n'
                  b'exit 1\n')


def say(msg):
    print(msg, flush=True)


def sha256(data_or_path):
    h = hashlib.sha256()
    if isinstance(data_or_path, (bytes, bytearray)):
        h.update(data_or_path)
    else:
        with open(data_or_path, 'rb') as f:
            for chunk in iter(lambda: f.read(1 << 20), b''):
                h.update(chunk)
    return h.hexdigest()


def fetch(key, cache, offline):
    d = DOWNLOADS[key]
    path = os.path.join(cache, d['file'])
    if key == 'mtk_uartboot':
        # A bare, already-verified mtk_uartboot.exe in the cache is accepted too.
        exe = os.path.join(cache, 'mtk_uartboot.exe')
        if os.path.exists(exe) and sha256(exe) == MTK_UARTBOOT_EXE_SHA256:
            say('  [cached] mtk_uartboot.exe')
            return exe
    if os.path.exists(path) and (d['sha256'] is None or sha256(path) == d['sha256']):
        say('  [cached] %s' % d['file'])
        return path
    if offline:
        raise SystemExit('missing or wrong file in cache: %s (run without --offline)' % path)
    say('  downloading %s' % d['url'])
    tmp = path + '.part'
    req = urllib.request.Request(d['url'], headers={'User-Agent': 'u6plus-emmc-recovery/1.0'})
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, 'wb') as f:
        shutil.copyfileobj(r, f, 1 << 20)
    got = sha256(tmp)
    if d['sha256'] is not None and got != d['sha256']:
        os.remove(tmp)
        raise SystemExit('checksum mismatch for %s\n  expected %s\n  got      %s' % (d['file'], d['sha256'], got))
    os.replace(tmp, path)
    say('    ok, sha256 %s...' % got[:16])
    return path


# ------------------------------------------------------------------ UniFi firmware container
def unifi_sections(fw):
    """Return {name: bytes} for the 'EMMC' sections of a UniFi MT7981 firmware file."""
    out = {}
    pos = fw.find(b'EMMC')
    while pos >= 0:
        name = fw[pos + 4:pos + 36].split(b'\0')[0]
        if name in (b'gpt', b'bl2', b'u-boot', b'kernel0'):
            size = struct.unpack_from('>I', fw, pos + 48)[0]
            out[name.decode()] = fw[pos + 56:pos + 56 + size]
        pos = fw.find(b'EMMC', pos + 4)
    for need in ('u-boot', 'kernel0'):
        if need not in out:
            raise SystemExit('firmware file has no %s section' % need)
    return out


# ------------------------------------------------------------------ FIT helpers
def fit_update_hashes(img_node, data):
    for h in img_node.children:
        if not h.name.startswith('hash'):
            continue
        algo = h.prop('algo').rstrip(b'\0').decode()
        if algo == 'crc32':
            h.set('value', struct.pack('>I', zlib.crc32(data) & 0xffffffff))
        else:
            h.set('value', hashlib.new(algo, data).digest())


def fit_verify(blob):
    fit, _ = fdt.parse(blob)
    for im in fdt.find(fit, '/images').children:
        data = im.prop('data')
        for h in im.children:
            if not h.name.startswith('hash'):
                continue
            algo = h.prop('algo').rstrip(b'\0').decode()
            got = struct.pack('>I', zlib.crc32(data) & 0xffffffff) if algo == 'crc32' else hashlib.new(algo, data).digest()
            if got != h.prop('value'):
                raise SystemExit('internal error: %s hash mismatch in built image' % im.name)


def patch_mmc(dtb_bytes, mmc_path, freq, nor_rest=False):
    """1 data line, max-frequency `freq`, no high-speed modes; optionally expose all SPI NOR read-only."""
    dtb, hdr = fdt.parse(dtb_bytes)
    mmc = fdt.find(dtb, mmc_path)
    mmc.set('bus-width', struct.pack('>I', 1))
    mmc.set('max-frequency', struct.pack('>I', freq))
    mmc.remove(*HS_PROPS)
    if nor_rest:
        n = fdt.Node('partition@90000')
        n.props = [['label', b'nor-rest\0'], ['reg', struct.pack('>II', 0x90000, 0xF70000)], ['read-only', b'']]
        fdt.find(dtb, OPENWRT_NOR_PARTS).children.append(n)
    return fdt.serialize(dtb, hdr['boot_cpuid'])


def fit_patch_fdt(fit_bytes, fdt_image, mmc_path, freq, nor_rest=False):
    fit, hdr = fdt.parse(fit_bytes)
    img = fdt.find(fit, '/images/' + fdt_image)
    new = patch_mmc(img.prop('data'), mmc_path, freq, nor_rest)
    img.set('data', new)
    fit_update_hashes(img, new)
    return fdt.serialize(fit, hdr['boot_cpuid'])


def fit_replace_kernel(fit_bytes, new_data):
    fit, hdr = fdt.parse(fit_bytes)
    k = fdt.find(fit, '/images/kernel-1')
    k.set('data', new_data)
    fit_update_hashes(k, new_data)
    return fdt.serialize(fit, hdr['boot_cpuid'])


# ------------------------------------------------------------------ update blocker
def block_updates(image):
    """In-place, same-size edits to the kernel's built-in initramfs so UniFi refuses every firmware update."""
    img = bytearray(image)
    start = cpio.find_archive(img, 'sbin/fwupdate')
    entries = {name: (d, sz) for _, _, name, d, sz in cpio.parse(img, start)}
    d, sz = entries['sbin/fwupdate']
    if bytes(img[d:d + 9]) != b'#!/bin/sh' or len(UPDATE_BLOCKER) >= sz - 2:
        raise SystemExit('unexpected sbin/fwupdate in this firmware')
    img[d:d + sz] = UPDATE_BLOCKER + b'#' * (sz - len(UPDATE_BLOCKER) - 1) + b'\n'
    d, sz = entries['sbin/fwupdate.real']
    if bytes(img[d:d + sz]) != b'ubntbox\x00':
        raise SystemExit('unexpected sbin/fwupdate.real symlink in this firmware')
    img[d:d + 8] = b'fwupdate'   # symlink -> the refusing script (same 8 bytes)
    d, sz = entries['sbin/ubntbox']
    blob = bytes(img[d:d + sz])
    n = blob.count(b'mmcblk0boot0')
    img[d:d + sz] = blob.replace(b'mmcblk0boot0', b'mmcblk0bootX')   # backstop: no device to write BL2 to
    check = {name: (d2, sz2) for _, _, name, d2, sz2 in cpio.parse(img, start)}
    assert len(check) == len(entries)
    say('    update blocker installed (fwupdate refuses; %d boot0 references disabled)' % n)
    return bytes(img)


def lzma_alone(data):
    """LZMA 'alone' stream with UniFi's original properties (lc=1 lp=2 pb=2, 8 MiB dictionary)."""
    filt = [{'id': lzma.FILTER_LZMA1, 'lc': 1, 'lp': 2, 'pb': 2, 'dict_size': 8 << 20, 'preset': 9 | lzma.PRESET_EXTREME}]
    comp = bytearray(lzma.compress(data, format=lzma.FORMAT_ALONE, filters=filt))
    if lzma.decompress(bytes(comp), format=lzma.FORMAT_ALONE) != data:
        raise SystemExit('internal error: LZMA round trip failed')
    # Store the real size like the original (U-Boot's decoder accepts size + end marker)
    struct.pack_into('<Q', comp, 5, len(data))
    return bytes(comp)


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--cache', default=os.path.join(ROOT, 'downloads'))
    ap.add_argument('--offline', action='store_true')
    args = ap.parse_args()
    if sys.version_info < (3, 8):
        raise SystemExit('Python 3.8 or newer is required')
    os.makedirs(args.cache, exist_ok=True)
    out_img = os.path.join(ROOT, 'images')
    out_bin = os.path.join(ROOT, 'bin')
    os.makedirs(out_img, exist_ok=True)
    os.makedirs(out_bin, exist_ok=True)

    say('1. Official downloads (checked against pinned SHA-256)')
    unifi_path = fetch('unifi', args.cache, args.offline)
    owrt_path = fetch('openwrt', args.cache, args.offline)
    mtk_path = fetch('mtk_uartboot', args.cache, args.offline)

    say('2. Extracting UniFi 6.7.54')
    with open(unifi_path, 'rb') as f:
        sec = unifi_sections(f.read())
    built = {}
    built['u6plus-stock-fip-6.7.54.bin'] = sec['u-boot']
    kernel0 = sec['kernel0']
    built['unifi-6.7.54-kernel0-original.itb'] = kernel0
    fit_verify(kernel0)

    say('3. Building the patched UniFi kernels (this takes a minute: LZMA)')
    fit, _ = fdt.parse(kernel0)
    image = lzma.decompress(fdt.find(fit, '/images/kernel-1').prop('data'), format=lzma.FORMAT_ALONE)
    blocked = lzma_alone(block_updates(image))
    for freq, name in ((25000000, 'unifi-6.7.54-1bit-noupdate.itb'), (5000000, 'unifi-6.7.54-1bit-5mhz-noupdate.itb')):
        patched = fit_patch_fdt(kernel0, 'fdt-u6-plus', UNIFI_MMC, freq)
        built[name] = fit_replace_kernel(patched, blocked)
        say('    %s (eMMC 1-bit @ %g MHz)' % (name, freq / 1e6))

    say('4. Building the OpenWrt RAM recovery systems')
    with open(owrt_path, 'rb') as f:
        owrt = f.read()
    for freq, name in ((25000000, 'openwrt-ram-1bit.itb'), (5000000, 'openwrt-ram-1bit-5mhz.itb')):
        built[name] = fit_patch_fdt(owrt, 'fdt-1', OPENWRT_MMC, freq, nor_rest=True)
        say('    %s (eMMC 1-bit @ %g MHz)' % (name, freq / 1e6))

    say('5. Writing images')
    for name, data in built.items():
        if name.endswith('.itb'):
            fit_verify(data)
        with open(os.path.join(out_img, name), 'wb') as f:
            f.write(data)
        ref = REFERENCE.get(name, '')
        tag = 'matches tested build' if sha256(data) == ref else 'structurally verified'
        say('    %-40s %10d bytes  %s' % (name, len(data), tag))
    bl2_dir = os.path.join(ROOT, 'firmware', 'bl2')
    for name in sorted(os.listdir(bl2_dir)):
        if name.startswith('bl2-'):
            shutil.copyfile(os.path.join(bl2_dir, name), os.path.join(out_img, name))
    install_mtk_uartboot(mtk_path, out_bin)

    say('6. Writing SHA256SUMS.txt')
    lines = []
    for sub in ('bin', 'images'):
        for name in sorted(os.listdir(os.path.join(ROOT, sub))):
            lines.append('%s  %s/%s' % (sha256(os.path.join(ROOT, sub, name)), sub, name))
    with open(os.path.join(ROOT, 'SHA256SUMS.txt'), 'w', newline='\n') as f:
        f.write('\n'.join(lines) + '\n')
    say('\nDone. Start the recovery tool with U6Plus-Fixer.bat')


def install_mtk_uartboot(src, out_bin):
    dst = os.path.join(out_bin, 'mtk_uartboot.exe')
    if src.lower().endswith('.zip'):
        import zipfile
        with zipfile.ZipFile(src) as z:
            member = next(n for n in z.namelist() if n.lower().endswith('mtk_uartboot.exe'))
            with z.open(member) as s, open(dst, 'wb') as d:
                shutil.copyfileobj(s, d)
    else:
        shutil.copyfile(src, dst)
    got = sha256(dst)
    if got != MTK_UARTBOOT_EXE_SHA256:
        raise SystemExit('mtk_uartboot.exe checksum mismatch: %s' % got)
    say('    bin/mtk_uartboot.exe (v0.1.1, verified)')


if __name__ == '__main__':
    main()
