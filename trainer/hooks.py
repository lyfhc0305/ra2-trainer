"""Entry hooks that jump into one allocated block of generated code.

Each site replaces complete instructions with JMP+NOP padding into its stub.
Sites are written only while no thread executes them, are owned per session,
and bytes changed by another program are never overwritten. Detached blocks
stay allocated: a thread may still return through them.
"""
from .executor import relative_jump, release_orphan
from .mem import PatchResidue


def entry_jump(address, target, length):
    return relative_jump(address, target) + b"\x90" * (length - 5)


def assemble(size, placements, label):
    """Place (offset, code) pairs into a zeroed block; reject overlaps."""
    blob = bytearray(size)
    ordered = sorted(placements)
    for i, (offset, code) in enumerate(ordered):
        limit = ordered[i + 1][0] if i + 1 < len(ordered) else size
        if offset + len(code) > limit:
            raise ValueError(f"{label}补丁过长")
        blob[offset:offset + len(code)] = code
    return bytes(blob)


class HookSites:
    """sites = {name: (address, original bytes, stub offset)}; build(base) -> block."""

    def __init__(self, proc, label, sites, build, size=0x1000, release=True):
        self.proc, self.label = proc, label
        self.sites, self.build, self.size = sites, build, size
        self.release = release  # False: the controller adopts its old block instead
        self.base = None
        self.installed = {}

    def replacement(self, name, base=None):
        address, original, offset = self.sites[name]
        return entry_jump(address, (self.base if base is None else base) + offset, len(original))

    def _block(self, base):
        return bytes(self.build(base)).ljust(self.size, b"\0")

    def _stub(self, name, base):
        """This site's region of the block, up to the next site's stub."""
        offset = self.sites[name][2]
        end = min([o for _a, _b, o in self.sites.values() if o > offset] + [self.size])
        return self._block(base)[offset:end]

    def prepare(self):
        if self.base is not None:
            return
        if self.release:
            for name, (address, original, offset) in self.sites.items():
                release_orphan(self.proc, address, original,
                               lambda target, n=name, o=offset: self._stub(n, target - o))
        for address, original, _offset in self.sites.values():
            if self.proc.read(address, len(original)) != original:
                raise RuntimeError(f"{self.label}代码与游戏版本不匹配")
        base = self.proc.alloc(self.size)
        if not base:
            raise OSError(f"无法分配{self.label}补丁")
        try:
            if not self.proc.patch(base, self._block(base)):
                raise OSError(f"{self.label}补丁写入失败")
        except Exception:
            self.proc.free(base)  # never reachable: no entry points at it yet
            raise
        self.base = base

    def locate(self, magic):
        """Base of a block an earlier session left, found through the first site's jump."""
        address, original, offset = next(iter(self.sites.values()))
        current = self.proc.read(address, len(original))
        if not current or current == original or current[0] != 0xE9:
            return None
        target = (address + 5 + int.from_bytes(current[1:5], "little", signed=True)) & 0xFFFFFFFF
        base = target - offset
        return base if self.proc.read(base, len(magic)) == magic else None

    def adopt(self, base, names=None):
        """Take ownership of a complete, byte-verified set of old entry jumps."""
        names = list(self.sites) if names is None else list(names)
        for name in names:
            address, original, _offset = self.sites[name]
            if self.proc.read(address, len(original)) != self.replacement(name, base):
                raise RuntimeError(f"{self.label}旧补丁不完整，未接管")
        self.base = base
        self.installed = {name: self.replacement(name, base) for name in names}

    def switch(self, name, on):
        """Install or remove one site; returns True when this call changed it."""
        address, original, _offset = self.sites[name]
        current = self.proc.read(address, len(original))
        installed = self.installed.get(name)
        if on:
            if installed and current == installed:
                return False
            if current != original:
                raise RuntimeError(f"{self.label}入口已被外部修改")
            replacement = self.replacement(name)
            try:
                self.proc.patch_quiescent(address, original, replacement)
            except PatchResidue as exc:
                if exc.current == replacement:
                    self.installed[name] = replacement  # live: keep it owned so it can be removed
                raise
            self.installed[name] = replacement
            return True
        if not installed:
            return False
        if current == installed:
            self.proc.patch_quiescent(address, installed, original)
        elif current not in (None, original):
            raise RuntimeError(f"{self.label}入口已被外部修改，未覆盖")
        del self.installed[name]
        return True

    def apply(self, wanted, after=None):
        """Install exactly the wanted sites, then run after(); all or nothing."""
        wanted = set(wanted)
        if wanted:
            self.prepare()
        before = set(self.installed)
        try:
            for name in self.sites:
                self.switch(name, name in wanted)
            if after:
                after()
        except Exception as exc:
            # Roll back every site whose ownership changed, including one a
            # failed write left fully installed; report whatever stays behind.
            residue = []
            for name in reversed(list(self.sites)):
                if (name in self.installed) == (name in before):
                    continue
                try:
                    self.switch(name, name in before)
                except Exception:
                    residue.append(name)
            if residue:
                raise RuntimeError(
                    f"{exc}；回滚未完成，{self.label}仍残留入口：{'、'.join(residue)}") from exc
            raise

    def close(self):
        """Remove every owned site; keep going past failures, then report the first."""
        error = None
        for name in reversed(list(self.installed)):
            try:
                self.switch(name, False)
            except Exception as exc:
                error = error or exc
        if error:
            raise error
