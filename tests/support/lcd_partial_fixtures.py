"""Native QEMU checks of panel addressing, retained GRAM and guarded DMA."""
from support.paths import RESOURCES
import argparse
import json
from pathlib import Path
from support.fixtures import base_state, qemu_run, words

PROFILE='cold-disabled-display-partial-fa-tx'
def w(a,v):return ('w',a,v,4)
def packet(raw,rs,trigger=1):
    n=len(raw);data=[0]*6
    for i,b in enumerate(raw):
        j=n-1-i;data[j//4]|=b<<(8*(3-j%4))
    steps=[w(0xf700017c-4*i,data[i]) for i in range((n+3)//4)]
    steps += [w(0xf700018c,sum(v<<(32-n+i) for i,v in enumerate(rs))),w(0xf70001dc,trigger)]
    return steps

def registers(pairs):
    raw=[]
    for a,v in pairs:raw += [0,a,v>>8,v&255]
    return packet(raw,[0,0,1,1]*len(pairs))

def setup(mode=0x7230,window=0xaf00,geometry=True):
    raw=bytes.fromhex('1f1d09001f2090991f289c9917980c001705150517f81f001f08')
    steps=[w(0xf7000100+i,int.from_bytes(raw[i:i+4],'little')) for i in range(0,len(raw),4)]
    steps += [w(0xf70001f8,0x200)]
    if geometry:steps+=registers([(3,mode),(0x44,window),(0x45,0xdb00)])
    steps += [('r',0xf70001f4,0,4)]*12
    return steps

def dma(cursor,count,source=0x4c000000):
    raw=bytearray.fromhex('1f1d09001f2090991f289c9917980a001705150517f813800000178800001760150117e017e81f001f0d')
    repeat=1
    while count>65535:count//=2;repeat*=2
    raw[24:26]=count.to_bytes(2,'little');raw[28:30]=repeat.to_bytes(2,'little')
    steps=[w(0xf700011c+i,int.from_bytes(raw[i:i+4],'little')) for i in range(0,len(raw),4)]
    steps += [w(0xf70001fc,0x1c1c1c),w(0xf70001f8,0x180200)]
    steps += packet([0,0,0,0,0,0x21,cursor>>8,cursor&255,0,0x22],[0,0,0,0,0,0,1,1,0,0],5)
    steps += [w(0xf2000030,1),w(0xf20001a0,source),w(0xf20001a4,0xf7000308),w(0xf20001a8,0),w(0xf20001ac,0x84489004),w(0xf20001b0,0xea52),w(0xf20001b0,0xea53)]
    return steps

def state(steps,raw,profile=PROFILE,limit=None):
    # Assemble MMIO steps and bounded CPU wait loops with a shared literal pool.
    ins=[];literals=[];patches=[]
    for op,a,v,width in steps:
        if op=='wait':
            ins += [0xe3a03b05,0xe2533001,0x1afffffd] # r3=5120; subs/bne
            continue
        for value,register in [(v,0),(a,1)]:
            patches.append((len(ins),len(literals),register));ins.append(0);literals.append(value)
        ins.append({('w',4):0xe5810000,('r',4):0xe5912000}[(op,width)])
    ins.append(0xeafffffe)
    for position,literal,register in patches:
        offset=(len(ins)+literal)*4-(position*4+8);assert 0<=offset<=4095
        ins[position]=0xe59f0000|(register<<12)|offset
    s=base_state(words(*ins,*literals))
    s['instruction_limit']=limit or len(ins)+len([s for s in steps if s[0]=='wait'])*10240+16
    s['pdi']={'profile':'r61500-write-packets-retained-latches'}
    s['pl080_dma']={'profile':profile,'address':0xf2000000,'channels':8}
    cfg=json.loads((RESOURCES/'experiments/0184.json').read_text())
    s['device_registers']=[{**r,'initial_value':0x120} for r in cfg['device_registers'] if r['address']==0xf7000304]
    s['regions'].append({'name':'pixels','address':0x4c000000,'size':0x20000,'seeds':[{'address':0x4c000000,'data':raw}] if raw else []})
    if limit:s['instruction_limit']=limit
    return s

def run(out):
    checks=[]
    def check(name,steps,raw,reason='instruction_limit',profile=PROFILE,limit=None):
        r=qemu_run(state(steps,raw,profile,limit),out/name,trace=True)
        assert r['stop']['reason']==reason,(name,r['stop'])
        checks.append({'name':name,'stop':r['stop'],'passed':True});return r
    raw=bytes(range(32))
    r=check('partial-cold-unknown',setup()+dma(0xa800,32),raw)
    f=r['pdi']['frames'][0]
    assert f['update_hex']==raw.hex() and f['pixels_hex']=='' and f['panel_known_pixels']==16 and f['cursor_end']==0xa810
    assert r['pl080']['channels'][5]['source']==0x4c000020 and r['pl080']['raw_tc']==32
    r=check('one-burst-completes',setup()+dma(0,16),raw)
    assert r['pdi']['dma_complete'] and r['pdi']['frames'][0]['bytes']==16
    r=check('row-wrap',setup()+dma(0xa8a8,32),raw)
    assert r['pdi']['frames'][0]['cursor_end']==0xa908
    r=check('panel-wrap',setup()+dma(0xdba8,32),raw)
    assert r['pdi']['frames'][0]['cursor_end']==8
    # Full source sets knowledge; next partial native STOP/TC-clear/enable preserves every other byte.
    full=bytes([0x12,0x34])*38720
    stop=[w(0xf2000008,32),w(0xf70001f8,0x180208),w(0xf70001dc,5)]
    # 4839 remaining bursts run while the CPU polls; no synthetic IRQ handler.
    polls=[('r',0xf70001f4,0,4)]*12
    # An explicit horizon allows the full transfer to finish without a CPU intervention.
    r=check('full-known-frame',setup()+dma(0,77440),full,limit=6000)
    assert bytes.fromhex(r['pdi']['frames'][0]['pixels_hex'])==full
    steps=setup()+dma(0,32)+polls+stop+dma(0x20,32,0x4c000020)
    r=check('retained-distinct-partials',steps,bytes(range(64)))
    assert len(r['pdi']['frames'])==2 and r['pdi']['panel']['known_pixels']==32
    assert [f['update_hex'] for f in r['pdi']['frames']]==[bytes(range(32)).hex(),bytes(range(32,64)).hex()]
    r=check('full-then-partial-composite',setup()+dma(0,77440)+[('wait',0,0,4)]+stop+dma(0xa800,32,0x4c012e80),full+raw)
    f=r['pdi']['frames'][1];expected=bytearray(full);expected[168*176*2:168*176*2+32]=raw
    assert bytes.fromhex(f['pixels_hex'])==expected and f['update_hex']==raw.hex()
    assert f['panel_known_pixels']==38720 and len(r['pdi']['frames'])==2
    for name,kw,reason in [
        ('unknown-geometry',{'geometry':False},'display_panel_geometry_unmodeled'),
        ('unsupported-mode',{'mode':0xd230},'display_panel_geometry_unmodeled'),
        ('unsupported-window',{'window':0xae00},'display_panel_geometry_unmodeled')]:
        check(name,setup(**kw)+dma(0xa800,32),raw,reason)
    for name,cursor,n,reason in [('x-group-misaligned',1,32,'display_panel_cursor_unmodeled'),('x-outside',0xb0,32,'display_panel_cursor_unmodeled'),('y-outside',0xdc00,32,'display_panel_cursor_unmodeled'),('unaligned-count',0,18,'display_panel_dma_count_unmodeled')]:
        check(name,setup()+dma(cursor,n),raw,reason)
    check('legacy-disabled',setup()+dma(0xa800,32),raw,'display_dma_configuration_unmodeled','cold-disabled-display-stream-fa-tx')
    check('unknown-panel-index',setup()+registers([(0x46,0),(3,0x7230),(0x44,0xaf00)]),raw,'display_panel_index_unmodeled')
    check('unmodeled-pio-pixels',setup()+registers([(0x22,0),(3,0x7230),(0x44,0xaf00)]),raw,'display_panel_data_unmodeled')
    check('unknown-first-source',setup()+dma(0,32),b'','display_dma_source_unknown')
    r=check('unknown-later-source',setup()+dma(0,32),raw[:16],'display_dma_source_unknown')
    assert r['pdi']['panel']['known_pixels']==8 and not r['pdi']['frames']
    return {'count':len(checks),'checks':checks,'native_firmware_probe_required':True}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False);r=run(a.output)
    (a.output/'verification.json').write_text(json.dumps(r,indent=2)+'\n');print(json.dumps({'passed':r['count']}))
