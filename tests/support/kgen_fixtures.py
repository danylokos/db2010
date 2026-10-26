"""Native synthetic-KGEN API/state fixtures; no archived CPU runner is used."""
from db2010.diagnostics.artifacts import artifact as artifact_path
import argparse
import json
from pathlib import Path
import re
import struct

from support.fixtures import qemu_run
from support import kgen_candidate as candidate

PROFILE = {'profile':'sha1-le92-init-update-8040'}
CTX, DATA = 0x4c000000, 0x4c001000


def state(kind, context=None, data=b'', *, enabled=True, bad_caller=False):
    caller, opcode, target = {'init':(0x44d9f9ce,0x4788,0xffff12a3),
                              'update':(0x44d9fa32,0x4798,0xffff12bf),
                              'final':(0x44d9fa92,0x4790,0xffff12c3)}[kind]
    if bad_caller:caller-=2
    regs=[0]*16;regs[0]=CTX
    if kind=='init':regs[1]=target
    elif kind=='update':regs[1:4]=[DATA,len(data),target]
    else:regs[1:3]=[CTX,target]
    seed=bytes.fromhex('ffee') if context is None else context
    result={'timebase': {'profile':'qemu-virtual-v1','icount_shift':6},'format_version':1,'entry':caller,'cpsr':0xd3|32,'sctlr':0x90078,
      'instruction_limit':3,'registers':regs,
      'executable_ranges':[{'start':caller,'end':caller+4}],
      'regions':[{'name':'fixture.code','address':0x44d9f000,'size':4096,'readonly':True,
                  'seeds':[{'address':caller,'data':struct.pack('<HH',opcode,0xe7fe)}]},
                 {'name':'fixture.context','address':CTX,'size':4096,
                  'seeds':[{'address':CTX,'data':seed}]},
                 {'name':'fixture.data','address':DATA,'size':8192,
                  'seeds':[{'address':DATA,'data':data}] if data else []}]}
    if enabled:result['synthetic_kgen']=PROFILE
    return result


def context_from(result):
    words=[]
    for line in result['memory_inspection'].splitlines():
        if ':' in line:
            words.extend(int(v,16) for v in re.findall(r'0x[0-9a-fA-F]+',line.split(':',1)[1]))
    return struct.pack('<23I',*words)


def run(output):
    checks=[]
    def check(name,s,reason,expected=None):
        result=qemu_run(s,output/name,trace=False,inspect='xp /23wx '+hex(CTX))
        assert result['stop']['reason']==reason,(name,result)
        if expected is not None:assert context_from(result)==expected,name
        if reason=='instruction_limit':
            assert result['registers'][:14]==s['registers'][:14],name
            assert result['cpsr']==s['cpsr'],name
        checks.append({'name':name,'passed':True,'stop':result['stop']})
        return result
    result=check('init',state('init'),'instruction_limit',candidate.initialize())
    assert result['synthetic_kgen']['init_calls']==1 and result['synthetic_kgen']['update_calls']==0
    for length in (0,1,55,56,63,64,65,128,4097):
        data=bytes((i*37+11)%256 for i in range(length))
        ctx=candidate.initialize()
        result=check('update-'+str(length),state('update',ctx,data),
                     'instruction_limit',candidate.update(ctx,data))
        assert result['synthetic_kgen']['input_bytes']==length
    prefix=b'partial prefix!'*3
    ctx=candidate.update(candidate.initialize(),prefix)
    data=bytes(range(128))
    check('pending-buffer',state('update',ctx,data),'instruction_limit',candidate.update(ctx,data))
    check('disabled',state('init',enabled=False),'fetch_outside_executable_bytes')
    check('wrong-caller',state('init',bad_caller=True),'synthetic_kgen_caller_unmodeled')
    check('final-guard',state('final'),'fetch_outside_executable_bytes')
    s=state('update',candidate.initialize(),b'abc');s['regions'][2]['seeds']=[]
    check('unknown-input',s,'synthetic_kgen_unknown_input',candidate.initialize())
    ctx=bytearray(candidate.initialize());ctx[20]=1
    check('invalid-context',state('update',bytes(ctx),b'a'),'synthetic_kgen_context_format_unmodeled',bytes(ctx))
    s=state('init',candidate.initialize());s['regions'][1]['readonly']=True
    check('readonly-context',s,'synthetic_kgen_context_unmodeled',candidate.initialize())
    s=state('update',candidate.initialize(),b'a');s['registers'][1]=0xf9090000
    check('mmio-input',s,'synthetic_kgen_input_unmodeled',candidate.initialize())
    check('unknown-context',state('update',None,b'a'),'synthetic_kgen_unknown_context')
    s=state('update',candidate.initialize());s['registers'][1]=0
    check('empty-null-input',s,'instruction_limit',candidate.initialize())
    s=state('update',candidate.initialize(),b'abc');s['registers'][1]=0xffffffff
    check('wrapping-input',s,'synthetic_kgen_input_unmodeled',candidate.initialize())
    traced=qemu_run(state('init'),output/'trace-init',trace=True)
    assert all(row['pc']<0xffff0000 for row in traced['trace_records'])
    producer=json.loads((artifact_path((output) / 'trace-init', 'producer.json')).read_text())
    assert producer['synthetic_api_observations']==1
    checks.append({'name':'trace-init','passed':True,'stop':traced['stop'],
                   'synthetic_api_observations':1,'ROM_instruction_records':0})
    return {'profile':PROFILE,'checks':checks,'ROM_equivalence_established':False}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
    result=run(a.output)
    (a.output/'verification.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'passed':len(result['checks'])}))
