# Third-party components

| Component | Where it comes from | License | In this repo? |
| --- | --- | --- | --- |
| BL2 boot loaders (`firmware/bl2/*`) | Trusted Firmware-A, MediaTek fork, with the [sam-413 U6+ workaround](https://github.com/sam-413/Bricked-U6Plus-eMMC-Workaround) and a 1-bit eMMC change | BSD-3-Clause | Yes (binaries; see `firmware/bl2/README.md`) |
| `mtk_uartboot` | [github.com/981213/mtk_uartboot](https://github.com/981213/mtk_uartboot), release v0.1.1 | AGPL-3.0 | No: downloaded by `prepare.py` from the official release |
| OpenWrt 25.12.5 U6+ initramfs | [downloads.openwrt.org](https://downloads.openwrt.org/releases/25.12.5/targets/mediatek/filogic/) | GPL-2.0 and others; sources at [git.openwrt.org](https://git.openwrt.org/) | No: downloaded by `prepare.py`, patched locally |
| UniFi 6.7.54 firmware for U6+ | [Ubiquiti download page](https://www.ui.com/download/software/u6-plus) | Proprietary (Ubiquiti); its Linux kernel is GPL-2.0 | No: downloaded by `prepare.py`, patched locally, never redistributed |

The images that `prepare.py` builds contain Ubiquiti and OpenWrt code. They are built on your own PC for
recovering your own hardware. Do not redistribute the `images/` folder.

## BSD-3-Clause (Trusted Firmware-A)

```
Copyright (c) 2013-2026, Arm Limited and Contributors. All rights reserved.
Copyright (c) MediaTek Inc. All rights reserved.

Redistribution and use in source and binary forms, with or without modification,
are permitted provided that the following conditions are met:

- Redistributions of source code must retain the above copyright notice, this
  list of conditions and the following disclaimer.

- Redistributions in binary form must reproduce the above copyright notice, this
  list of conditions and the following disclaimer in the documentation and/or
  other materials provided with the distribution.

- Neither the name of Arm nor the names of its contributors may be used to
  endorse or promote products derived from this software without specific prior
  written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR
ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

## Trademarks

UniFi and Ubiquiti are trademarks of Ubiquiti Inc. This project is not affiliated with, endorsed by or
supported by Ubiquiti. MediaTek is a trademark of MediaTek Inc. OpenWrt is a registered trademark of the
Software Freedom Conservancy.
