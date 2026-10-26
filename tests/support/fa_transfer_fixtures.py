"""Native FA-port protocol fixtures and authenticated original Thumb transfers."""
import argparse
import hashlib
import json
from pathlib import Path
import struct

from support.fixtures import base_state, qemu_run, words

from support.paths import ROOT


def profile(initial=()):
    return {'profile':'word-pairs-128ns','initial_halfwords':[
        {'address':a,'value':v} for a,v in initial]}


def memory(snapshot):
    result={}
    for span in snapshot['fa_transfer']['memory']:
        data=bytes.fromhex(span['little_endian_hex'])
        assert len(data)==2*span['halfwords']
        for i,(v,) in enumerate(struct.iter_unpack('<H',data)):
            result[span['start_halfword']+i]=v
    return result


def small(code,initial=(),regs=None):
    s=base_state(words(*code,0xeafffffe),limit=len(code),regs=regs or [1,0xfa040028,0xec318e00,0xfa040024,0x8001,2]+[0]*10)
    s['fa_transfer']=profile(initial)
    return s


def original(count,initial=()):
    main=(ROOT / 'fw/non-provisioned/w800i-r1l002-cid49-main.bin').read_bytes()
    assert hashlib.sha256(main).hexdigest()=='46e09443e9d35fb3a4a3891e235af0b19180501ad9d57f959baf7869fd83fa8d'
    data=main[0x441683f0-0x44020000:0x441683f0-0x44020000+2*count]
    regs=[0]*16;regs[0:3]=[0x8000,count,0x4c001000];regs[13:15]=[0x4c008000,0x1001]
    s={'format_version':1,'entry':0x449ff6f4,'cpsr':0xf3,'sctlr':0x90078,
       'instruction_limit':800,'registers':regs,
       'executable_ranges':[{'start':0x449ff6f4,'end':0x449ff756},{'start':0x1000,'end':0x1002}],
       'regions':[{'name':'original.helper','address':0x449ff000,'size':4096,'readonly':True,
                   'seeds':[{'address':0x449ff000,'data':main[0x449ff000-0x44020000:0x44a00000-0x44020000]}]},
                  {'name':'fixture.return','address':0x1000,'size':4096,'readonly':True,
                   'seeds':[{'address':0x1000,'data':bytes.fromhex('fee7')}]},
                  {'name':'fixture.ram','address':0x4c000000,'size':0x10000,
                   'seeds':[{'address':0x4c001000,'data':data}] if data else []}],
       'fa_transfer':profile(initial)}
    return s,data


def run(output):
    checks=[]
    def check(name,s,reason):
        r=qemu_run(s,output/name,trace=True)
        assert r['stop']['reason']==reason,(name,r['stop'])
        checks.append({'name':name,'passed':True,'stop':r['stop'],'state':r['fa_transfer']})
        return r
    r=check('write-busy-readback',small([0xe5814000,0xe5812004,0xe5830000,
        0xe5936000,0xe1a07007,0xe5937000,0xe5835000,0xe1a08008,0xe1a08008,0xe5938000,0xe5919004]),'instruction_limit')
    assert r['registers'][6:10]==[1,0,0,0xec318e00]
    assert memory(r)=={0x8000:0xec31,0x8001:0x8e00}
    assert (r['fa_transfer']['write_commands'],r['fa_transfer']['read_commands'],r['fa_transfer']['completions'])==(1,1,2)
    r=check('cold-command-idle',small([0xe5936000]),'instruction_limit')
    assert r['registers'][6]==0 and not memory(r)
    for name,code,reason in [
      ('cold-data',[0xe5916004],'fa_transfer_data_not_ready'),
      ('address-read',[0xe5916000],'fa_transfer_address_read_unmodeled'),
      ('no-address',[0xe5830000],'fa_transfer_staging_unknown'),
      ('no-data',[0xe5814000,0xe5830000],'fa_transfer_staging_unknown'),
      ('unknown-peer',[0xe5814000,0xe5835000],'fa_transfer_peer_memory_unknown'),
      ('busy-address',[0xe5814000,0xe5812004,0xe5830000,0xe5814000],'fa_transfer_write_while_busy_unmodeled'),
      ('early-data',[0xe5814000,0xe5835000,0xe5916004],'fa_transfer_data_not_ready'),
      ('byte-store',[0xe5c14000],'fa_transfer_access_unmodeled'),
      ('halfword-store',[0xe1c140b0],'fa_transfer_access_unmodeled'),
      ('crossing-word',[0xe5814002],'fa_transfer_access_unmodeled'),
      ('neighbor',[0xe5014008],'device_register_access_unmodeled'),
    ]:
        seeds=[(0x8000,0x1234),(0x8001,0x5678)] if name=='early-data' else []
        r=check(name,small(code,seeds),reason)
        assert memory(r)==dict(seeds)
    for value in (0,0x800000,0xffffffff):
        regs=[1,0xfa040028,0,0xfa040024,value]+[0]*11
        check('bad-address-'+hex(value),small([0xe5814000],regs=regs),'fa_transfer_address_unmodeled')
    r=check('bad-command',small([0xe5814000,0xe5812004,0xe3a00003,0xe5830000]),'fa_transfer_command_unmodeled')
    assert not memory(r) and r['fa_transfer']['write_commands']==0
    for count in (0,2,16,17):
        seeds=[(0x8010,0xffff),(0x8011,0xa55a)] if count==17 else []
        s,data=original(count,seeds);r=check('original-'+str(count),s,'instruction_limit')
        assert r['registers'][15]==0x1000
        expected=dict(seeds);expected.update({0x8000+i:v[0] for i,v in enumerate(struct.iter_unpack('<H',data))})
        assert memory(r)==expected
        assert r['fa_transfer']['write_commands']==(count+1)//2
        assert r['fa_transfer']['read_commands']==count%2
        assert all(x['pc']==0x1000 or 0x449ff6f4<=x['pc']<0x449ff756 for x in r['trace_records'])
    s,data=original(17);r=check('original-unknown-tail',s,'fa_transfer_peer_memory_unknown')
    assert r['fa_transfer']['write_commands']==8 and r['fa_transfer']['read_commands']==0
    assert memory(r)=={0x8000+i:v[0] for i,v in enumerate(struct.iter_unpack('<H',data[:32]))}
    return {'checks':checks,'count':len(checks),'original_MAIN_sha256':'46e09443e9d35fb3a4a3891e235af0b19180501ad9d57f959baf7869fd83fa8d'}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False);r=run(a.output)
    (a.output/'verification.json').write_text(json.dumps(r,indent=2)+'\n');print(json.dumps({'passed':r['count']}))
