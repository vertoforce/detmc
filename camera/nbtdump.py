import gzip, zlib, struct, sys, io
T={0:'END',1:'byte',2:'short',3:'int',4:'long',5:'float',6:'double',7:'bytearr',8:'str',9:'list',10:'comp',11:'intarr',12:'longarr'}
class R:
    def __init__(s,d): s.d=d; s.i=0
    def u(s,f,n): v=struct.unpack_from(f,s.d,s.i); s.i+=n; return v[0]
    def b(s): return s.u('>b',1)
    def sh(s): return s.u('>h',2)
    def i32(s): return s.u('>i',4)
    def st(s):
        n=s.u('>H',2); v=s.d[s.i:s.i+n].decode('utf8','replace'); s.i+=n; return v
def payload(r,t):
    if t==1: return r.b()
    if t==2: return r.sh()
    if t==3: return r.i32()
    if t==4: return r.u('>q',8)
    if t==5: return r.u('>f',4)
    if t==6: return r.u('>d',8)
    if t==7:
        n=r.i32(); r.i+=n; return f'<bytearr {n}>'
    if t==8: return r.st()
    if t==9:
        it=r.b(); n=r.i32(); return [payload(r,it) for _ in range(n)] if n>0 else []
    if t==10:
        o={}
        while True:
            tt=r.b()
            if tt==0: return o
            nm=r.st(); o[nm]=payload(r,tt)
    if t==11:
        n=r.i32(); v=[r.i32() for _ in range(n)]; return v
    if t==12:
        n=r.i32(); v=list(struct.unpack_from(f'>{n}q', r.d, r.i)); r.i+=8*n; return v
    raise Exception(f'bad tag {t}')
def parse(d):
    r=R(d); t=r.b(); nm=r.st() if t!=0 else ''
    return payload(r,t)
def show(o,ind=0,maxd=4):
    p=' '*ind
    if isinstance(o,dict):
        for k,v in o.items():
            if isinstance(v,(dict,list)) and ind<maxd*2:
                print(f'{p}{k}:'); show(v,ind+2,maxd)
            else:
                s=str(v)
                print(f'{p}{k} = {s[:120]}')
    elif isinstance(o,list):
        print(f'{p}[{len(o)} items]')
        for v in o[:3]: show(v,ind+2,maxd)
