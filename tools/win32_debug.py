"""Minimal x86-on-x64 debugger for owned RBO test processes (no UI input)."""
import ctypes as C
from ctypes import wintypes as W
import struct
import os
import tempfile
from pathlib import Path
import time

K = C.WinDLL('kernel32', use_last_error=True)
D = W.DWORD
P = C.c_void_p

class Startup(C.Structure):
    _fields_ = [('cb',D),('reserved',W.LPWSTR),('desktop',W.LPWSTR),('title',W.LPWSTR)] + [(n,D) for n in ('x','y','xs','ys','xc','yc','fill','flags')] + [('show',W.WORD),('cb2',W.WORD),('reserved2',P),('stdin',P),('stdout',P),('stderr',P)]
class ProcessInfo(C.Structure):
    _fields_ = [('process',P),('thread',P),('pid',D),('tid',D)]
class FloatSave(C.Structure):
    _fields_ = [(n,D) for n in ('control','status','tag','error_offset','error_selector','data_offset','data_selector')] + [('registers',C.c_byte*80),('cr0',D)]
class Context(C.Structure):
    _fields_ = [('flags',D)] + [(n,D) for n in ('dr0','dr1','dr2','dr3','dr6','dr7')] + [('float',FloatSave)] + [(n,D) for n in ('gs','fs','es','ds','edi','esi','ebx','edx','ecx','eax','ebp','eip','cs','eflags','esp','ss')] + [('extended',C.c_byte*512)]
class ExceptionRecord(C.Structure):
    _fields_ = [('code',D),('flags',D),('record',P),('address',P),('number',D),('info',C.c_size_t*15)]
class ExceptionInfo(C.Structure):
    _fields_ = [('record',ExceptionRecord),('first',D)]
class CreateInfo(C.Structure):
    _fields_ = [('file',P),('process',P),('thread',P),('base',P),('debug_offset',D),('debug_size',D),('tls',P),('start',P),('image_name',P),('unicode',W.WORD)]
class ThreadInfo(C.Structure):
    _fields_ = [('thread',P),('tls',P),('start',P)]
class DllInfo(C.Structure):
    _fields_ = [('file',P),('base',P),('debug_offset',D),('debug_size',D),('name',P),('unicode',W.WORD)]
class EventUnion(C.Union):
    _fields_ = [('exception',ExceptionInfo),('create',CreateInfo),('thread',ThreadInfo),('dll',DllInfo),('exit_code',D)]
class Event(C.Structure):
    _fields_ = [('code',D),('pid',D),('tid',D),('u',EventUnion)]
assert C.sizeof(Context)==716 and C.sizeof(Event)==176

def api(name, args, restype=W.BOOL):
    fn=getattr(K,name); fn.argtypes=args; fn.restype=restype; return fn
Create=api('CreateProcessW',[W.LPCWSTR,W.LPWSTR,P,P,W.BOOL,D,P,W.LPCWSTR,C.POINTER(Startup),C.POINTER(ProcessInfo)])
Wait=api('WaitForDebugEvent',[C.POINTER(Event),D])
Continue=api('ContinueDebugEvent',[D,D,D])
Read=api('ReadProcessMemory',[P,P,P,C.c_size_t,C.POINTER(C.c_size_t)])
Write=api('WriteProcessMemory',[P,P,P,C.c_size_t,C.POINTER(C.c_size_t)])
GetContext=api('Wow64GetThreadContext',[P,C.POINTER(Context)])
SetContext=api('Wow64SetThreadContext',[P,C.POINTER(Context)])
Flush=api('FlushInstructionCache',[P,P,C.c_size_t])
Close=api('CloseHandle',[P])
Terminate=api('TerminateProcess',[P,W.UINT])
Allocate=api('VirtualAllocEx',[P,P,C.c_size_t,D,D],P)

def check(ok):
    if not ok: raise C.WinError(C.get_last_error())
    return ok

class Debugger:
    def __init__(self, exe, arguments=''):
        self.exe=Path(exe).resolve(); self.breakpoints={}; self.threads={}; self.stepping={}; self.exceptions=[]
        if os.environ.get('RBO_TEST_SILENT') == '1':
            # Test-only override, before the child can create an audio device.
            # Never changes the installation or any user's active game.
            owned = [Path(tempfile.gettempdir()) / name for name in
                     ('rbo-netplay-lab', 'rbo-netplay-20261108')]
            if not any(self.exe.is_relative_to(p.resolve()) for p in owned):
                raise ValueError('Silent test games must use an isolated runtime')
            movies = self.exe.parent / 'DATA/Movie'
            if movies.exists() and any(p.is_file() for p in movies.rglob('*')):
                raise ValueError('Silent test runtime must not contain movie audio')
            config = self.exe.parent / 'DATA/SaveData/SystemConfig.v4'
            defaults = (3, 0, 3, 0, 0, 0, 32, 32, 32, 32, 32, 32, 2, 2, 1, 3, 60, 0, 0, 0)
            data = bytearray(config.read_bytes() if config.exists() else struct.pack('<20I', *defaults))
            if len(data) != 80:
                raise ValueError('Unexpected test sound configuration size')
            if struct.unpack_from('<I', data)[0] != 3:
                raise ValueError('Unrecognized test configuration version; refusing audible defaults')
            struct.pack_into('<I', data, 15 * 4, 3)  # All non-movie audio off.
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_bytes(data)
        self.info=ProcessInfo(); startup=Startup(); startup.cb=C.sizeof(startup)
        command=C.create_unicode_buffer('"'+str(self.exe)+'" '+arguments)
        check(Create(str(self.exe),command,None,None,False,2,None,str(self.exe.parent),C.byref(startup),C.byref(self.info)))
        self.handle=self.info.process; self.pid=self.info.pid; self.exited=False; self.exit_code=None
        self.threads[self.info.tid]=self.info.thread

    def read(self,address,size):
        b=C.create_string_buffer(size); n=C.c_size_t()
        check(Read(self.handle,address,b,size,C.byref(n)))
        if n.value!=size: raise RuntimeError('Partial process read')
        return b.raw

    def uint(self,address): return struct.unpack('<I',self.read(address,4))[0]
    def allocate_data(self, size):
        address = Allocate(self.handle, None, size, 0x3000, 0x04)
        check(address)
        return address
    def words(self,address,count): return list(struct.unpack('<'+'I'*count,self.read(address,4*count)))
    def write(self,address,data):
        b=C.create_string_buffer(data); n=C.c_size_t()
        check(Write(self.handle,address,b,len(data),C.byref(n)))
        if n.value!=len(data): raise RuntimeError('Partial process write')
        check(Flush(self.handle,address,len(data)))

    def breakpoint(self,address,callback):
        if address in self.breakpoints: raise ValueError('Duplicate breakpoint')
        self.breakpoints[address]=(self.read(address,1),callback)
        self.write(address,b'\xcc')

    def remove_breakpoint(self, address):
        original, _ = self.breakpoints.pop(address)
        self.write(address, original)

    def run(self,on_create,seconds=120):
        deadline=time.monotonic()+seconds if seconds is not None else None
        try:
            while deadline is None or time.monotonic()<deadline:
                event=Event()
                if not Wait(C.byref(event),200):
                    err=C.get_last_error()
                    if err==121: continue
                    raise C.WinError(err)
                status=0x10002
                try:
                    if event.code==3:
                        self.base=event.u.create.base
                        if event.u.create.file: Close(event.u.create.file)
                        on_create(self)
                    elif event.code==2:
                        self.threads[event.tid]=event.u.thread.thread
                    elif event.code==4:
                        ended=self.threads.pop(event.tid,None)
                        if ended and ended!=self.info.thread:Close(ended)
                    elif event.code==6:
                        if event.u.dll.file: Close(event.u.dll.file)
                    elif event.code==5:
                        self.exited=True
                        self.exit_code=event.u.exit_code
                        print('PROCESS_EXIT',event.u.exit_code,flush=True)
                        return
                    elif event.code==1:
                        ex=event.u.exception.record; addr=ex.address; code=ex.code
                        thread=self.threads[event.tid]
                        if code in (0x80000003,0x4000001f) and addr in self.breakpoints:
                            ctx=Context();ctx.flags=0x10007;check(GetContext(thread,C.byref(ctx)))
                            old,callback=self.breakpoints[addr]
                            self.write(addr,old)
                            ctx.eip=addr
                            keep=callback(self,ctx)
                            if keep is False:
                                # Restore the instruction pointer before resuming
                                # the event for process cleanup.
                                check(SetContext(thread,C.byref(ctx)))
                                return
                            if addr in self.breakpoints:
                                ctx.eflags|=0x100
                                self.stepping[event.tid]=addr
                            check(SetContext(thread,C.byref(ctx)))
                        elif code in (0x80000004,0x4000001e) and event.tid in self.stepping:
                            addr=self.stepping.pop(event.tid)
                            self.write(addr,b'\xcc')
                        elif code not in (0x80000003,0x4000001f):
                            if code!=0xe06d7363 or not event.u.exception.first:
                                self.exceptions.append({'code': hex(code), 'address': hex(addr or 0), 'first': bool(event.u.exception.first)})
                                self.exceptions = self.exceptions[-8:]
                                print('EXCEPTION',hex(code),hex(addr or 0),'first',event.u.exception.first,flush=True)
                            status=0x80010001
                finally:
                    check(Continue(event.pid,event.tid,status))
        finally:
            if not self.exited:
                Terminate(self.handle,0)
                # Debug events are queued per debugger thread, not per object.
                # Drain this child's termination before a recovery creates the
                # next child on the same thread, or stale thread IDs leak in.
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    ended = Event()
                    if not Wait(C.byref(ended), 100):
                        continue
                    if ended.code == 6 and ended.u.dll.file:
                        Close(ended.u.dll.file)
                    if ended.code == 2:
                        self.threads[ended.tid] = ended.u.thread.thread
                    check(Continue(ended.pid, ended.tid, 0x10002))
                    if ended.pid == self.pid and ended.code == 5:
                        self.exited = True
                        break
            for thread in self.threads.values():
                if thread!=self.info.thread:Close(thread)
            Close(self.info.thread);Close(self.handle)
