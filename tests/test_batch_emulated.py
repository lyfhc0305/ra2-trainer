"""Batched garrison and ownership commands on a CPU emulator (needs unicorn)."""
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

import test_core  # noqa: F401
from trainer import auto_enter as AE, operations as O
from trainer.addresses import PLAYER_PTR, INFANTRY_VT, BUILDING_VT, HOUSE_VT

def machine():
    mu=Uc(UC_ARCH_X86,UC_MODE_32)
    for a,s in ((0x400000,0x800000),(0x10000000,0x100000),(0x30000000,0x1000000),(0x7F000000,0x100000)): mu.mem_map(a,s)
    return mu
def ret(uc,val,argbytes):
    esp=uc.reg_read(UC_X86_REG_ESP); r=struct.unpack('<I',uc.mem_read(esp,4))[0]
    uc.reg_write(UC_X86_REG_EAX,val); uc.reg_write(UC_X86_REG_ESP,esp+4+argbytes); uc.reg_write(UC_X86_REG_EIP,r)
    uc.reg_write(UC_X86_REG_ECX,0xBAD1); uc.reg_write(UC_X86_REG_EDX,0xBAD2)
def call(mu,block,code_len_ok=True):
    esp=0x7F080000; mu.mem_write(esp,struct.pack('<I',0x7F000100)); mu.mem_write(0x7F000100,b'\x90')
    for r,v in ((UC_X86_REG_ESP,esp),(UC_X86_REG_EBX,0x11),(UC_X86_REG_ESI,0x22),(UC_X86_REG_EDI,0x33),(UC_X86_REG_EBP,0x44)): mu.reg_write(r,v)
    mu.emu_start(block,0,count=2_000_000)
    return all(mu.reg_read(r)==v for r,v in ((UC_X86_REG_EBX,0x11),(UC_X86_REG_ESI,0x22),(UC_X86_REG_EDI,0x33),(UC_X86_REG_EBP,0x44))) and mu.reg_read(UC_X86_REG_ESP)==esp+4



@unittest.skipUnless(HAVE_UNICORN, "unicorn not installed")
class BatchCommandTests(unittest.TestCase):
    def test_garrison_batch(self):
        # ---------- garrison ----------
        PL=0x20000000; mu=machine(); mu.mem_write(PLAYER_PTR,struct.pack('<I',PL))
        CLICK=0x30004000; CANOCC=0x452EB0
        mu.mem_write(INFANTRY_VT+0x320,struct.pack('<I',CLICK)); mu.mem_write(CLICK,b'\xc3'); mu.mem_write(CANOCC,b'\xc3')
        itype=0x30010000; mu.mem_write(itype+0xc08,b'\x01')
        soldiers=[0x30100000+i*0x1000 for i in range(5)]
        for i,s_ in enumerate(soldiers):
            mu.mem_write(s_,struct.pack('<I',INFANTRY_VT)); mu.mem_write(s_+0x10,struct.pack('<I',100+i)); mu.mem_write(s_+0x1b4,struct.pack('<I',PL))
            mu.mem_write(s_+0x6c,struct.pack('<i',100)); mu.mem_write(s_+0x76,b'\x00'); mu.mem_write(s_+0x7d,b'\x01'); mu.mem_write(s_+0x5a8,struct.pack('<I',itype))
        refuse=soldiers[3]  # CanOccupy refuses this one everywhere
        btype2=0x30020000; mu.mem_write(btype2+0x121e,b'\x01'); mu.mem_write(btype2+0x1220,struct.pack('<i',2))
        btype3=0x30021000; mu.mem_write(btype3+0x121e,b'\x01'); mu.mem_write(btype3+0x1220,struct.pack('<i',3))
        B1,B2=0x30200000,0x30201000
        for b,t in ((B1,btype2),(B2,btype3)):
            mu.mem_write(b,struct.pack('<I',BUILDING_VT)); mu.mem_write(b+0x10,struct.pack('<I',b>>12)); mu.mem_write(b+0x6c,struct.pack('<i',500))
            mu.mem_write(b+0x76,b'\x00'); mu.mem_write(b+0x7d,b'\x01'); mu.mem_write(b+0x1b4,struct.pack('<I',PL)); mu.mem_write(b+0x418,struct.pack('<I',t)); mu.mem_write(b+0x564,struct.pack('<i',0))
        orders=[]
        def hook(uc,a,s,d):
            if a==CANOCC:
                esp=uc.reg_read(UC_X86_REG_ESP); inf=struct.unpack('<I',uc.mem_read(esp+4,4))[0]
                ret(uc,0 if inf==refuse else 1,4)
            elif a==CLICK:
                esp=uc.reg_read(UC_X86_REG_ESP); args=struct.unpack('<IIII',uc.mem_read(esp+4,16))
                orders.append((uc.reg_read(UC_X86_REG_ECX),args)); ret(uc,1,16)
            elif a==0x7F000100: uc.emu_stop()
        mu.hook_add(UC_HOOK_CODE,hook)
        # soldier 4 recycled (id mismatch)
        plans=[(soldiers[i],100+i if i!=4 else 999,[(B1,B1>>12),(B2,B2>>12)]) for i in range(5)]
        reservations={B2:1}
        targets=sorted({t for _o,_i,n in plans for t,_ in n}); n=len(plans)
        ca=AE.ENTER_CODE; sa=ca+4*len(targets); ra=sa+AE.SOLDIER*n
        BLOCK=0x10000000
        counter={t:BLOCK+ca+4*i for i,t in enumerate(targets)}
        code=AE.enter_batch_code(PL,n,BLOCK+sa,BLOCK+ra)
        data=code.ljust(ca,b'\xcc')+b''.join(struct.pack('<I',reservations.get(t,0)) for t in targets)
        for obj,uid,near in plans:
            e=struct.pack('<I',len(near))+b''.join(struct.pack('<IIIII',obj,uid,t,tid,counter[t]) for t,tid in near); data+=e.ljust(AE.SOLDIER,b'\0')
        data+=bytes(4*n); mu.mem_write(BLOCK,data)
        ok=call(mu,BLOCK)
        res=struct.unpack(f'<{n}I',mu.mem_read(BLOCK+ra,4*n))
        cnt=[struct.unpack('<I',mu.mem_read(counter[t],4))[0] for t in targets]
        # B1 cap 2: soldiers0,1 ; B2 cap3 with 1 reserved -> soldier2 fills B2 slots 2, soldier3 refused, soldier4 stale
        self.assertTrue(ok)
        self.assertEqual(res, (1, 1, 2, 0, 0))  # capacity, reservations, refusal, recycled
        self.assertEqual(cnt, [2, 2])
        self.assertEqual(len(orders), 3)
        self.assertTrue(all(a[0] == 8 and a[1] == 0 and a[2] in (B1, B2) and a[3] == 0 for _, a in orders))

    def run_transfer(self, records, house_vt=HOUSE_VT):
        """Run transfer_batch's real code on the emulator; returns (count, changed objects, abi ok)."""
        mu=machine(); SETOWN=0x30005000; UVT=0x7ADDF8; HOUSE=0x20000000
        mu.mem_map(HOUSE,0x1000); mu.mem_write(HOUSE,struct.pack('<I',house_vt))
        mu.mem_write(UVT+0x378,struct.pack('<I',SETOWN)); mu.mem_write(SETOWN,b'\xc3')
        for obj,vt,uid,owner,hp in records:
            mu.mem_write(obj,struct.pack('<I',vt)); mu.mem_write(obj+0x10,struct.pack('<I',uid))
            mu.mem_write(obj+0x1b4,struct.pack('<I',owner)); mu.mem_write(obj+0x6c,struct.pack('<i',hp))
        changed=[]
        def hook(uc,a,s,d):
            if a==SETOWN:
                esp=uc.reg_read(UC_X86_REG_ESP); h,f=struct.unpack('<II',uc.mem_read(esp+4,8))
                changed.append(uc.reg_read(UC_X86_REG_ECX)); ret(uc,1,8)
            elif a==0x7F000100: uc.emu_stop()
        mu.hook_add(UC_HOOK_CODE,hook)
        abi=[]
        class FakeProc:
            def read_u32(s,a): return HOUSE_VT if a==HOUSE else (3 if a==FakeExec.base else 0)
            def alloc(s,n): return 0x10000000
            def patch(s,a,d): s.data=d; return True
            def free(s,a): pass
        class FakeExec:
            base=0x1f000000
            def install(s): pass
            def call(s,f,timeout=0):
                mu.mem_write(f,fp.data); abi.append(call(mu,f)); return mu.reg_read(UC_X86_REG_EAX)
        fp=FakeProc(); op=O.GameOperations.__new__(O.GameOperations); op.proc=fp; op.executor=FakeExec()
        return op, HOUSE, changed, abi

    def test_transfer_batch(self):
        UVT=0x7ADDF8; ME=0x20000800
        objs=[0x30100000+i*0x1000 for i in range(6)]
        snapshot=[(o,UVT,100+i,ME) for i,o in enumerate(objs)]
        memory=[(objs[0],UVT,100,ME,50),
                (objs[1],UVT,101,ME,50),
                (objs[2],0x12345678,102,ME,50),  # replaced by another class
                (objs[3],UVT,999,ME,50),         # same class, same address, new object
                (objs[4],UVT,104,0x20000900,50), # captured since the click
                (objs[5],UVT,105,ME,0)]          # dead
        op,house,changed,abi=self.run_transfer(memory)
        self.assertEqual(op.transfer_batch(snapshot,house), 2)
        self.assertEqual(abi, [True])
        self.assertEqual(changed, objs[:2])

    def test_transfer_batch_rechecks_target_house_in_game(self):
        UVT=0x7ADDF8; obj=0x30100000
        op,house,changed,abi=self.run_transfer([(obj,UVT,1,0x20000800,50)], house_vt=0)
        with self.assertRaises(RuntimeError):
            op.transfer_batch([(obj,UVT,1,0x20000800)],house)
        self.assertEqual(abi, [True])
        self.assertEqual(changed, [])


okT = False

if __name__ == "__main__":
    unittest.main()
