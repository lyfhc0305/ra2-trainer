"""Version-gated patches with ownership and rollback."""
from .addresses import PATCHES


class PatchManager:
    def __init__(self, proc):
        self.proc = proc
        self.owned = {}

    def compatible(self):
        return self.proc.is32 and all(
            self.proc.read(va, len(off) // 2) in (bytes.fromhex(off), bytes.fromhex(on))
            for spec in PATCHES.values() for va, off, on in spec["sites"])

    def enable(self, fid, adopt=False):
        """adopt=True also takes ownership of sites already patched.

        Used only on attach for switches the saved state says were on, so a
        trainer that exited abnormally can still switch its own bytes off.
        Otherwise bytes already patched belong to someone else and are left alone.
        """
        if not self.compatible():
            raise RuntimeError("游戏版本或补丁字节不匹配，已停止写入")
        changed = []
        try:
            for va, off, on in PATCHES[fid]["sites"]:
                before, after = bytes.fromhex(off), bytes.fromhex(on)
                current = self.proc.read(va, len(before))
                if current == after:
                    if adopt and va not in self.owned:
                        self.owned[va] = (fid, before, after)
                    continue
                if current != before:
                    raise RuntimeError("补丁地址已被其他程序修改")
                # Game threads are paused so none executes a half-written
                # instruction; a failed write is rolled back by patch_quiescent.
                self.proc.patch_quiescent(va, before, after)
                changed.append((va, before, after))
            for va, before, after in changed:
                self.owned[va] = (fid, before, after)
        except Exception:
            for va, before, after in reversed(changed):
                try:
                    self.proc.patch_quiescent(va, after, before)
                except Exception:
                    self.owned[va] = (fid, before, after)  # retain ownership
            raise

    def disable(self, fid):
        """Restore every site of fid; keep going past failures, then report the first.

        Sites still holding the patched bytes but never adopted belong to
        someone else and are left alone — but reported, so switching the
        feature off cannot quietly leave the patch in effect.
        """
        error = None
        covered = set()
        for va, (owner, before, after) in list(self.owned.items()):
            if owner != fid:
                continue
            covered.add(va)
            try:
                current = self.proc.read(va, len(after))
                if current == after:
                    self.proc.patch_quiescent(va, after, before)
                elif current not in (before, None):
                    raise RuntimeError("补丁字节发生外部变化，未覆盖")
                del self.owned[va]
            except Exception as exc:
                error = error or exc
        for va, _off, on in PATCHES[fid]["sites"]:
            if va in covered:
                continue
            try:
                current = self.proc.read(va, len(on) // 2)
            except Exception:
                continue
            if current == bytes.fromhex(on):
                error = error or RuntimeError(f"{PATCHES[fid]['name']}：残留补丁字节非本修改器写入，未还原")
                break
        if error:
            raise error

    def close(self):
        error = None
        for fid in sorted({owner for owner, _, _ in self.owned.values()}):
            try:
                self.disable(fid)
            except Exception as exc:
                error = error or exc
        if error:
            raise error
