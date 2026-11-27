#!/usr/bin/env python3
"""
u6plus_fixer.py - Linux recovery engine for UniFi U6+ (UAPL6, MT7981) units bricked by eMMC errors.
Port of Fix-U6Plus.ps1 to Python/Linux.

Modes
  Fix        Make a bricked unit boot UniFi 6.7.54 on its own: 1-bit eMMC boot loader in boot0, patched
             UniFi kernel (1-bit eMMC, firmware updates blocked) in SPI flash, U-Boot bootcmd_real.
  Reset      Factory-reset a unit that was already fixed (wipes the UniFi config partition).
  Restore    Revert a fixed unit to its pre-fix state using a backup from backups/.
  RepairFIP  Overwrite only the eMMC U-Boot partition with the stock FIP. Use when the FIP is
             corrupted but the rest of the device is fine.

Options
  --repair-fip   Force rewriting the eMMC U-Boot partition with the stock FIP even if the header looks
                 valid. Use this when the FIP is silently corrupted (Fix mode only).

Usage:
  python3 u6plus_fixer.py [-h] [--mode {Fix,Reset,Restore,RepairFIP}] [--com-port /dev/ttyUSB0]
                          [--adapter eth0] [--factory-reset] [--skip-backup]
                          [--speed {auto,fast,slow}] [--backup-dir BACKUP_DIR]
                          [--repair-fip] [--gui] [--test-soft-reboot]

Dependencies: Python 3.8+, pyserial
"""

import argparse
import datetime
import hashlib
import ipaddress
import os
import pathlib
import re
import shutil
import socket
import struct
import subprocess
import sys
import textwrap
import time
from typing import List, Optional, Tuple

# Optional: pyserial is required
try:
    import serial
except ImportError:
    print("ERROR: pyserial is required. Install it with: pip3 install pyserial")
    sys.exit(1)


# --------------------------------------------------------------------------- constants
PROFILES = {
    "fast": {
        "name": "Linux eMMC 1-bit 25 MHz",
        "openwrt": "openwrt-ram-1bit.itb",
        "kernel": "unifi-6.7.54-1bit-noupdate.itb",
        "write_kernel0": True,
        "test_mib": 32,
    },
    "slow": {
        "name": "Linux eMMC 1-bit 5 MHz",
        "openwrt": "openwrt-ram-1bit-5mhz.itb",
        "kernel": "unifi-6.7.54-1bit-5mhz-noupdate.itb",
        "write_kernel0": True,
        "test_mib": 8,
    },
}
BL2_EMMC = "bl2-emmc-x1-400k"
NOR_OFFSET = "0x100000"
NOR_LIMIT = 0xF00000
OWRT_IP = "192.168.1.1"

SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=10",
]


# --------------------------------------------------------------------------- helpers
class Colors:
    GRAY = "\033[90m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    RESET = "\033[0m"
    BOLD = "\033[1m"


def _colorize(text: str, color: str) -> str:
    if sys.stdout.isatty():
        return f"{color}{text}{Colors.RESET}"
    return text


# --------------------------------------------------------------------------- main engine
class U6PlusFixer:
    def __init__(self, args: argparse.Namespace):
        self.mode = args.mode
        self.com_port = args.com_port
        self.adapter = args.adapter
        self.factory_reset = args.factory_reset
        self.skip_backup = args.skip_backup
        self.backup_dir = args.backup_dir
        self.repair_fip = args.repair_fip
        self.speed = args.speed
        self.gui = args.gui
        self.test_soft_reboot = args.test_soft_reboot

        self.root = pathlib.Path(__file__).parent.resolve()
        self.img = self.root / "images"
        self.bin_dir = self.root / "bin"
        self.stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.run_dir = self.root / "runs" / f"{self.stamp}-{self.mode.lower()}"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = self.run_dir / "fix.log"
        self.console_log = self.run_dir / "console.log"

        self.owrt_ip = OWRT_IP
        self.loader = "emmc"          # 'emmc' or 'ramfip'
        self.powered_once = False
        self.stage_no = 0
        self.last_prompt = ""
        self.got_result = False
        self.warnings = 0
        self.unit_mac = ""
        self.fip_ok = False

        self._port: Optional[serial.Serial] = None
        self._console_buf = bytearray()

        # mtk_uartboot binary path
        self.mtk_uartboot = self.bin_dir / "mtk_uartboot"
        if not self.mtk_uartboot.exists():
            # Fallback: look for any mtk_uartboot binary
            candidates = list(self.bin_dir.glob("mtk_uartboot*"))
            if candidates:
                self.mtk_uartboot = candidates[0]

    # ------------------------------------------------------------------ output
    def emit(self, line: str) -> None:
        if self.gui:
            print(line, flush=True)

    def log(self, message: str, color: str = Colors.GRAY) -> None:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {message}"
        if self.gui:
            self.emit(line)
        else:
            print(_colorize(line, color))
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def warn(self, message: str) -> None:
        self.warnings += 1
        self.emit(f"##WARN {message}")
        self.log(f"WARNING: {message}", Colors.YELLOW)

    def prompt(self, message: str) -> None:
        self.last_prompt = message
        self.emit(f"##PROMPT {message}")
        if not self.gui:
            print()
            print(_colorize(f"  >>> {message} <<<", Colors.YELLOW))
            print()
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(f"[PROMPT] {message}\n")

    def prompt_clear(self) -> None:
        if self.last_prompt:
            self.last_prompt = ""
            self.emit("##PROMPTCLEAR")

    def stage(self, n: int, title: str) -> None:
        if self.stage_no > 0 and self.stage_no != n:
            self.emit(f"##STAGE {self.stage_no} OK")
        self.stage_no = n
        self.emit(f"##STAGE {n} RUN")
        self.log("")
        self.log(f"== Stage {n}: {title}", Colors.CYAN)

    def fail(self, message: str) -> None:
        self.prompt_clear()
        self.emit(f"##STAGE {self.stage_no} FAIL")
        self.log(f"FAILED: {message}", Colors.RED)
        self.log(f"Logs: {self.run_dir}", Colors.RED)
        self.emit(f"##RESULT FAILED {message}")
        self.ser_close()
        self.stop_loader()
        sys.exit(1)

    def power_prompt(self) -> str:
        if not self.powered_once:
            self.powered_once = True
            return "PLUG IN THE AP's POWER (PoE) NOW"
        return "UNPLUG THE PoE, WAIT 5 SECONDS, THEN PLUG IT BACK IN"

    # ------------------------------------------------------------------ serial
    def ser_open(self) -> None:
        if self._port and self._port.is_open:
            return
        self._port = serial.Serial(
            port=self.com_port,
            baudrate=115200,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0,
            write_timeout=5,
        )

    def ser_close(self) -> None:
        if self._port and self._port.is_open:
            self._port.close()
            self._port = None

    def ser_read(self) -> str:
        if not self._port or not self._port.is_open:
            return ""
        data = self._port.read(self._port.in_waiting or 0)
        if data:
            self._console_buf.extend(data)
            with open(self.console_log, "ab") as f:
                f.write(data)
            return data.decode("utf-8", errors="replace")
        return ""

    def ser_wait(self, pattern: str, timeout_sec: int) -> Optional[str]:
        buf = ""
        start = time.time()
        regex = re.compile(pattern)
        while time.time() - start < timeout_sec:
            data = self.ser_read()
            if data:
                buf += data
                if regex.search(buf):
                    return buf
            time.sleep(0.03)
        return None

    def ub(self, cmd: str, timeout_sec: int = 10) -> str:
        for attempt in range(1, 3):
            self.ser_read()
            self._port.write(f"{cmd}\r".encode())
            out = self.ser_wait(r"MT7981#\s*$", timeout_sec)
            if out is not None:
                return out
            self._port.write(b"\x03")
            time.sleep(0.6)
            self.ser_read()
            if timeout_sec > 60:
                break
        raise TimeoutError(f"U-Boot command timed out: {cmd}")

    def catch_uboot(self, timeout_sec: int = 90) -> None:
        esc = b"\x1b"
        mark = len(self._console_buf)
        start = time.time()
        probe_start = time.time()
        hit = None
        while time.time() - start < timeout_sec:
            self._port.write(esc)
            time.sleep(0.02)
            self.ser_read()
            recent = self._console_buf[mark:].decode("utf-8", errors="replace")
            if re.search(r"hw code|BL2: v|NOTICE", recent):
                self.prompt_clear()
            if hit is None and re.search(r"Autobooting|MT7981#", recent):
                hit = time.time()
            if hit and time.time() - hit > 3:
                break
            if re.search(r"Starting kernel|ubnt boot", recent):
                raise RuntimeError("missed the U-Boot autoboot window")
            if hit is None and time.time() - probe_start > 2:
                self._port.write(b"\x03")
                probe_start = time.time()
        if hit is None:
            raise RuntimeError("U-Boot never appeared on the serial console")
        for _ in range(3):
            self._port.write(b"\x03")
            time.sleep(0.4)
        self.ser_read()
        v = self.ub("version", 5)
        if "U-Boot 20" not in v:
            raise RuntimeError("U-Boot prompt not responding")

    # ------------------------------------------------------------------ loader
    def stop_loader(self) -> None:
        try:
            subprocess.run(["pkill", "-f", "mtk_uartboot"], capture_output=True, check=False)
        except FileNotFoundError:
            pass

    def run_loader(
        self,
        loader_args: List[str],
        tag: str,
        ok_pattern: str,
        ok_on_exit_pattern: str = "",
        trigger=None,
    ) -> str:
        self.stop_loader()
        ub_log = self.run_dir / f"mtk_uartboot-{tag}.log"
        ub_err = self.run_dir / f"mtk_uartboot-{tag}.err"
        for p in (ub_log, ub_err):
            if p.exists():
                p.unlink()

        cmd = [str(self.mtk_uartboot)] + loader_args
        env = os.environ.copy()
        env["PWD"] = str(self.img)
        log_fh = open(ub_log, "w")
        err_fh = open(ub_err, "w")
        proc = subprocess.Popen(
            cmd,
            cwd=self.img,
            stdout=log_fh,
            stderr=err_fh,
            env=env,
        )
        prompted = False
        if trigger:
            self.log("  rebooting the AP from software (no need to touch the power)")
            time.sleep(1)
            try:
                trigger()
            except Exception:
                pass
        else:
            self.prompt(self.power_prompt())
            prompted = True

        start = time.time()
        result = "noresponse"
        txt = ""
        while time.time() - start < 300:
            try:
                txt = ""
                if ub_log.exists():
                    txt += ub_log.read_text(errors="replace")
                if ub_err.exists():
                    txt += ub_err.read_text(errors="replace")
            except Exception:
                pass
            if re.search(r"hw code", txt):
                self.prompt_clear()
            elif not prompted and time.time() - start > 45:
                self.warn("The AP did not restart into the loader by itself")
                self.prompt(self.power_prompt())
                prompted = True
            if re.search(ok_pattern, txt):
                result = "ok"
                break
            if re.search(r"panicked", txt):
                result = "rejected"
                break
            if re.search(
                r"Failed to load image|FIP boot source initialization failed|Failed to switch to UDA|PANIC at PC",
                txt,
            ):
                result = "emmc"
                break
            if proc.poll() is not None:
                if ok_on_exit_pattern and re.search(ok_on_exit_pattern, txt):
                    result = "ok"
                break
            time.sleep(0.02)

        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)
        log_fh.close()
        err_fh.close()

        errs = len(re.findall(r"MSDC: (Command has (timed out|CRC error)|CRC error)", txt))
        if errs:
            self.log(f"  boot loader saw {errs} eMMC errors")
        if result == "noresponse" and re.search(r"Handshake", txt) and not re.search(r"hw code", txt):
            self.log("  no reply to the serial handshake")
        return result

    def load_emmc(self, trigger=None) -> str:
        self.log(f"  loading {BL2_EMMC} over UART (U-Boot comes from the eMMC)")
        return self.run_loader(
            [
                "-s", self.com_port,
                "-p", f"{BL2_EMMC}.bin",
                "--aarch64",
                "--brom-load-baudrate", "115200",
                "-f", "u6plus-stock-fip-6.7.54.bin",
            ],
            "emmc",
            r"Hello BL31|U-Boot 20",
            r"Located partition 'u-boot'",
            trigger,
        )

    def load_ramfip(self, trigger=None) -> str:
        self.log("  loading a RAM boot loader and U-Boot over UART (no eMMC needed)")
        return self.run_loader(
            [
                "-s", self.com_port,
                "-p", "bl2-ram-uartdl.bin",
                "--aarch64",
                "--brom-load-baudrate", "115200",
                "--bl2-load-baudrate", "115200",
                "-f", "u6plus-stock-fip-6.7.54.bin",
            ],
            "ramfip",
            r"Received FIP",
            "",
            trigger,
        )

    def reach_uboot(self, soft_reboot=None):
        rejects = 0
        silent = 0
        misses = 0
        trigger = soft_reboot
        while True:
            self.ser_close()
            if self.loader == "emmc":
                r = self.load_emmc(trigger)
            else:
                r = self.load_ramfip(trigger)
            trigger = None
            if r == "ok":
                self.ser_open()
                try:
                    self.catch_uboot(90)
                    self.prompt_clear()
                    self.log("  U-Boot prompt reached")
                    return
                except Exception as e:
                    misses += 1
                    if misses >= 3:
                        self.fail(f"Could not stop U-Boot ({e}) after 3 tries.")
                    self.warn(f"Missed U-Boot ({e}); retrying")
                    continue
            elif r == "rejected":
                rejects += 1
                if rejects >= 3:
                    self.fail("The BootROM rejected the upload 3 times. Check the serial adapter and its cable.")
                self.warn("The BootROM rejected the upload (a known one-off glitch); retrying")
            elif r == "noresponse":
                silent += 1
                if silent >= 2:
                    self.fail("No response from the AP. Check the serial wiring (try swapping RX and TX) and the PoE power.")
                self.warn("No response from the AP. Check the serial wiring (swap RX and TX if unsure); retrying")
            elif r == "emmc":
                if self.loader == "emmc":
                    self.warn("The boot loader could not read U-Boot from the eMMC; switching to loading U-Boot over the serial line")
                    self.loader = "ramfip"
                else:
                    self.fail("Could not start U-Boot over the serial line either.")

    # ------------------------------------------------------------------ network
    def get_pc_ip(self) -> Optional[str]:
        try:
            result = subprocess.run(
                ["ip", "-4", "addr", "show", "dev", self.adapter],
                capture_output=True,
                text=True,
                check=False,
            )
            match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)/\d+", result.stdout)
            if match:
                return match.group(1)
        except FileNotFoundError:
            pass
        return None

    def wait_pc_ip(self, timeout_sec: int = 90) -> Optional[str]:
        start = time.time()
        while time.time() - start < timeout_sec:
            ip = self.get_pc_ip()
            if ip:
                return ip
            time.sleep(2)
        return None

    @staticmethod
    def get_peer_ip(pc: str) -> Tuple[str, str]:
        octets = pc.split(".")
        if octets[0] == "169" and octets[1] == "254":
            last = "201" if octets[3] == "200" else "200"
            return (f"{octets[0]}.{octets[1]}.{octets[2]}.{last}", "255.255.0.0")
        last = "251" if octets[3] == "250" else "250"
        return (f"{octets[0]}.{octets[1]}.{octets[2]}.{last}", "255.255.255.0")

    def tftp_put(self, local_ip: str, server: str, file_path: pathlib.Path) -> None:
        data = file_path.read_bytes()
        srv = ipaddress.ip_address(server)
        local = ipaddress.ip_address(local_ip)

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((str(local), 0))
        sock.settimeout(1.0)
        try:
            one = bytes([0])
            for port in range(1024, 4096):
                sock.sendto(one, (str(srv), port))

            name = file_path.name.encode("ascii")
            req = bytes([0, 2]) + name + b"\x00" + b"octet\x00"
            peer = None
            for _ in range(30):
                if peer:
                    break
                sock.sendto(req, (str(srv), 69))
                try:
                    while True:
                        r, addr = sock.recvfrom(2048)
                        if r[1] == 5:
                            raise RuntimeError("TFTP server returned an error")
                        if r[1] == 4 and r[2] == 0 and r[3] == 0:
                            peer = addr
                            break
                except socket.timeout:
                    pass
            if not peer:
                raise RuntimeError("no answer from U-Boot tftpsrv")

            blk = 512
            n = len(data) // blk + (1 if len(data) % blk else 0)
            for i in range(n):
                bno = (i + 1) & 0xFFFF
                length = min(blk, len(data) - i * blk)
                pkt = bytearray(4 + length)
                pkt[0] = 0
                pkt[1] = 3
                pkt[2] = (bno >> 8) & 0xFF
                pkt[3] = bno & 0xFF
                if length > 0:
                    pkt[4:4 + length] = data[i * blk:i * blk + length]
                acked = False
                for _ in range(30):
                    if acked:
                        break
                    sock.sendto(bytes(pkt), peer)
                    try:
                        while True:
                            a, addr = sock.recvfrom(2048)
                            if a[1] == 4 and ((a[2] << 8) | a[3]) == bno:
                                acked = True
                                break
                    except socket.timeout:
                        pass
                if not acked:
                    raise RuntimeError(f"TFTP timed out at block {i + 1}")
        finally:
            sock.close()

    def push_to_uboot(self, file_path: pathlib.Path) -> None:
        size = file_path.stat().st_size
        for attempt in range(1, 4):
            try:
                pc = self.wait_pc_ip(90)
                if not pc:
                    raise RuntimeError(f"no network link or address on '{self.adapter}' (is the PoE injector's LAN port cabled to this PC?)")
                ap = self.get_peer_ip(pc)
                self.ub(f"setenv ipaddr {ap[0]}")
                self.ub(f"setenv netmask {ap[1]}")
                self.ser_read()
                self._port.write(b"tftpsrv 0x46000000\r")
                if self.ser_wait(r"Loading:", 10) is None:
                    raise RuntimeError("tftpsrv did not start")
                self.tftp_put(pc, ap[0], file_path)
                out = self.ser_wait(r"Bytes transferred = \d+[\s\S]*MT7981#\s*$", 60)
                if out is None or f"Bytes transferred = {size} " not in out:
                    raise RuntimeError("transfer incomplete")
                self.log(f"  sent {file_path.name} ({size:,} bytes) to {ap[0]}")
                return
            except Exception as e:
                if attempt >= 3:
                    raise RuntimeError(f"Could not send {file_path.name} to the AP: {e}")
                self.warn(f"Transfer of {file_path.name} failed ({e}); retrying")
                self._port.write(b"\x03")
                time.sleep(1)
                self._port.write(b"\x03")
                time.sleep(1)
                self.ser_read()

    def drop_signature(self) -> None:
        self.ub("fdt addr ${fdtcontroladdr}")
        self.ub("fdt rm /signature")

    # ------------------------------------------------------------------ ssh
    def ssh_run(self, remote_cmd: str) -> str:
        f = self.run_dir / "remote-cmd.sh"
        f.write_text(remote_cmd + "\n", encoding="utf-8")
        with open(f, "rb") as fh:
            result = subprocess.run(
                ["ssh"] + SSH_OPTS + [f"root@{self.owrt_ip}", "sh", "-s"],
                stdin=fh,
                capture_output=True,
                text=True,
                check=False,
            )
        return (result.stdout + result.stderr).strip()

    def ssh_run_file(self, script_text: str, arg_text: str = "") -> List[str]:
        f = self.run_dir / "remote-script.sh"
        f.write_text(script_text + "\n", encoding="utf-8")
        with open(f, "rb") as fh:
            result = subprocess.run(
                ["ssh"] + SSH_OPTS + [f"root@{self.owrt_ip}", f"sh -s {arg_text}"],
                stdin=fh,
                capture_output=True,
                text=True,
                check=False,
            )
        return (result.stdout + result.stderr).strip().splitlines()

    def ssh_upload(self, local: pathlib.Path, remote: str) -> None:
        for attempt in range(1, 3):
            with open(local, "rb") as fh:
                subprocess.run(
                    ["ssh"] + SSH_OPTS + [f"root@{self.owrt_ip}", f"cat > {remote}"],
                    stdin=fh,
                    capture_output=True,
                    check=False,
                )
            l_hash = hashlib.sha256(local.read_bytes()).hexdigest()
            r_hash = self.ssh_run(f"sha256sum {remote}").split()[0]
            if l_hash == r_hash:
                return
            self.warn(f"Upload of {local.name} was corrupted; retrying")
        raise RuntimeError(f"Upload of {local.name} failed twice")

    def ssh_backup(self, name: str, remote_cmd: str, bdir: pathlib.Path) -> None:
        r = self.ssh_run(f"{remote_cmd} > /tmp/bk.bin && sha256sum /tmp/bk.bin").split()[0]
        f = bdir / name
        for attempt in range(1, 3):
            with open(f, "wb") as fh:
                subprocess.run(
                    ["ssh"] + SSH_OPTS + [f"root@{self.owrt_ip}", "cat /tmp/bk.bin"],
                    stdout=fh,
                    capture_output=False,
                    check=False,
                )
            l_hash = hashlib.sha256(f.read_bytes()).hexdigest()
            if l_hash == r:
                break
            if attempt >= 2:
                raise RuntimeError(f"Backup {name} does not match the AP")
        self.ssh_run("rm -f /tmp/bk.bin")
        with open(bdir / "SHA256SUMS.txt", "a", encoding="utf-8") as fh:
            fh.write(f"{r}  {name}\n")
        self.log(f"  {name:26} {f.stat().st_size:12,} bytes  verified")

    def test_ssh(self, ip: str) -> bool:
        result = subprocess.run(
            ["ssh"] + SSH_OPTS + [f"root@{ip}", "true"],
            capture_output=True,
            check=False,
        )
        return result.returncode == 0

    # ------------------------------------------------------------------ OpenWrt
    def boot_openwrt(self, prf: dict) -> None:
        self.push_to_uboot(self.img / prf["openwrt"])
        self.drop_signature()
        o = self.ub("iminfo 0x46000000", 30)
        if "Hash(es) for Image 0 (kernel-1): crc32+ sha1+" not in o:
            raise RuntimeError("OpenWrt image failed its hash check in U-Boot")
        self.ser_read()
        self._port.write(b"bootm 0x46000000\r")
        if self.ser_wait(r"Please press Enter to activate this console", 180) is None:
            raise RuntimeError("OpenWrt did not finish booting")
        self.log("  OpenWrt is up (running from RAM)")

        # Path 1: DHCP
        start = time.time()
        while not re.match(r"192\.168\.1\.\d+", self.get_pc_ip() or "") and time.time() - start < 60:
            time.sleep(2)
        self.owrt_ip = "192.168.1.1"
        for _ in range(10):
            if self.test_ssh(self.owrt_ip):
                self.log(f"  SSH to OpenWrt at {self.owrt_ip}")
                return
            time.sleep(3)

        # Path 2: link-local fallback
        pc = self.get_pc_ip()
        if not pc:
            raise RuntimeError("The PC has no address on the AP link")
        peer = self.get_peer_ip(pc)
        bits = 16 if peer[1] == "255.255.0.0" else 24
        self.warn(f"No DHCP address from OpenWrt; adding {peer[0]} to OpenWrt over the serial console")
        self._port.write(b"\n")
        time.sleep(1)
        self._port.write(f"ip addr add {peer[0]}/{bits} dev br-lan\n".encode())
        time.sleep(2)
        self.ser_read()
        self.owrt_ip = peer[0]
        for _ in range(10):
            if self.test_ssh(self.owrt_ip):
                self.log(f"  SSH to OpenWrt at {self.owrt_ip}")
                return
            time.sleep(3)
        raise RuntimeError("Cannot reach OpenWrt over SSH")

    def get_emmc_errors(self) -> int:
        n = self.ssh_run(
            r'dmesg | grep -iE "msdc|mmc0" | grep -viE "cmd=(52|8|5|55) " | grep -ciE "error|timeout|crc"'
        )
        if re.match(r"^\d+$", n):
            return int(n)
        return 999

    def test_emmc_present(self) -> bool:
        emmc = self.ssh_run(
            'grep -E "^(clock|bus width)" /sys/kernel/debug/mmc0/ios; '
            'cat /sys/block/mmcblk0/device/name 2>/dev/null; '
            'test -b /dev/mmcblk0 && echo BLOCK-OK'
        )
        self.log(f"  eMMC: {re.sub(r'\s+', ' ', emmc)}")
        return bool(re.search(r"bus width:\s+0 \(1 bits\)", emmc) and "BLOCK-OK" in emmc)

    def test_emmc(self, mib: int, base_errors: int = 0) -> bool:
        if not self.test_emmc_present():
            self.log("  eMMC is not present or not in 1-bit mode")
            return False
        lines = self.ssh_run(
            f"for i in 1 2; do echo 3 > /proc/sys/vm/drop_caches; "
            f"dd if=/dev/mmcblk0 bs=1M count={mib} 2>/dev/null | md5sum | cut -d' ' -f1; done"
        ).splitlines()
        lines = [l for l in lines if l.strip()]
        errs = self.get_emmc_errors()
        ok = (
            len(lines) >= 2
            and re.match(r"^[0-9a-f]{32}$", lines[0])
            and lines[0] == lines[1]
            and errs <= base_errors
        )
        self.log(f"  read test: {mib} MiB twice -> {' '.join(lines)} (new eMMC errors: {errs - base_errors})")
        return ok

    def set_emmc_slow_clock(self) -> bool:
        o = self.ssh_run(
            "echo 5000000 > /sys/kernel/debug/mmc0/clock; grep -E '^clock' /sys/kernel/debug/mmc0/ios"
        )
        return bool(re.search(r"clock:\s+[1-5]\d{6} Hz", o))

    def check_layout(self) -> None:
        layout_cmd = (
            'for d in /sys/block/mmcblk0/mmcblk0p*; do echo "$(basename $d)=$(grep PARTNAME $d/uevent | cut -d= -f2)"; done\n'
            'echo fip=$(dd if=/dev/mmcblk0p4 bs=8 count=1 2>/dev/null | hexdump -v -e "8/1 \\"%02x\\"")\n'
            'echo eeprom=$(dd if=/dev/mtd0 bs=64 count=1 2>/dev/null | hexdump -v -e "64/1 \\"%02x\\"")\n'
            'grep -E "mtd[0-9]" /proc/mtd | tr "\\n" " "'
        )
        info = self.ssh_run(layout_cmd)
        mapping = {}
        for line in info.splitlines():
            m = re.match(r"^(\w+)=(.*)$", line)
            if m:
                mapping[m.group(1)] = m.group(2).strip()
        want = {
            "mmcblk0p1": "bl2",
            "mmcblk0p4": "u-boot",
            "mmcblk0p5": "EEPROM",
            "mmcblk0p6": "kernel0",
            "mmcblk0p9": "cfg",
        }
        for k, v in want.items():
            if mapping.get(k) != v:
                raise RuntimeError(f"Unexpected eMMC partition layout ({k} is '{mapping.get(k)}', expected '{v}'). Not touching this unit.")
        self.log("  eMMC partition layout matches the U6+")
        self.fip_ok = mapping.get("fip") == "010064aa78563412"
        if self.fip_ok:
            self.log("  U-Boot partition has a valid FIP header")
        else:
            self.warn(f"U-Boot partition header is not a valid FIP ({mapping.get('fip')}); it will be repaired")
        ee = mapping.get("eeprom", "")
        if not ee or re.match(r"^(ff)+$", ee) or re.match(r"^(00)+$", ee):
            self.warn("The Wi-Fi calibration area (SPI EEPROM) looks blank. Wi-Fi may not work on this unit regardless of the fix.")
        if "mtd2: 00f70000" not in info:
            raise RuntimeError("SPI flash layout is not the expected 16 MB chip")

    # ================================================================== main
    def run(self) -> None:
        self.log(f"U6+ eMMC recovery - mode {self.mode} - run {self.stamp}", Colors.WHITE)
        if self.mode == "Fix":
            self.emit(
                "##STAGES Pre-flight checks|Reach U-Boot|"
                "Boot OpenWrt in RAM and test the eMMC|Back up this unit|"
                "Write boot loader to the eMMC|Write UniFi kernel to SPI flash|Unattended test boot"
            )
        elif self.mode == "Reset":
            self.emit(
                "##STAGES Pre-flight checks|Reach U-Boot|Boot OpenWrt in RAM|"
                "Back up the UniFi config|Wipe the UniFi config|Test boot with factory defaults"
            )
        elif self.mode == "Restore":
            self.emit(
                "##STAGES Pre-flight checks|Reach U-Boot|Boot OpenWrt in RAM|"
                "Restore eMMC from backup|Restore SPI flash from backup|Verify restored boot behavior"
            )
        elif self.mode == "RepairFIP":
            self.emit(
                "##STAGES Pre-flight checks|Reach U-Boot|Boot OpenWrt in RAM|"
                "Write stock FIP to eMMC|Test boot"
            )

        if self.mode == "Restore" and not self.backup_dir:
            self.fail("Restore mode requires --backup-dir. Use: --backup-dir backups/MAC-YYYYMMDD-HHMMSS")

        # ------------------------------------------------------------- pre-flight
        self.stage(1, "Pre-flight checks")
        sums_file = self.root / "SHA256SUMS.txt"
        if not sums_file.exists():
            self.fail("The firmware images have not been prepared yet. Run prepare.sh once (needs Python 3 and internet).")
        sums = sums_file.read_text(encoding="utf-8").splitlines()
        for line in sums:
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            h, n = parts
            fp = self.root / n
            if not fp.exists():
                self.fail(f"Missing package file {n}")
            if hashlib.sha256(fp.read_bytes()).hexdigest() != h:
                self.fail(f"Package file {n} is corrupted (hash mismatch). Re-copy the tool folder.")
        self.log(f"  all {len(sums)} package files present and intact")

        if not self.com_port:
            ports = self._find_serial_ports()
            if len(ports) != 1:
                self.fail(f"Could not pick the serial port automatically (found: {', '.join(ports)}). Choose it explicitly.")
            self.com_port = ports[0]
        if not pathlib.Path(self.com_port).exists():
            self.fail(f"Serial port {self.com_port} does not exist. Plug in the USB-serial adapter and pick its port.")
        try:
            self.ser_open()
            self.ser_close()
        except Exception as e:
            self.fail(f"Serial port {self.com_port} is in use by another program (close minicom or any serial terminal). {e}")
        self.log(f"  serial port: {self.com_port} (free)")

        if not self.adapter:
            adapters = self._find_wired_adapters()
            if len(adapters) != 1:
                self.fail(f"Could not pick the Ethernet adapter automatically (found {len(adapters)} wired adapters). Choose it explicitly.")
            self.adapter = adapters[0]
        if not self._adapter_exists(self.adapter):
            self.fail(f"Network adapter '{self.adapter}' not found")
        self.log(f"  network adapter: {self.adapter} ({self._adapter_state(self.adapter)})")

        # Check gateway / IP
        gw = self._get_gateway()
        ip = self.get_pc_ip()
        if gw and ip and not ip.startswith("169.254.") and not ip.startswith("192.168.1."):
            self.fail(
                f"'{self.adapter}' is connected to a network ({ip}, with a gateway). "
                "Cable the PoE injector straight to this PC: the recovery system runs a DHCP server that must not reach a real LAN."
            )
        if self._adapter_state(self.adapter) == "up":
            self.warn("'{self.adapter}' already has a link. Make sure the AP is UNPOWERED before you continue (the tool will ask you to plug it in).")

        if not shutil.which("ssh"):
            self.fail("OpenSSH client (ssh) not found. Install it with your package manager.")

        free = shutil.disk_usage(self.root).free
        if free < 200 * 1024 * 1024:
            self.fail(f"Less than 200 MB free on {self.root}: for backups and logs")

        self.stop_loader()

        try:
            self._run_recovery()
        except Exception as e:
            self.fail(str(e))

    def _run_recovery(self) -> None:
        first = "slow" if self.speed == "slow" else "fast"
        prof = None

        self.stage(2, "Reach U-Boot")
        self.reach_uboot()

        if self.mode == "Fix":
            self.stage(3, f"Boot OpenWrt in RAM and test the eMMC ({PROFILES[first]['name']})")
        else:
            self.stage(3, "Boot OpenWrt in RAM")
        self.boot_openwrt(PROFILES[first])

        if self.mode == "RepairFIP":
            self._do_repair_fip()
            return

        if first == "slow":
            if self.test_emmc(PROFILES["slow"]["test_mib"]):
                prof = PROFILES["slow"]
            else:
                self.fail("The eMMC is not reliable even at 5 MHz. This unit cannot be fixed this way.")
        elif (not self.test_soft_reboot) and self.test_emmc(PROFILES["fast"]["test_mib"]):
            prof = PROFILES["fast"]
        elif self.speed == "fast":
            self.fail("The eMMC is not reliable at 25 MHz (you chose Fast). Run again with Auto or Slow.")
        else:
            self.warn("The eMMC is not reliable at 25 MHz; switching to 5 MHz")
            if (not self.test_soft_reboot) and self.test_emmc_present() and self.set_emmc_slow_clock():
                base = self.get_emmc_errors()
                if self.test_emmc(PROFILES["slow"]["test_mib"], base):
                    prof = PROFILES["slow"]
                    self.log("  eMMC is reliable at 5 MHz (switched in place, no reboot needed)")
            if not prof:
                self.warn("Restarting into the 5 MHz recovery system")
                self.emit("##STAGE 3 RETRY")
                self.stage_no = 0
                self.stage(2, "Reach U-Boot")
                self.reach_uboot(soft_reboot=lambda: self.ssh_run("reboot"))
                self.stage(3, f"Boot OpenWrt in RAM and test the eMMC ({PROFILES['slow']['name']})")
                self.boot_openwrt(PROFILES["slow"])
                if self.test_emmc(PROFILES["slow"]["test_mib"]):
                    prof = PROFILES["slow"]
                else:
                    self.fail("The eMMC is not reliable even at 5 MHz. This unit cannot be fixed this way.")

        self.log(f"  profile: {prof['name']}", Colors.WHITE)
        mac = self.ssh_run("cat /sys/class/net/eth0/address").replace(":", "").upper()
        if not re.match(r"^[0-9A-F]{12}$", mac):
            raise RuntimeError(f"Could not read the unit's MAC address ({mac})")
        self.log(f"  unit MAC: {mac}")
        self.emit(f"##UNIT {mac}")
        self.check_layout()

        if self.mode == "Fix":
            self._do_fix(prof, mac)
        elif self.mode == "Reset":
            self._do_reset(mac)
        elif self.mode == "Restore":
            self._do_restore(mac)
            return

        # ------------------------------------------------- shared: check the boot
        mark = len(self._console_buf)
        o = self.ser_wait(r"Please press Enter to activate this console|login:", 300)
        if o is None:
            boot = self._console_buf[mark:].decode("utf-8", errors="replace")
            if re.search(r"Failed to load image|FIP boot source initialization failed", boot):
                raise RuntimeError("The unit's boot loader could not read the eMMC on its own. This unit's eMMC is too unreliable for this fix.")
            if re.search(r"ubnt boot", boot):
                raise RuntimeError("U-Boot ran the stock boot instead of the SPI flash kernel. In Reset mode this means the unit was never fixed: run Fix first.")
            raise RuntimeError("UniFi did not finish booting")
        time.sleep(5)
        self._port.write(b"\r")
        o = self.ser_wait(r"login:", 30)
        boot = self._console_buf[mark:].decode("utf-8", errors="replace")
        if o is None:
            raise RuntimeError("UniFi did not show its login prompt")
        if "bytes @ 0x100000 Read: OK" not in boot:
            raise RuntimeError("U-Boot did not load the kernel from SPI flash")
        if "ubnthal: Ubiquiti U6+" not in boot:
            raise RuntimeError("UniFi booted but did not identify the board as U6+")
        crc = len([
            m for m in re.finditer(r"(?im)^.*(msdc|mmc0).*(error|crc|timeout).*$", boot)
            if not re.search(r"cmd=(52|8|5|55) ", m.group(0))
        ])
        if crc > 0:
            raise RuntimeError(f"eMMC reported {crc} errors during boot")
        hostname_match = re.search(r"(\S+) login:", boot)
        hostname = hostname_match.group(1) if hostname_match else "unknown"
        if self.mode == "Reset" or self.factory_reset:
            if "running.cfg" in boot:
                self.warn("UniFi still loaded a saved config: the reset may not have taken effect")
            else:
                self.log("  UniFi started with factory defaults (no saved config loaded)")
        self.log(f"  UniFi booted unattended (prompt '{hostname} login:'), eMMC errors: 0")

        self.emit(f"##STAGE {self.stage_no} OK")
        self.ser_close()
        self.stop_loader()
        self.log("")
        if self.mode == "Fix":
            self.log("SUCCESS: unit fixed. Disconnect the serial adapter, connect the AP to the network and adopt it.", Colors.GREEN)
            self.log("Turn off automatic firmware updates for this unit in the UniFi controller.", Colors.GREEN)
        else:
            self.log("SUCCESS: unit reset to factory defaults. Disconnect the serial adapter; it will appear in the controller ready to adopt.", Colors.GREEN)
        self.log(f"Logs: {self.run_dir}", Colors.GREEN)
        self.emit("##RESULT SUCCESS")

    def _do_fix(self, prof: dict, mac: str) -> None:
        # ------------------------------------------------------- backup
        self.stage(4, "Back up this unit")
        if self.skip_backup:
            self.warn("Backup skipped at your request")
        else:
            bdir = self.root / "backups" / f"{mac}-{self.stamp}"
            bdir.mkdir(parents=True, exist_ok=True)
            info = self.ssh_run(
                "cat /sys/kernel/debug/mmc0/mmc0:0001/ext_csd; echo; "
                "for f in name cid date life_time pre_eol_info; do echo \"$f=$(cat /sys/block/mmcblk0/device/$f)\"; done"
            )
            (bdir / "emmc-info.txt").write_text(info + "\n", encoding="ascii")
            self.log(f"  eMMC wear: {self.ssh_run('echo life_time=$(cat /sys/block/mmcblk0/device/life_time) pre_eol=$(cat /sys/block/mmcblk0/device/pre_eol_info)')}")
            self.ssh_backup("boot0.bin", "dd if=/dev/mmcblk0boot0 bs=64k 2>/dev/null", bdir)
            self.ssh_backup("boot1.bin", "dd if=/dev/mmcblk0boot1 bs=64k 2>/dev/null", bdir)
            self.ssh_backup("emmc-gpt-p1-p5.bin", "dd if=/dev/mmcblk0 bs=512 count=17536 2>/dev/null", bdir)
            self.ssh_backup("emmc-p8-bs.bin", "dd if=/dev/mmcblk0p8 bs=512 2>/dev/null", bdir)
            self.ssh_backup("emmc-p9-cfg.bin", "dd if=/dev/mmcblk0p9 bs=64k 2>/dev/null", bdir)
            self.ssh_backup(
                "emmc-gpt-backup.bin",
                "dd if=/dev/mmcblk0 bs=512 skip=$(( $(cat /sys/block/mmcblk0/size) - 2048 )) count=2048 2>/dev/null",
                bdir,
            )
            self.ssh_backup("nor-mtd0-EEPROM.bin", "cat /dev/mtd0", bdir)
            self.ssh_backup("nor-mtd1-u-boot-env.bin", "cat /dev/mtd1", bdir)
            self.ssh_backup("nor-mtd2-rest-0x90000.bin", "cat /dev/mtd2", bdir)
            self.log(f"  backups in {bdir}")

        # ------------------------------------------------- eMMC writes
        self.stage(5, "Write boot loader to the eMMC")
        repair_fip = self.repair_fip or (not self.fip_ok) or (self.loader == "ramfip")
        if self.repair_fip and self.fip_ok:
            self.log("  --repair-fip requested: U-Boot partition will be rewritten regardless of header")
        self.ssh_upload(self.img / f"{BL2_EMMC}.img", "/tmp/bl2.img")
        if prof["write_kernel0"]:
            self.ssh_upload(self.img / "unifi-6.7.54-kernel0-original.itb", "/tmp/kernel0.itb")
        else:
            self.log("  kernel0 copy skipped (not used for booting)")
        if repair_fip:
            self.log("  the eMMC U-Boot partition will be rewritten with the stock 6.7.54 FIP")
            self.ssh_upload(self.img / "u6plus-stock-fip-6.7.54.bin", "/tmp/fip.bin")

        sh = textwrap.dedent(
            """\
            set -e
            part() { for d in /sys/block/mmcblk0/mmcblk0p*; do grep -q "^PARTNAME=$1$" $d/uevent && { echo /dev/$(basename $d); return 0; }; done; return 1; }
            K=$(part kernel0); C=$(part cfg); U=$(part u-boot)
            [ "$K" = /dev/mmcblk0p6 ] && [ "$C" = /dev/mmcblk0p9 ] && [ "$U" = /dev/mmcblk0p4 ] || { echo "LAYOUT-MISMATCH $K $C $U"; exit 1; }
            verify() { w=$(sha256sum "$1" | cut -d" " -f1); g=$(head -c "$(wc -c < "$1")" "$2" | sha256sum | cut -d" " -f1); [ "$w" = "$g" ] && echo "VERIFY-OK $2" || { echo "VERIFY-FAIL $2"; exit 1; }; }
            if [ -f /tmp/fip.bin ]; then dd if=/tmp/fip.bin of=$U bs=512 2>/dev/null; sync; echo 3 > /proc/sys/vm/drop_caches; verify /tmp/fip.bin $U; fi
            echo 0 > /sys/block/mmcblk0boot0/force_ro
            dd if=/tmp/bl2.img of=/dev/mmcblk0boot0 bs=512 2>/dev/null; sync
            echo 1 > /sys/block/mmcblk0boot0/force_ro
            echo 3 > /proc/sys/vm/drop_caches
            verify /tmp/bl2.img /dev/mmcblk0boot0
            if [ -f /tmp/kernel0.itb ]; then dd if=/tmp/kernel0.itb of=$K bs=512 2>/dev/null; sync; echo 3 > /proc/sys/vm/drop_caches; verify /tmp/kernel0.itb $K; fi
            if [ "$1" = wipecfg ]; then dd if=/dev/zero of=$C bs=512 count=$(cat /sys/block/mmcblk0/$(basename $C)/size) 2>/dev/null; sync; echo CFG-WIPED; fi
            n=$(dmesg | grep -iE "msdc|mmc0" | grep -viE "cmd=(52|8|5|55) " | grep -ciE "error|timeout|crc" || true); echo "EMMC-ERRORS $n"
            echo INSTALL-OK
            """
        )
        arg = "wipecfg" if self.factory_reset else ""
        res = self.ssh_run_file(sh, arg)
        for line in res:
            self.log(f"  {line}")
        txt = "\n".join(res)
        if "INSTALL-OK" not in txt:
            raise RuntimeError("eMMC write step failed (see above)")
        m = re.search(r"EMMC-ERRORS ([1-9]\d*)", txt)
        if m:
            self.warn(f"Linux logged {m.group(1)} eMMC errors during the writes (all writes verified OK)")

        # ------------------------------------------------- SPI flash
        self.stage(6, "Write UniFi kernel to SPI flash")
        kernel_file = self.img / prof["kernel"]
        size = kernel_file.stat().st_size
        if size >= NOR_LIMIT:
            raise RuntimeError("Kernel image too large for the SPI flash area")
        hex_size = f"0x{size:x}"
        self.ser_read()
        self.ssh_run("reboot")
        caught = False
        for attempt in range(1, 3):
            try:
                self.catch_uboot(120)
                caught = True
                self.log("  U-Boot reached again, this time from the new boot loader in the eMMC")
                break
            except Exception as e:
                self.warn(f"Missed U-Boot after reboot ({e})")
                self.prompt("UNPLUG THE PoE, WAIT 5 SECONDS, THEN PLUG IT BACK IN")
        if not caught:
            self.warn("U-Boot did not come up from the eMMC boot loader; reaching it over the serial line instead (the unit may not boot on its own)")
            self.reach_uboot()
        time.sleep(5)
        self.push_to_uboot(kernel_file)
        self.drop_signature()
        o = self.ub("iminfo 0x46000000", 30)
        if "Hash(es) for Image 0 (kernel-1): sha1+" not in o:
            raise RuntimeError("UniFi kernel failed its hash check in U-Boot")
        o = self.ub("sf probe", 10)
        if "16 MiB" not in o:
            raise RuntimeError("SPI flash is not the expected 16 MB chip")
        written = False
        for attempt in range(1, 3):
            self.log(f"  writing {size} bytes to SPI flash at {NOR_OFFSET} (about 90 s)")
            o = self.ub(f"sf update 0x46000000 {NOR_OFFSET} {hex_size}", 400)
            m = re.search(r"(\d+) bytes written, (\d+) bytes skipped", o)
            if not m or (int(m.group(1)) + int(m.group(2))) != size:
                self.warn("SPI flash write did not report completion")
                continue
            if int(m.group(2)) == size:
                self.log("  SPI flash already held this exact kernel (nothing needed writing)")
            self.ub(f"mw.b 0x4A000000 0x00 {hex_size}", 30)
            self.ub(f"sf read 0x4A000000 {NOR_OFFSET} {hex_size}", 120)
            o = self.ub(f"cmp.b 0x46000000 0x4A000000 {hex_size}", 120)
            if re.search(rf"Total of {size} byte\(s\) were the same", o):
                written = True
            else:
                self.warn("SPI flash read-back differs; writing again")
        if not written:
            raise RuntimeError("SPI flash write could not be verified after 2 tries")
        self.log("  SPI flash read-back matches byte for byte")
        self.ub("setenv ipaddr 192.168.1.20")
        self.ub("setenv netmask 255.255.255.0")
        bc = f"sf probe; sf read 0x46000000 {NOR_OFFSET} {hex_size}; fdt addr ${{fdtcontroladdr}}; fdt rm /signature; bootm 0x46000000"
        self.ub(f"setenv bootcmd_real '{bc}'")
        o = self.ub("printenv bootcmd_real")
        if f"sf read 0x46000000 {NOR_OFFSET} {hex_size}" not in o:
            raise RuntimeError("bootcmd_real was not set correctly")
        o = self.ub("saveenv", 30)
        if "OK" not in o:
            raise RuntimeError("saveenv failed")
        self.log("  boot setting saved")

        # ------------------------------------------------- test boot
        self.stage(7, "Unattended test boot")
        self.ser_read()
        mark = len(self._console_buf)
        self._port.write(b"reset\r")

    def _do_reset(self, mac: str) -> None:
        self.stage(4, "Back up the UniFi config")
        if self.skip_backup:
            self.warn("Backup skipped at your request")
        else:
            bdir = self.root / "backups" / f"{mac}-{self.stamp}-reset"
            bdir.mkdir(parents=True, exist_ok=True)
            self.ssh_backup("emmc-p9-cfg.bin", "dd if=/dev/mmcblk0p9 bs=64k 2>/dev/null", bdir)
            self.ssh_backup("nor-mtd1-u-boot-env.bin", "cat /dev/mtd1", bdir)
            self.log(f"  backup in {bdir}")

        self.stage(5, "Wipe the UniFi config")
        sh = textwrap.dedent(
            """\
            set -e
            C=/dev/mmcblk0p9
            grep -q "^PARTNAME=cfg$" /sys/block/mmcblk0/mmcblk0p9/uevent || { echo LAYOUT-MISMATCH; exit 1; }
            dd if=/dev/zero of=$C bs=512 count=$(cat /sys/block/mmcblk0/mmcblk0p9/size) 2>/dev/null; sync
            echo 3 > /proc/sys/vm/drop_caches
            n=$(hexdump -v -e "16/1 \"%02x\"" $C | grep -c "[1-9a-f]" || true)
            [ "$n" = 0 ] && echo CFG-ZERO-OK || { echo "CFG-NOT-ZERO $n"; exit 1; }
            echo WIPE-OK
            """
        )
        res = self.ssh_run_file(sh, "")
        for line in res:
            self.log(f"  {line}")
        if "WIPE-OK" not in "\n".join(res):
            raise RuntimeError("Wiping the config partition failed (see above)")

        self.stage(6, "Test boot with factory defaults")
        self.ser_read()
        mark = len(self._console_buf)
        self.ssh_run("reboot")

    # ------------------------------------------------------------------ repair FIP
    def _do_repair_fip(self) -> None:
        self.check_layout()
        mac = self.ssh_run("cat /sys/class/net/eth0/address").replace(":", "").upper()
        if not re.match(r"^[0-9A-F]{12}$", mac):
            raise RuntimeError(f"Could not read the unit's MAC address ({mac})")
        self.log(f"  unit MAC: {mac}")
        self.emit(f"##UNIT {mac}")

        self.stage(4, "Write stock FIP to eMMC")
        if not self.skip_backup:
            bdir = self.root / "backups" / f"{mac}-{self.stamp}-repairfip"
            bdir.mkdir(parents=True, exist_ok=True)
            self.ssh_backup("emmc-p4-u-boot-pre-repair.bin", "dd if=/dev/mmcblk0p4 bs=512 2>/dev/null", bdir)
            self.log(f"  backup in {bdir}")

        self.ssh_upload(self.img / "u6plus-stock-fip-6.7.54.bin", "/tmp/fip.bin")
        sh = textwrap.dedent(
            """\
            set -e
            part() { for d in /sys/block/mmcblk0/mmcblk0p*; do grep -q "^PARTNAME=$1$" $d/uevent && { echo /dev/$(basename $d); return 0; }; done; return 1; }
            U=$(part u-boot)
            [ "$U" = /dev/mmcblk0p4 ] || { echo "LAYOUT-MISMATCH $U"; exit 1; }
            verify() { w=$(sha256sum "$1" | cut -d" " -f1); g=$(head -c "$(wc -c < "$1")" "$2" | sha256sum | cut -d" " -f1); [ "$w" = "$g" ] && echo "VERIFY-OK $2" || { echo "VERIFY-FAIL $2"; exit 1; }; }
            dd if=/tmp/fip.bin of=$U bs=512 2>/dev/null; sync; echo 3 > /proc/sys/vm/drop_caches; verify /tmp/fip.bin $U
            n=$(dmesg | grep -iE "msdc|mmc0" | grep -viE "cmd=(52|8|5|55) " | grep -ciE "error|timeout|crc" || true); echo "EMMC-ERRORS $n"
            echo FIP-REPAIR-OK
            """
        )
        res = self.ssh_run_file(sh, "")
        for line in res:
            self.log(f"  {line}")
        txt = "\n".join(res)
        if "FIP-REPAIR-OK" not in txt:
            raise RuntimeError("FIP repair failed (see above)")
        m = re.search(r"EMMC-ERRORS ([1-9]\d*)", txt)
        if m:
            self.warn(f"Linux logged {m.group(1)} eMMC errors during the write (write verified OK)")

        self.stage(5, "Test boot")
        self.ser_read()
        mark = len(self._console_buf)
        self.ssh_run("reboot")
        o = self.ser_wait(r"Please press Enter to activate this console|login:|ubnt boot|MT7981#", 300)
        if o is None:
            boot = self._console_buf[mark:].decode("utf-8", errors="replace")
            if re.search(r"Failed to load image|FIP boot source initialization failed", boot):
                raise RuntimeError("The unit's boot loader could not read the eMMC on its own. The FIP repair did not help.")
            raise RuntimeError("UniFi did not finish booting")
        time.sleep(5)
        self._port.write(b"\r")
        o = self.ser_wait(r"login:", 30)
        boot = self._console_buf[mark:].decode("utf-8", errors="replace")
        if o is None:
            raise RuntimeError("UniFi did not show its login prompt")
        crc = len([
            m for m in re.finditer(r"(?im)^.*(msdc|mmc0).*(error|crc|timeout).*$", boot)
            if not re.search(r"cmd=(52|8|5|55) ", m.group(0))
        ])
        if crc > 0:
            raise RuntimeError(f"eMMC reported {crc} errors during boot")
        hostname_match = re.search(r"(\S+) login:", boot)
        hostname = hostname_match.group(1) if hostname_match else "unknown"
        self.log(f"  UniFi booted unattended (prompt '{hostname} login:'), eMMC errors: 0")

        self.emit(f"##STAGE {self.stage_no} OK")
        self.ser_close()
        self.stop_loader()
        self.log("")
        self.log("SUCCESS: FIP repaired. The unit should now boot normally.", Colors.GREEN)
        self.log(f"Logs: {self.run_dir}", Colors.GREEN)
        self.emit("##RESULT SUCCESS")

    # ------------------------------------------------------------------ restore
    def _validate_backup(self, bdir: pathlib.Path) -> None:
        if not bdir.exists():
            raise RuntimeError(f"Backup directory not found: {bdir}")
        sums_file = bdir / "SHA256SUMS.txt"
        if not sums_file.exists():
            raise RuntimeError(f"Backup SHA256SUMS.txt not found in {bdir}")
        sums = {}
        for line in sums_file.read_text(encoding="utf-8").splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2:
                sums[parts[1]] = parts[0]
        required = [
            "boot0.bin", "boot1.bin", "emmc-gpt-p1-p5.bin",
            "emmc-p8-bs.bin", "emmc-p9-cfg.bin", "emmc-gpt-backup.bin",
            "nor-mtd0-EEPROM.bin", "nor-mtd1-u-boot-env.bin", "nor-mtd2-rest-0x90000.bin",
        ]
        missing = []
        for name in required:
            fpath = bdir / name
            if not fpath.exists():
                missing.append(name)
                continue
            if name in sums:
                h = hashlib.sha256(fpath.read_bytes()).hexdigest()
                if h != sums[name]:
                    raise RuntimeError(f"Backup file {name} is corrupted (hash mismatch)")
        if missing:
            raise RuntimeError(f"Backup is missing required files: {', '.join(missing)}")
        self.log(f"  backup validated: {bdir}")

    def _get_mtd_layout(self) -> List[dict]:
        info = self.ssh_run("cat /proc/mtd")
        parts = []
        for line in info.splitlines():
            m = re.match(r'^mtd(\d+): ([0-9a-f]+) ([0-9a-f]+) "(.+)"$', line)
            if m:
                parts.append({
                    "index": int(m.group(1)),
                    "size": int(m.group(2), 16),
                    "erasesize": int(m.group(3), 16),
                    "name": m.group(4),
                })
        offset = 0
        for p in sorted(parts, key=lambda x: x["index"]):
            p["offset"] = offset
            offset += p["size"]
        return parts

    def _restore_emmc_from_backup(self, bdir: pathlib.Path) -> None:
        files = {
            "boot0.bin": "/tmp/boot0.bin",
            "boot1.bin": "/tmp/boot1.bin",
            "emmc-gpt-p1-p5.bin": "/tmp/gpt.bin",
            "emmc-p8-bs.bin": "/tmp/p8.bin",
            "emmc-p9-cfg.bin": "/tmp/p9.bin",
            "emmc-gpt-backup.bin": "/tmp/gpt-backup.bin",
        }
        for name, remote in files.items():
            self.ssh_upload(bdir / name, remote)
        sh = textwrap.dedent(
            """\
            set -e
            part() { for d in /sys/block/mmcblk0/mmcblk0p*; do grep -q "^PARTNAME=$1$" $d/uevent && { echo /dev/$(basename $d); return 0; }; done; return 1; }
            P8=$(part bs); P9=$(part cfg)
            [ -n "$P8" ] && [ -n "$P9" ] || { echo "LAYOUT-MISMATCH P8=$P8 P9=$P9"; exit 1; }
            verify() { w=$(sha256sum "$1" | cut -d" " -f1); sz=$(wc -c < "$1"); g=$(head -c "$sz" "$2" | sha256sum | cut -d" " -f1); [ "$w" = "$g" ] && echo "VERIFY-OK $2" || { echo "VERIFY-FAIL $2"; exit 1; }; }
            verify_at() { w=$(sha256sum "$1" | cut -d" " -f1); sz=$(wc -c < "$1"); g=$(dd if="$3" bs=512 skip="$2" count=$(( (sz + 511) / 512 )) 2>/dev/null | head -c "$sz" | sha256sum | cut -d" " -f1); [ "$w" = "$g" ] && echo "VERIFY-OK $3@$2" || { echo "VERIFY-FAIL $3@$2"; exit 1; }; }
            echo 0 > /sys/block/mmcblk0boot0/force_ro
            dd if=/tmp/boot0.bin of=/dev/mmcblk0boot0 bs=512 2>/dev/null; sync
            echo 1 > /sys/block/mmcblk0boot0/force_ro
            echo 3 > /proc/sys/vm/drop_caches
            verify /tmp/boot0.bin /dev/mmcblk0boot0
            echo 0 > /sys/block/mmcblk0boot1/force_ro
            dd if=/tmp/boot1.bin of=/dev/mmcblk0boot1 bs=512 2>/dev/null; sync
            echo 1 > /sys/block/mmcblk0boot1/force_ro
            echo 3 > /proc/sys/vm/drop_caches
            verify /tmp/boot1.bin /dev/mmcblk0boot1
            dd if=/tmp/gpt.bin of=/dev/mmcblk0 bs=512 2>/dev/null; sync
            echo 3 > /proc/sys/vm/drop_caches
            verify /tmp/gpt.bin /dev/mmcblk0
            dd if=/tmp/p8.bin of=$P8 bs=512 2>/dev/null; sync
            echo 3 > /proc/sys/vm/drop_caches
            verify /tmp/p8.bin $P8
            dd if=/tmp/p9.bin of=$P9 bs=512 2>/dev/null; sync
            echo 3 > /proc/sys/vm/drop_caches
            verify /tmp/p9.bin $P9
            total=$(cat /sys/block/mmcblk0/size)
            skip=$(( total - 2048 ))
            dd if=/tmp/gpt-backup.bin of=/dev/mmcblk0 bs=512 seek=$skip 2>/dev/null; sync
            echo 3 > /proc/sys/vm/drop_caches
            verify_at /tmp/gpt-backup.bin $skip /dev/mmcblk0
            n=$(dmesg | grep -iE "msdc|mmc0" | grep -viE "cmd=(52|8|5|55) " | grep -ciE "error|timeout|crc" || true); echo "EMMC-ERRORS $n"
            echo RESTORE-OK
            """
        )
        res = self.ssh_run_file(sh, "")
        for line in res:
            self.log(f"  {line}")
        txt = "\n".join(res)
        if "RESTORE-OK" not in txt:
            raise RuntimeError("eMMC restore failed (see above)")

    def _restore_spi_from_uboot(self, bdir: pathlib.Path) -> None:
        mtd_files = {
            0: "nor-mtd0-EEPROM.bin",
            1: "nor-mtd1-u-boot-env.bin",
            2: "nor-mtd2-rest-0x90000.bin",
        }
        o = self.ub("sf probe", 10)
        if "16 MiB" not in o:
            raise RuntimeError("SPI flash is not the expected 16 MB chip")
        for mtd in self.mtd_layout:
            idx = mtd["index"]
            if idx not in mtd_files:
                continue
            fname = mtd_files[idx]
            fpath = bdir / fname
            if not fpath.exists():
                self.warn(f"Backup file {fname} not found, skipping mtd{idx}")
                continue
            size = fpath.stat().st_size
            offset = mtd["offset"]
            hex_size = f"0x{size:x}"
            hex_offset = f"0x{offset:x}"
            self.log(f"  restoring {fname} to SPI offset {hex_offset} ({size:,} bytes)")
            written = False
            for attempt in range(1, 3):
                self.push_to_uboot(fpath)
                self.log(f"  writing to SPI flash at {hex_offset}")
                o = self.ub(f"sf update 0x46000000 {hex_offset} {hex_size}", 400)
                m = re.search(r"(\d+) bytes written, (\d+) bytes skipped", o)
                if not m or (int(m.group(1)) + int(m.group(2))) != size:
                    self.warn("SPI flash write did not report completion")
                    continue
                if int(m.group(2)) == size:
                    self.log("  SPI flash already held this exact data")
                self.ub(f"mw.b 0x4A000000 0x00 {hex_size}", 30)
                self.ub(f"sf read 0x4A000000 {hex_offset} {hex_size}", 120)
                o = self.ub(f"cmp.b 0x46000000 0x4A000000 {hex_size}", 120)
                if re.search(rf"Total of {size} byte\(s\) were the same", o):
                    written = True
                    self.log("  SPI flash read-back matches")
                    break
                else:
                    self.warn("SPI flash read-back differs; writing again")
            if not written:
                raise RuntimeError(f"SPI flash restore of {fname} could not be verified")
        self.ub("setenv bootcmd_real")
        o = self.ub("saveenv", 30)
        if "OK" not in o:
            raise RuntimeError("saveenv failed when clearing bootcmd_real")
        self.log("  bootcmd_real cleared")

    def _do_restore(self, mac: str) -> None:
        bdir = pathlib.Path(self.backup_dir)
        if not bdir.is_absolute():
            bdir = self.root / bdir
        self._validate_backup(bdir)
        m = re.match(r"^([0-9A-F]{12})-", bdir.name)
        if m:
            backup_mac = m.group(1)
            if backup_mac != mac:
                self.warn(f"Backup MAC {backup_mac} does not match unit MAC {mac}")
        self.stage(4, "Restore eMMC from backup")
        self.mtd_layout = self._get_mtd_layout()
        self._restore_emmc_from_backup(bdir)
        self.stage(5, "Restore SPI flash from backup")
        self.ser_read()
        self.ssh_run("reboot")
        caught = False
        for attempt in range(1, 3):
            try:
                self.catch_uboot(120)
                caught = True
                self.log("  U-Boot reached from restored eMMC")
                break
            except Exception as e:
                self.warn(f"Missed U-Boot after reboot ({e})")
                if attempt < 2:
                    self.prompt("UNPLUG THE PoE, WAIT 5 SECONDS, THEN PLUG IT BACK IN")
        if not caught:
            self.warn("U-Boot did not come up from restored eMMC; reaching it over the serial line")
            self.reach_uboot()
        time.sleep(5)
        self._restore_spi_from_uboot(bdir)
        self.stage(6, "Verify restored boot behavior")
        self.ser_read()
        mark = len(self._console_buf)
        self._port.write(b"reset\r")
        o = self.ser_wait(r"BL2:|U-Boot 20|MSDC:|Card did not respond", 120)
        if o is None:
            raise RuntimeError("No boot activity seen after restore")
        time.sleep(25)
        boot = self._console_buf[mark:].decode("utf-8", errors="replace")
        if "bootcmd_real" in boot and "sf read" in boot:
            raise RuntimeError("U-Boot still uses the SPI flash bootcmd_real")
        if "bytes @ 0x100000 Read: OK" in boot:
            raise RuntimeError("U-Boot still loaded kernel from SPI flash")
        has_login = "login:" in boot or "Please press Enter to activate this console" in boot
        has_stock_error = bool(re.search(r"Failed to load image|FIP boot source initialization failed|MSDC:.*CRC error|Card did not respond", boot))
        if has_login:
            self.log("  UniFi booted successfully from eMMC (stock behavior restored)")
        elif has_stock_error:
            self.log("  Unit shows original eMMC failure (stock boot loader restored)")
            self.warn("The unit is back to its original bricked state")
        else:
            raise RuntimeError("Unexpected boot behavior after restore")
        self.emit(f"##STAGE {self.stage_no} OK")
        self.ser_close()
        self.stop_loader()
        self.log("")
        self.log("SUCCESS: unit restored to pre-fix state. Disconnect the serial adapter.", Colors.GREEN)
        self.log(f"Logs: {self.run_dir}", Colors.GREEN)
        self.emit("##RESULT SUCCESS")

    # ------------------------------------------------------------------ platform helpers
    @staticmethod
    def _find_serial_ports() -> List[str]:
        ports = []
        for prefix in ("/dev/ttyUSB", "/dev/ttyACM"):
            for i in range(10):
                p = f"{prefix}{i}"
                if os.path.exists(p):
                    ports.append(p)
        return ports

    @staticmethod
    def _find_wired_adapters() -> List[str]:
        adapters = []
        net_path = pathlib.Path("/sys/class/net")
        if not net_path.exists():
            return adapters
        for iface in net_path.iterdir():
            if iface.name == "lo":
                continue
            # Check if it's a physical device
            if not (iface / "device").exists():
                continue
            # Check if it's 802.3 (Ethernet)
            type_file = iface / "type"
            if type_file.exists():
                try:
                    iface_type = int(type_file.read_text().strip())
                    if iface_type == 1:  # ARPHRD_ETHER
                        adapters.append(iface.name)
                except ValueError:
                    pass
        return adapters

    @staticmethod
    def _adapter_exists(name: str) -> bool:
        return (pathlib.Path("/sys/class/net") / name).exists()

    @staticmethod
    def _adapter_state(name: str) -> str:
        operstate = pathlib.Path("/sys/class/net") / name / "operstate"
        if operstate.exists():
            return operstate.read_text().strip().lower()
        return "unknown"

    def _get_gateway(self) -> Optional[str]:
        try:
            result = subprocess.run(
                ["ip", "route", "show", "dev", self.adapter],
                capture_output=True,
                text=True,
                check=False,
            )
            for line in result.stdout.splitlines():
                if line.startswith("default"):
                    parts = line.split()
                    for i, part in enumerate(parts):
                        if part == "via" and i + 1 < len(parts):
                            return parts[i + 1]
        except FileNotFoundError:
            pass
        return None


# --------------------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(
        description="U6+ eMMC recovery engine for Linux",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            Examples:
              python3 u6plus_fixer.py --mode Fix --com-port /dev/ttyUSB0 --adapter eth0
              python3 u6plus_fixer.py --mode Reset --skip-backup
              python3 u6plus_fixer.py --mode Fix --speed slow
              python3 u6plus_fixer.py --mode Fix --repair-fip
              python3 u6plus_fixer.py --mode RepairFIP --com-port /dev/ttyUSB0 --adapter eth0
              python3 u6plus_fixer.py --mode Restore --backup-dir backups/AABBCCDDEEFF-20240101-120000
            """
        ),
    )
    ap.add_argument("--mode", choices=["Fix", "Reset", "Restore", "RepairFIP"], default="Fix", help="Recovery mode (default: Fix)")
    ap.add_argument("--com-port", default=None, help="Serial port (e.g. /dev/ttyUSB0)")
    ap.add_argument("--adapter", default=None, help="Network adapter (e.g. eth0)")
    ap.add_argument("--factory-reset", action="store_true", help="Also wipe UniFi settings (Fix mode)")
    ap.add_argument("--skip-backup", action="store_true", help="Skip backup (not recommended)")
    ap.add_argument("--speed", choices=["auto", "fast", "slow"], default="auto", help="eMMC speed (default: auto)")
    ap.add_argument("--backup-dir", default=None, help="Backup directory for Restore mode (e.g. backups/MAC-YYYYMMDD-HHMMSS)")
    ap.add_argument("--repair-fip", action="store_true", help="Force rewriting the eMMC U-Boot partition with the stock FIP even if the header looks valid (Fix mode)")
    ap.add_argument("--gui", action="store_true", help="Emit machine-readable progress lines")
    ap.add_argument("--test-soft-reboot", action="store_true", help="Test the software reboot path to slow profile")
    args = ap.parse_args()

    fixer = U6PlusFixer(args)
    fixer.run()


if __name__ == "__main__":
    main()
