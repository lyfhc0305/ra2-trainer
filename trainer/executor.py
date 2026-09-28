"""RA2 1.006 simulation-thread command mailbox.

The hook replaces one complete MOV at the frame-counter update. It preserves
flags, general registers, x87 and SSE state. Only one request may be in flight.
Detached trampoline memory is retained until game exit: a thread may still be
returning through it even after the original instruction has been restored.
"""
import struct
import time

from .mem import build_call_stub

HOOK = 0x540684
ORIGINAL = bytes.fromhex("89152c0da400")


class CommandPending(OSError):
    """A command was (or may have been) submitted and was not seen to finish.

    The game may still run it later, so its code and data must stay allocated."""


class CommandTimeout(CommandPending, TimeoutError):
    pass


class ProcessExited(OSError):
    pass


def relative_jump(source, destination):
    return b"\xe9" + struct.pack("<I", (destination - source - 5) & 0xFFFFFFFF)


def release_orphan(proc, address, original, expected_at):
    """Restore an entry hook left by an earlier trainer run that did not exit cleanly.

    The site must hold JMP+NOP padding, and its target must contain exactly the
    code expected_at(target) would write, so other programs' hooks are never
    touched. The old code block stays allocated: a thread may still return
    through it. Returns True when the original bytes were restored.
    """
    current = proc.read(address, len(original))
    if (not current or current == original or current[0] != 0xE9
            or current[5:] != b"\x90" * (len(original) - 5)):
        return False
    target = (address + 5 + struct.unpack_from("<i", current, 1)[0]) & 0xFFFFFFFF
    try:
        code = expected_at(target)
        if not code or proc.read(target, len(code)) != code:
            return False
        proc.patch_quiescent(address, current, original)
    except Exception:
        return False
    return True


def build_trampoline(base):
    code = bytearray(b"\x9c\x60\x89\xe5\x83\xe4\xf0\x81\xec\x20\x02\x00\x00")
    code += b"\x0f\xae\x04\x24"  # fxsave [esp]
    code += b"\x83\x3d" + struct.pack("<I", base) + b"\x01"
    branch = len(code)
    code += b"\x0f\x85\x00\x00\x00\x00"
    code += b"\xc7\x05" + struct.pack("<II", base, 2)
    code += b"\xb8" + struct.pack("<I", base + 0x100) + b"\xff\xd0"
    code += b"\xa3" + struct.pack("<I", base + 4)
    code += b"\xc7\x05" + struct.pack("<II", base, 3)
    struct.pack_into("<i", code, branch + 2, len(code) - branch - 6)
    code += b"\x0f\xae\x0c\x24\x89\xec\x61\x9d"  # fxrstor / registers / flags
    code += ORIGINAL
    code += relative_jump(base + 0x300 + len(code), HOOK + len(ORIGINAL))
    return bytes(code)


class MainThreadExecutor:
    def __init__(self, proc):
        self.proc = proc
        self.base = None
        self.installed = False

    def install(self):
        if self.installed:
            return
        if not self.proc.is32:
            raise RuntimeError("主循环特征码不匹配，停止安装")
        release_orphan(self.proc, HOOK, ORIGINAL,
                       lambda target: build_trampoline(target - 0x300))
        if self.proc.read(HOOK, len(ORIGINAL)) != ORIGINAL:
            raise RuntimeError("主循环特征码不匹配，停止安装")
        base = self.proc.alloc(0x1000)
        if not base:
            raise OSError("无法分配主线程命令区")
        patch = relative_jump(HOOK, base + 0x300) + b"\x90"
        try:
            if not self.proc.write(base, bytes(0x1000)) or not self.proc.patch(
                    base + 0x300, build_trampoline(base)):
                raise OSError("无法初始化主线程命令区")
            held = self.proc.suspend_all()
            try:
                if not held or self.proc.read(HOOK, len(ORIGINAL)) != ORIGINAL:
                    raise RuntimeError("主循环状态已变化，停止安装")
                if not self.proc.patch(HOOK, patch):
                    self.proc.patch(HOOK, ORIGINAL)
                    raise OSError("主循环补丁写入失败")
            finally:
                self.proc.resume_all(held)
        except Exception:
            # Release the block only when the entry verifiably holds the
            # original bytes; a full or partial jump may still reach it.
            if self.proc.read(HOOK, len(ORIGINAL)) == ORIGINAL:
                self.proc.free(base)
            raise
        self.base, self.patch = base, patch
        self.installed = True

    def call(self, func, this=0, args=(), timeout=5.0):
        self.install()
        state = self.proc.read_u32(self.base)
        if state not in (0, 3):
            raise RuntimeError("上一条主线程命令尚未结束")
        # Same preserved-register wrapper; this is a normal call, not WINAPI.
        code = build_call_stub(func, this, tuple(args))[:-3] + b"\xc3"
        if not self.proc.patch(self.base + 0x100, code):
            raise OSError("主线程命令写入失败")
        # From here on the game may pick the command up: any failure leaves
        # its outcome unknown and the caller must keep the code it points to.
        if not self.proc.write_u32(self.base, 1):
            raise CommandPending("主线程命令提交结果未知；命令与参数已保留")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.proc.read_u32(self.base)
            if state == 3:
                result = self.proc.read_u32(self.base + 4)
                if result is None:
                    raise OSError("主线程命令已完成，但无法读取返回值")
                return result
            if state is None:
                alive = getattr(self.proc, "alive", None)
                if alive is not None and alive() is False:
                    raise ProcessExited("游戏进程已退出")
                raise CommandPending("无法读取主线程命令状态，结果未知；命令与参数已保留")
            time.sleep(0.01)
        raise CommandTimeout("游戏主线程未完成命令（可能暂停）；命令与参数必须保留")

    def close(self):
        if not self.installed:
            return
        if self.proc.read(HOOK, len(ORIGINAL)) is None:
            self.installed = False
            return
        held = self.proc.suspend_all()
        try:
            current = self.proc.read(HOOK, len(ORIGINAL))
            if current == self.patch and not self.proc.patch(HOOK, ORIGINAL):
                raise OSError("主循环补丁恢复失败")
            if current not in (self.patch, ORIGINAL, None):
                raise RuntimeError("主循环补丁被其他程序修改，未覆盖")
            self.installed = False
        finally:
            self.proc.resume_all(held)
