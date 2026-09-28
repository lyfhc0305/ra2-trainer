"""Batched clone placement on a CPU emulator with a fake map (needs unicorn)."""
import struct
import unittest

try:
    from unicorn import Uc, UC_ARCH_X86, UC_MODE_32, UC_HOOK_CODE
    from unicorn.x86_const import (UC_X86_REG_EAX, UC_X86_REG_EBP, UC_X86_REG_EBX, UC_X86_REG_ECX,
                                   UC_X86_REG_EDI, UC_X86_REG_EDX, UC_X86_REG_EIP, UC_X86_REG_ESI,
                                   UC_X86_REG_ESP)
    HAVE_UNICORN = True
except ImportError:
    HAVE_UNICORN = False

import test_core  # noqa: F401  (sets up the import path)
from test_core import needs
from trainer import operations as O

def run(kinds, anchor_cell=(20,20), blocked=set(), house=0x20000000, zone=lambda cell: 1):
    """kinds: list of 'inf'/'veh' per selected unit. Returns placements list."""
    mu = Uc(UC_ARCH_X86,UC_MODE_32)
    for a,s in ((0x400000,0x800000),(0x10000000,0x100000),(0x30000000,0x1000000),(0x40000000,0x1000000),(0x7F000000,0x100000)):
        mu.mem_map(a,s)
    BLOCK=0x10000000
    types=[]; TYPEBASE=0x40000000; TVT=0x30001000; OVT=0x30002000
    for i,k in enumerate(kinds):
        t=TYPEBASE+0x100+i*0x600; mu.mem_write(t,struct.pack('<II',TVT,1 if k=='inf' else 0)); mu.mem_write(t+0x500,struct.pack('<I',2 if k=='inf' else 1)); types.append(t)
    FN={'create':0x30003000,'destroy':0x30003010,'can':0x30003020,'unlimbo':0x30003030}
    mu.mem_write(TVT+0x8c,struct.pack('<I',FN['create']))
    mu.mem_write(OVT+0x20,struct.pack('<I',FN['destroy'])); mu.mem_write(OVT+0x194,struct.pack('<I',FN['can'])); mu.mem_write(OVT+0xd4,struct.pack('<I',FN['unlimbo']))
    for a in list(FN.values())+[0x548070,0x55A740,0x4706C0,O.GET_ZONE]: mu.mem_write(a,b'\xc3')
    state={'next':0x30100000,'cells':{},'objs':{},'destroyed':0,'placed':[]}
    def cellptr(x,y): return 0x30800000+((y&0xff)*256+(x&0xff))*0x10
    def cellxy(ptr): i=(ptr-0x30800000)//0x10; return (i%256,i//256)
    def ret(uc,val,argbytes):
        esp=uc.reg_read(UC_X86_REG_ESP); r=struct.unpack('<I',uc.mem_read(esp,4))[0]
        uc.reg_write(UC_X86_REG_EAX,val); uc.reg_write(UC_X86_REG_ESP,esp+4+argbytes); uc.reg_write(UC_X86_REG_EIP,r)
        # clobber volatile regs like real code
        uc.reg_write(UC_X86_REG_ECX,0xBAD1); uc.reg_write(UC_X86_REG_EDX,0xBAD2)
    def arg(uc,i): esp=uc.reg_read(UC_X86_REG_ESP); return struct.unpack('<I',uc.mem_read(esp+4+4*i,4))[0]
    def ok_for(kind,cell):
        occ=state['cells'].get(cell,[])
        if cell in blocked: return False
        if kind=='veh': return not occ
        return all(k=='inf' for k in occ) and len(occ)<3
    def hook(uc,addr,size,d):
        if addr==FN['create']:
            t=uc.reg_read(UC_X86_REG_ECX); kind=struct.unpack('<I',uc.mem_read(t+4,4))[0]
            o=state['next']; state['next']+=0x100
            uc.mem_write(o,struct.pack('<II',OVT,kind)); state['objs'][o]='inf' if kind else 'veh'
            assert arg(uc,0)==house; ret(uc,o,4)
        elif addr==0x548070:
            p=arg(uc,0); x,y=struct.unpack('<hh',uc.mem_read(p,4))
            ret(uc,cellptr(x,y) if 0<=x<256 and 0<=y<256 else 0xA6F9F8,4)
        elif addr==0x55A740: ret(uc,1,8)
        elif addr==O.GET_ZONE:
            p=arg(uc,0); x,y=struct.unpack('<hh',uc.mem_read(p,4)); mz=arg(uc,1)
            assert mz in (1,2) and arg(uc,2)==0 and uc.reg_read(UC_X86_REG_ECX)==0x8324E0
            ret(uc,zone((x,y)),12)
        elif addr==0x4706C0: ret(uc,0,4)
        elif addr==FN['can']:
            o=uc.reg_read(UC_X86_REG_ECX); cell=cellxy(arg(uc,0))
            ret(uc,0 if ok_for(state['objs'][o],cell) else 1,20)
        elif addr==FN['unlimbo']:
            o=uc.reg_read(UC_X86_REG_ECX); p=arg(uc,0); x,y,z=struct.unpack('<iii',uc.mem_read(p,12))
            cell=(x>>8,y>>8); state['cells'].setdefault(cell,[]).append(state['objs'][o]); state['placed'].append((state['objs'][o],cell))
            ret(uc,1,8)
        elif addr==FN['destroy']:
            state['destroyed']+=1; ret(uc,0,4)
        elif addr==0x7F000100: uc.emu_stop()
    mu.hook_add(UC_HOOK_CODE,hook)
    anchor=(anchor_cell[0]*256+128,anchor_cell[1]*256+128,0)
    dests=O.GameOperations._clone_destinations(anchor,len(types))
    n,m=len(types),len(dests)
    ta=O.CLONE_CODE; ra=ta+4*n; resa=ra+O.CLONE_RECORD*m
    za=resa+4*n
    code=O.clone_batch_code(house,n,BLOCK+ta,BLOCK+ra,m,BLOCK+resa,BLOCK+za)
    assert len(code)<=O.CLONE_CODE
    data=code.ljust(O.CLONE_CODE,b'\xcc')+struct.pack(f'<{n}I',*types)+b''.join(O.clone_record(x) for x in dests)+bytes(4*n)+struct.pack('<I',O.ZONE_UNKNOWN)*16
    mu.mem_write(BLOCK,data)
    esp=0x7F080000; mu.mem_write(esp,struct.pack('<I',0x7F000100)); mu.mem_write(0x7F000100,b'\x90')
    for r,v in ((UC_X86_REG_ESP,esp),(UC_X86_REG_EBX,0x11),(UC_X86_REG_ESI,0x22),(UC_X86_REG_EDI,0x33),(UC_X86_REG_EBP,0x44)): mu.reg_write(r,v)
    mu.emu_start(BLOCK,0,count=5_000_000)
    res=struct.unpack(f'<{n}I',mu.mem_read(BLOCK+resa,4*n))
    regs_ok=all(mu.reg_read(r)==v for r,v in ((UC_X86_REG_EBX,0x11),(UC_X86_REG_ESI,0x22),(UC_X86_REG_EDI,0x33),(UC_X86_REG_EBP,0x44))) and mu.reg_read(UC_X86_REG_ESP)==esp+4
    return res,state,regs_ok,len(code)



@needs(HAVE_UNICORN, "unicorn not installed")
class ClonePlacementTests(unittest.TestCase):
    def test_infantry_share_cells_in_one_call(self):
        res, st, ok, _ = run(["inf"] * 9)
        cells = [c for _, c in st["placed"]]
        self.assertTrue(all(res) and ok)
        self.assertEqual(len(set(cells)), 3)

    def test_vehicles_one_per_cell(self):
        res, st, ok, _ = run(["veh"] * 5)
        self.assertTrue(all(res) and ok)
        self.assertEqual(len({c for _, c in st["placed"]}), 5)

    def test_large_mixed_batch(self):
        res, _st, ok, _ = run(["veh"] * 60 + ["inf"] * 60)
        self.assertEqual(sum(1 for r in res if r), 120)
        self.assertTrue(ok)

    def test_blocked_and_off_map(self):
        res, st, ok, _ = run(["veh", "inf", "inf"], blocked={(20, 20)})
        self.assertTrue(all(res) and ok and st["destroyed"] >= 1)
        self.assertNotIn((20, 20), {c for _, c in st["placed"]})
        res, st, ok, _ = run(["veh"] * 3, anchor_cell=(300, 300))
        self.assertFalse(any(res))
        self.assertEqual(st["destroyed"], len(st["objs"]))

    def test_cells_outside_the_cursor_zone_are_skipped(self):
        # A cliff pocket / walled patch next to the cursor: enterable, but another zone.
        pocket = {(21, 20), (21, 21), (20, 21), (19, 19)}
        zone = lambda cell: 9 if cell in pocket else 1
        res, st, ok, _ = run(["veh"] * 6 + ["inf"] * 3, zone=zone)
        self.assertTrue(all(res) and ok)
        self.assertFalse(pocket & {c for _, c in st["placed"]})

    def test_occupied_cursor_uses_nearest_success_zone(self):
        # Cursor cell not enterable: the nearest placed clone decides the zone.
        zone = lambda cell: 1 if cell[0] <= 20 else 5
        res, st, ok, _ = run(["veh"] * 4, blocked={(20, 20)}, zone=zone)
        self.assertTrue(all(res) and ok)
        self.assertEqual({zone(c) for _, c in st["placed"]}, {zone(st["placed"][0][1])})


if __name__ == "__main__":
    unittest.main()
