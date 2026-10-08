# BL2 boot loaders

Trusted Firmware-A BL2 builds for the MT7981 (U6+), BSD-3-Clause. They replace Ubiquiti's BL2 in the
eMMC boot area so the boot loader reads the eMMC on a single data line.

| File | Use | eMMC bus | eMMC clock |
| --- | --- | --- | --- |
| `bl2-emmc-x1-400k.bin` | Loaded over UART by the tool | 1-bit | 400 kHz |
| `bl2-emmc-x1-400k.img` | Written to eMMC boot0 by the tool (MediaTek `EMMC_BOOT` / BRLYT / GFH container) | 1-bit | 400 kHz |
| `bl2-emmc-x1.bin`, `.img` | Earlier variant (unit 1). Some chips miss BL2's 100 ms busy timeout at this speed, so the tool no longer uses it | 1-bit | 25 MHz |
| `bl2-ram-uartdl.bin` | RAM boot loader: receives U-Boot over UART, for units whose eMMC copy of U-Boot is unreadable | n/a | n/a |

## Source

- Base: [sam-413/Bricked-U6Plus-eMMC-Workaround](https://github.com/sam-413/Bricked-U6Plus-eMMC-Workaround) at commit `6cc1631`
  (fork of `mtk-openwrt/arm-trusted-firmware`, branch `mtksoc`). Its changes: eMMC pin drive strength
  raised from code 0x1 to 0x2, the clock pin pulled up instead of down, and the FIP looked up in the GPT
  partition named `u-boot`.
- Build settings (from `BUILD-INFO.txt`): `PLAT=mt7981`, `BOOT_DEVICE=emmc`, `BOARD_BGA=1`, DDR3 at the
  default speed. `bl2-ram-uartdl.bin` is the RAM / UART-download variant (`BOOT_DEVICE=ram`,
  `RAM_BOOT_UART_DL=1`).
- 1-bit change, all `bl2-emmc-x1*` files: in `plat/mediatek/mt7981/bl2/bl2_dev_mmc.c`, the eMMC entry's
  `.bus_width = MMC_BUS_WIDTH_8` becomes `.bus_width = MMC_BUS_WIDTH_1`.
- 400 kHz change, `bl2-emmc-x1-400k*` only: in `drivers/mmc/mtk-sd.c`, the bus clock is left at the
  400 kHz identification clock (`DEFAULT_CLK_FREQ 400000`) instead of being raised for data transfer.

`BUILD-INFO.txt` is a short build summary.

## Checksums (SHA-256)

```
7ab250bd6a348f4d49a20373602efa29d58e4106ff7d7f0e273330efd18294fb  bl2-emmc-x1-400k.img
70d866866163944c1dae37cce21ffeeaa7fb4804c00ee18a84a90c4fb40d2191  bl2-emmc-x1-400k.bin
c139725faa0e105329badfeccd0759d1a706981326780bf6e437b8e6f25e9ae0  bl2-emmc-x1.img
009ee6b05c73659d7aed63162f1f216a94cc25623158930158790fbbc7674853  bl2-emmc-x1.bin
91173d174c96f57c80075464b92018f26545de80c366ba7d74b7ca29b8e725ac  bl2-ram-uartdl.bin
```
