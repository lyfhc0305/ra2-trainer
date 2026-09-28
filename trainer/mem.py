"""Windows 32-bit process memory access + code patching for RA2 (game.exe)."""
import ctypes
import ctypes.wintypes as wt
import struct
import sys

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)

# Explicit pointer-sized signatures are required when Python is 64-bit.
def _bind(dll, name, result, *args):
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = result, args


P, D, H, S, B = ctypes.c_void_p, wt.DWORD, wt.HANDLE, ctypes.c_size_t, wt.BOOL
for name, result, args in [
    ("OpenProcess", H, (D, B, D)), ("CloseHandle", B, (H,)),
    ("IsWow64Process", B, (H, ctypes.POINTER(B))),
    ("ReadProcessMemory", B, (H, P, P, S, ctypes.POINTER(S))),
    ("WriteProcessMemory", B, (H, P, P, S, ctypes.POINTER(S))),
    ("VirtualAllocEx", P, (H, P, S, D, D)),
    ("VirtualFreeEx", B, (H, P, S, D)),
    ("VirtualProtectEx", B, (H, P, S, D, ctypes.POINTER(D))),
    ("VirtualQueryEx", S, (H, P, P, S)),
    ("FlushInstructionCache", B, (H, P, S)),
    ("CreateToolhelp32Snapshot", H, (D, D)),
    ("Process32First", B, (H, P)), ("Process32Next", B, (H, P)),
    ("Thread32First", B, (H, P)), ("Thread32Next", B, (H, P)),
    ("OpenThread", H, (D, B, D)), ("SuspendThread", D, (H,)),
    ("ResumeThread", D, (H,)),
    ("Wow64GetThreadContext", B, (H, P)),
    ("GetThreadContext", B, (H, P)),
    ("CreateRemoteThread", H, (H, P, S, P, P, D, ctypes.POINTER(D))),
    ("WaitForSingleObject", D, (H, D)),
    ("GetExitCodeThread", B, (H, ctypes.POINTER(D))),
    ("GetExitCodeProcess", B, (H, ctypes.POINTER(D))),
]:
    _bind(kernel32, name, result, *args)
_bind(psapi, "EnumProcessModulesEx", B, H, P, D, ctypes.POINTER(D), D)
_bind(psapi, "GetModuleBaseNameW", D, H, H, wt.LPWSTR, D)

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def build_call_stub(func, this=0, args=()):
    """x86 WINAPI thread entry; preserve nonvolatile registers and clean its argument."""
    values = [func, this, *args]
    if any(not isinstance(v, int) or not 0 <= v <= 0xFFFFFFFF for v in values):
        raise ValueError("调用参数必须是32位无符号整数")
    if len(args) > 32:
        raise ValueError("最多支持32个参数")
    code = bytearray(b"\x55\x89\xe5\x53\x56\x57\x83\xe4\xf0")
    code += b"\x81\xec\x00\x04\x00\x00"
    pad = (-4 * len(args)) % 16
    if pad:
        code += b"\x83\xec" + bytes([pad])
    for value in reversed(args):
        code += b"\x68" + struct.pack("<I", value)
    code += b"\xb9" + struct.pack("<I", this)
    code += b"\xb8" + struct.pack("<I", func) + b"\xff\xd0"
    code += b"\x8d\x65\xf4\x5f\x5e\x5b\x5d\xc2\x04\x00"
    return bytes(code)

PROCESS_ALL_ACCESS = 0x1F0FFF
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
    ]


class Process:
    """Attach to a 32-bit game process and provide R/W/patch helpers."""

    def __init__(self, name_or_pid, module_base=0x400000):
        self.pid = self._resolve(name_or_pid)
        self.module_base = module_base
        self.h = kernel32.OpenProcess(
            PROCESS_ALL_ACCESS, False, self.pid
        )
        if not self.h:
            raise OSError(f"OpenProcess failed (err={ctypes.get_last_error()})")
        self.is32 = self._target_is_32bit()
        self.ptr_size = 4 if self.is32 else 8
        self.max_addr = 0xFFFFFFFF if self.is32 else 0x7FFFFFFFFFFF
        self._pending_calls = []

    def _target_is_32bit(self):
        """True if the target process is 32-bit."""
        if sys.maxsize <= 2 ** 32:      # we are 32-bit -> target can only be 32-bit
            return True
        try:
            wow = wt.BOOL(False)
            if kernel32.IsWow64Process(self.h, ctypes.byref(wow)):
                return bool(wow.value)   # WOW64 == 32-bit process on 64-bit OS
        except Exception:
            pass
        return False

    # ---------- process discovery ----------
    @staticmethod
    def find_pids(names):
        """Return list of (pid, exe_name). Uses Toolhelp snapshot (fast, no subprocess)."""
        if isinstance(names, str):
            names = [names]
        names = {n.lower() for n in names}
        try:
            return Process._find_pids_snapshot(names)
        except Exception:
            pass
        return Process._find_pids_tasklist(names)

    @staticmethod
    def _find_pids_snapshot(names):
        TH32CS_SNAPPROCESS = 0x2
        MAX_PATH = 260

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wt.DWORD),
                ("cntUsage", wt.DWORD),
                ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", wt.DWORD),
                ("cntThreads", wt.DWORD),
                ("th32ParentProcessID", wt.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wt.DWORD),
                ("szExeFile", ctypes.c_char * MAX_PATH),
            ]

        out = []
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == INVALID_HANDLE_VALUE or not snap:
            raise OSError("snapshot failed")
        try:
            pe = PROCESSENTRY32()
            pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
            if kernel32.Process32First(snap, ctypes.byref(pe)):
                while True:
                    name = pe.szExeFile.decode("latin1")
                    if name.lower() in names:
                        out.append((pe.th32ProcessID, name))
                    if not kernel32.Process32Next(snap, ctypes.byref(pe)):
                        break
        finally:
            kernel32.CloseHandle(snap)
        return out

    @staticmethod
    def _find_pids_tasklist(names):
        import subprocess
        out = []
        try:
            raw = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True, timeout=15,
            ).stdout.decode("gbk", "ignore")
            for line in raw.splitlines():
                parts = [p.strip('"') for p in line.split('","')]
                if len(parts) >= 2 and parts[0].lower() in names:
                    out.append((int(parts[1]), parts[0]))
        except Exception:
            pass
        return out

    @classmethod
    def wait_for(cls, names, timeout=0.0):
        import time
        t0 = time.time()
        while True:
            pids = cls.find_pids(names)
            if pids:
                return pids[0]
            if timeout and time.time() - t0 > timeout:
                return None
            time.sleep(0.5)

    def _resolve(self, spec):
        if isinstance(spec, int):
            return spec
        pids = self.find_pids(spec)
        if not pids:
            raise ProcessLookupError(f"process not found: {spec}")
        return pids[0][0]

    # ---------- raw read/write ----------
    def alive(self):
        """True/False when the process state is known, None when it can't be queried.

        A failed ReadProcessMemory alone does not prove the process exited."""
        code = D()
        if not self.h or not kernel32.GetExitCodeProcess(self.h, ctypes.byref(code)):
            return None
        return code.value == 259  # STILL_ACTIVE

    def read(self, addr, size):
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t(0)
        ok = kernel32.ReadProcessMemory(
            self.h, ctypes.c_void_p(addr), buf, size, ctypes.byref(got)
        )
        if not ok or got.value != size:
            return None
        return buf.raw

    def write(self, addr, data):
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        got = ctypes.c_size_t(0)
        ok = kernel32.WriteProcessMemory(
            self.h, ctypes.c_void_p(addr), buf, len(data), ctypes.byref(got)
        )
        return bool(ok) and got.value == len(data)

    # ---------- typed helpers (writes temporarily lift page protection) ----------
    def read_u32(self, addr):
        b = self.read(addr, 4)
        return None if b is None else struct.unpack("<I", b)[0]

    def read_i32(self, addr):
        b = self.read(addr, 4)
        return None if b is None else struct.unpack("<i", b)[0]

    def read_u8(self, addr):
        b = self.read(addr, 1)
        return None if b is None else b[0]

    def read_f32(self, addr):
        b = self.read(addr, 4)
        return None if b is None else struct.unpack("<f", b)[0]

    def read_f64(self, addr):
        b = self.read(addr, 8)
        return None if b is None else struct.unpack("<d", b)[0]

    def read_cstr(self, addr, size=64):
        b = self.read(addr, size)
        if b is None:
            return None
        return b.split(b"\0")[0].decode("latin1")

    def write_i32(self, addr, val):
        return self.write(addr, struct.pack("<i", int(val)))

    def write_u8(self, addr, val):
        return self.write(addr, struct.pack("<B", int(val)))

    def write_u32(self, addr, val):
        return self.write(addr, struct.pack("<I", int(val)))

    def write_f32(self, addr, val):
        return self.write(addr, struct.pack("<f", float(val)))

    def write_f64(self, addr, val):
        return self.write(addr, struct.pack("<d", float(val)))

    # ---------- pointer chain ----------
    def resolve_chain(self, chain):
        """chain = [global_addr, off1, off2, ...] -> final *address*。

        首元素解引用得到基址，中间偏移逐层解引用，最后一个偏移只加不解引用。
        例：[0xA35DB4, 0x24C]  ->  *(*0xA35DB4) + 0x24C  = 玩家资金地址
        """
        if not chain:
            return None
        cur = self.read_u32(chain[0])
        if cur is None or not cur:
            return None
        for off in chain[1:-1]:
            cur = self.read_u32(cur + off)
            if cur is None or not cur:
                return None
        if len(chain) > 1:
            cur = (cur + chain[-1]) & 0xFFFFFFFF
        return cur

    def read_value(self, addr, kind="i32"):
        if addr is None:
            return None
        return getattr(self, "read_" + kind)(addr)

    def write_value(self, addr, val, kind="i32"):
        if addr is None:
            return False
        return getattr(self, "write_" + kind)(addr, val)

    # ---------- thread suspend / resume ----------
    def _thread_ids(self):
        TH32CS_SNAPTHREAD = 0x4

        class THREADENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wt.DWORD),
                ("cntUsage", wt.DWORD),
                ("th32ThreadID", wt.DWORD),
                ("th32OwnerProcessID", wt.DWORD),
                ("tpBasePri", ctypes.c_long),
                ("tpDeltaPri", ctypes.c_long),
                ("dwFlags", wt.DWORD),
            ]

        out = []
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
        if not snap or snap == INVALID_HANDLE_VALUE:
            raise OSError("无法枚举游戏线程")
        try:
            te = THREADENTRY32()
            te.dwSize = ctypes.sizeof(THREADENTRY32)
            if kernel32.Thread32First(snap, ctypes.byref(te)):
                while True:
                    if te.th32OwnerProcessID == self.pid:
                        out.append(te.th32ThreadID)
                    if not kernel32.Thread32Next(snap, ctypes.byref(te)):
                        break
        finally:
            kernel32.CloseHandle(snap)
        return out

    def suspend_all(self):
        """挂起目标进程所有线程，返回句柄列表（供 resume_all 使用）。"""
        handles = []
        try:
            for tid in self._thread_ids():
                h = kernel32.OpenThread(0x000A, False, tid)
                if not h:
                    raise OSError("无法打开游戏线程")
                if kernel32.SuspendThread(h) == 0xFFFFFFFF:
                    kernel32.CloseHandle(h)
                    raise OSError("无法暂停游戏线程")
                handles.append(h)
        except Exception:
            self.resume_all(handles)
            raise
        return handles

    def resume_all(self, handles):
        for h in handles:
            kernel32.ResumeThread(h)
            kernel32.CloseHandle(h)

    @staticmethod
    def _eips(held):
        eips = []
        for thread in held:
            # WOW64_CONTEXT layout: EIP at 184, CONTEXT_CONTROL = 0x10001.
            context = ctypes.create_string_buffer(716)
            struct.pack_into("<I", context, 0, 0x10001)
            get_context = (kernel32.Wow64GetThreadContext if sys.maxsize > 2**32
                           else kernel32.GetThreadContext)
            if not get_context(thread, context):
                raise OSError("读取暂停线程状态失败")
            eips.append(struct.unpack_from("<I", context, 184)[0])
        return eips

    def code_idle(self, addr, size):
        """True when no thread is executing in [addr, addr+size).

        Only meaningful for detached code without calls: once no EIP is inside
        and its entry jump is gone, nothing can reach it again.
        """
        held = self.suspend_all()
        try:
            return bool(held) and not any(addr <= eip < addr + size for eip in self._eips(held))
        finally:
            self.resume_all(held)

    def patch_quiescent(self, addr, expected, replacement):
        """Replace complete instructions only when no suspended EIP is inside them."""
        import time
        if not self.is32 or len(expected) != len(replacement):
            raise ValueError("钩子补丁要求32位目标和相同字节长度")
        for _ in range(20):
            held = self.suspend_all()
            try:
                if not held or self.read(addr, len(expected)) != expected:
                    raise RuntimeError("钩子代码已改变或进程已退出")
                busy = any(addr < eip < addr + len(expected) for eip in self._eips(held))
                if not busy:
                    if not self.patch(addr, replacement) or self.read(addr, len(expected)) != replacement:
                        self.patch(addr, expected)
                        raise OSError("钩子写入校验失败")
                    return
            finally:
                self.resume_all(held)
            time.sleep(0.005)
        raise RuntimeError("线程正在执行钩子位置，请稍后重试")

    # ---------- remote function call (32-bit target) ----------
    def alloc(self, size, protect=0x40):
        addr = kernel32.VirtualAllocEx(
            self.h, None, ctypes.c_size_t(size), 0x3000, protect  # COMMIT|RESERVE
        )
        return addr or None

    def free(self, addr):
        if addr:
            kernel32.VirtualFreeEx(self.h, ctypes.c_void_p(addr), 0, 0x8000)

    def reap_calls(self):
        """Only release code after its remote thread has actually finished."""
        pending = []
        for thread, buf in self._pending_calls:
            if kernel32.WaitForSingleObject(thread, 0) == 0:
                self.free(buf)
                kernel32.CloseHandle(thread)
            else:
                pending.append((thread, buf))
        self._pending_calls = pending

    def call(self, func, this=0, args=(), timeout=5000, suspend=False):
        """Call an x86 function. Timeout raises and retains still-running code.

        This executes on a separate thread, not the game simulation thread.
        Do not use it for creating/destroying game objects.
        """
        if not self.is32:
            raise ValueError("远程函数调用仅支持32位目标")
        self.reap_calls()
        if self._pending_calls:
            raise RuntimeError("上次调用尚未结束，不能开始新调用")
        code = build_call_stub(func, this, tuple(args))
        buf = self.alloc(len(code))
        if not buf:
            return None, False
        thread = None
        held = []
        try:
            if not self.write(buf, code):
                return None, False
            if not kernel32.FlushInstructionCache(self.h, buf, len(code)):
                return None, False
            if suspend:
                held = self.suspend_all()
            thread = kernel32.CreateRemoteThread(self.h, None, 0, buf, None, 0, None)
            if not thread:
                return None, False
            wait = kernel32.WaitForSingleObject(thread, timeout)
            if wait != 0:
                self._pending_calls.append((thread, buf))
                thread, buf = None, None
                if wait == 0x102:
                    raise TimeoutError("远程调用超时；执行代码已保留，请勿释放参数内存")
                raise OSError("等待远程线程失败；执行代码已保留")
            rc = wt.DWORD()
            ok = kernel32.GetExitCodeThread(thread, ctypes.byref(rc))
            return (rc.value, True) if ok else (None, False)
        finally:
            if held:
                self.resume_all(held)
            if thread:
                kernel32.CloseHandle(thread)
            if buf:
                self.free(buf)

    # ---------- code patching ----------
    def virtual_protect(self, addr, size, protect):
        old = wt.DWORD(0)
        ok = kernel32.VirtualProtectEx(
            self.h, ctypes.c_void_p(addr), size, protect, ctypes.byref(old)
        )
        return bool(ok), old.value

    def patch(self, addr, data, protect=0x40):  # PAGE_EXECUTE_READWRITE
        ok, old = self.virtual_protect(addr, len(data), protect)
        if not ok:
            return False
        res = self.write(addr, data)
        self.virtual_protect(addr, len(data), old)
        kernel32.FlushInstructionCache(self.h, ctypes.c_void_p(addr), len(data))
        return res

    def read_bytes(self, addr, size):
        return self.read(addr, size)

    # ---------- pointer chains ----------
    def read_ptr_chain(self, addr, offsets):
        """Read pointer at addr, then follow each offset (deref each step)."""
        cur = self.read_u32(addr)
        if cur is None:
            return None
        for i, off in enumerate(offsets):
            last = i == len(offsets) - 1
            if last:
                return cur + off
            cur = self.read_u32(cur + off)
            if cur is None or cur == 0:
                return None
        return cur

    # ---------- module base ----------
    def remote_module_base(self, name="game.exe"):
        """Read the actual load address of a module in the target (no-ASLR: 0x400000)."""
        # EnumProcessModules from 64-bit works for 32-bit targets on WOW64
        arr = (ctypes.c_void_p * 1024)()
        needed = wt.DWORD(0)
        if not psapi.EnumProcessModulesEx(
            self.h, ctypes.byref(arr), ctypes.sizeof(arr),
            ctypes.byref(needed), 0x03,  # LIST_MODULES_ALL
        ):
            return None
        count = needed.value // ctypes.sizeof(ctypes.c_void_p)
        for i in range(count):
            mod = arr[i]
            buf = ctypes.create_unicode_buffer(260)
            if psapi.GetModuleBaseNameW(self.h, ctypes.c_void_p(mod), buf, 260):
                if buf.value.lower() == name.lower():
                    return int(mod)
        return None

    # ---------- memory scanning ----------
    READABLE = (0x02, 0x04, 0x08, 0x20, 0x40, 0x80)  # RO/RW/RC/X/XR/XRC
    WRITABLE = (0x04, 0x08, 0x40, 0x80)

    @classmethod
    def _has_prot(cls, protect, kinds):
        p = protect & 0xFF
        return any(p == k for k in kinds)

    def iter_regions(self, min_addr=0x10000, max_addr=None):
        if max_addr is None:
            max_addr = self.max_addr
        addr = min_addr
        mbi = MEMORY_BASIC_INFORMATION()
        while addr < max_addr:
            r = kernel32.VirtualQueryEx(
                self.h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)
            )
            if not r:
                break
            base = int(mbi.BaseAddress or 0)
            size = int(mbi.RegionSize)
            if size <= 0:
                break
            yield base, size, int(mbi.State), int(mbi.Protect), int(mbi.Type)
            nxt = base + size
            if nxt <= addr:      # guard against wrap-around
                break
            addr = nxt

    def scan_i32(self, value, min_addr=0x10000, max_addr=None):
        """All addresses holding the given int32 (readable committed pages)."""
        pat = struct.pack("<i", int(value))
        out = []
        for base, size, state, protect, typ in self.iter_regions(min_addr, max_addr):
            if state != MEM_COMMIT or protect & (PAGE_GUARD | PAGE_NOACCESS):
                continue
            if not self._has_prot(protect, self.READABLE):
                continue
            if size > 0x8000000:
                continue
            blob = self.read(base, size)
            if not blob:
                continue
            i = blob.find(pat)
            while i >= 0:
                out.append(base + i)
                i = blob.find(pat, i + 1)
        return out

    def find_pointers_to(self, addr, min_addr=None, max_addr=None):
        """Find pointer-sized values equal to addr (candidate global pointers)."""
        pat = struct.pack("<I" if self.ptr_size == 4 else "<Q", addr)
        out = []
        if min_addr is None:
            min_addr = 0x10000
        for base, size, state, protect, typ in self.iter_regions(min_addr, max_addr):
            if state != MEM_COMMIT or protect & (PAGE_GUARD | PAGE_NOACCESS):
                continue
            if not self._has_prot(protect, self.WRITABLE):
                continue
            if size > 0x4000000:
                continue
            blob = self.read(base, size)
            if not blob:
                continue
            i = blob.find(pat)
            while i >= 0:
                out.append(base + i)
                i = blob.find(pat, i + 1)
        return out

    def close(self):
        if self.h:
            self.reap_calls()
            # Unfinished remote code belongs to the target until process exit.
            for thread, _buf in self._pending_calls:
                kernel32.CloseHandle(thread)
            self._pending_calls.clear()
            kernel32.CloseHandle(self.h)
            self.h = None
