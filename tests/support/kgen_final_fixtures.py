"""Native synthetic Final: SHA-1 boundaries and pre-effect API guards (0163)."""


from db2010.diagnostics.artifacts import artifact as artifact_path


import argparse


import hashlib


import json


from pathlib import Path


import struct


from support.fixtures import qemu_run


from support.kgen_fixtures import state as old_state, context_from


from support import kgen_candidate as candidate


PROFILE={'profile':'sha1-le92-init-update-final-8040'}


CTX,OUT=0x4c000000,0x4c002000


def state(context, *, enabled=True):
    s=old_state('final',context,enabled=enabled)
    if enabled:s['synthetic_kgen']=PROFILE
    s['registers'][0]=OUT
    # Original BLX R2; ordinary native LDMIA reads the output into R3..R7.
    s['regions'][0]['seeds'][0]['data']=struct.pack('<HHH',0x4790,0xc8f8,0xe7fe)
    s['executable_ranges'][0]['end']+=2
    s['regions'][2]['seeds']=[{'address':OUT,'data':bytes([0xa5])*20}]
    return s


def run(output):
    checks=[]
    def check(name,s,reason,context=None,digest=None):
        r=qemu_run(s,output/name,trace=True,inspect='xp /23wx '+hex(CTX))
        assert r['stop']['reason']==reason,(name,r['stop'])
        if context is not None:assert context_from(r)==context,name
        if digest is not None:
            actual=struct.pack('<5I',*r['registers'][3:8]);assert actual==digest,(name,actual.hex(),digest.hex())
            assert r['synthetic_kgen']['final_calls']==1
            assert r['cpsr']==s['cpsr']
            assert r['registers'][0]==OUT+20 and r['registers'][1:3]==s['registers'][1:3]
            assert r['registers'][8:14]==s['registers'][8:14]
        elif s.get('synthetic_kgen')==PROFILE:assert r['synthetic_kgen']['final_calls']==0
        metrics=json.loads((artifact_path(output/name, 'producer.json')).read_text())
        if s.get('synthetic_kgen')==PROFILE:
            assert metrics['synthetic_api_observations']==1
            assert all(x['pc']!=0xffff12c2 for x in r['trace_records'])
        checks.append({'name':name,'passed':True,'stop':r['stop'],'digest':digest.hex()if digest else None})
        return r
    for length in [0,1,3,55,56,63,64,65,119,120,127,128,272,4097]:
        data=bytes((i*37+11)%256 for i in range(length));ctx=candidate.initialize()
        for i in range(0,length,7):ctx=candidate.update(ctx,data[i:i+7])
        after,digest=candidate.finalize(ctx);assert digest==hashlib.sha1(data).digest()
        check('final-'+str(length),state(ctx),'instruction_limit',after,digest)
    ctx=candidate.initialize()
    check('disabled',state(ctx,enabled=False),'fetch_outside_executable_bytes',ctx)
    s=state(ctx);s['synthetic_kgen']={'profile':'sha1-le92-init-update-8040'}
    check('old-profile-still-guards',s,'fetch_outside_executable_bytes',ctx)
    check('unknown-context',state(None),'synthetic_kgen_unknown_context')
    bad=bytearray(ctx);bad[20]=1
    check('invalid-bit-count',state(bytes(bad)),'synthetic_kgen_context_format_unmodeled',bytes(bad))
    for name,dest in [('null-output',0),('mmio-output',0xf9090000),('wrap-output',0xfffffff8),('overlap-output',CTX+8)]:
        s=state(ctx);s['registers'][0]=dest;check(name,s,'synthetic_kgen_output_unmodeled',ctx)
    s=state(ctx);s['regions'][2]['readonly']=True;check('readonly-output',s,'synthetic_kgen_output_unmodeled',ctx)
    s=state(ctx);s['regions'][1]['readonly']=True;check('readonly-context',s,'synthetic_kgen_context_unmodeled',ctx)
    s=state(ctx);s['registers'][1]=0xf9090000;check('mmio-context',s,'synthetic_kgen_context_unmodeled',ctx)
    s=state(ctx);s['regions'][0]['seeds'][0]['data']=struct.pack('<HHH',0x4790,0xc8f8,0xe7fe)
    s['entry']-=2;s['regions'][0]['seeds'][0]['address']-=2;s['executable_ranges'][0]['start']-=2
    check('wrong-caller',s,'synthetic_kgen_caller_unmodeled',ctx)
    return {'count':len(checks),'checks':checks,'ROM_equivalence_established':False}
