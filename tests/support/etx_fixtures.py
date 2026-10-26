"""Native guarded synthetic ETX completion fixtures; no real verification claimed."""


from db2010.diagnostics.artifacts import artifact as artifact_path


import argparse


import copy


import json


from pathlib import Path


import re


import struct


from support.fixtures import qemu_run


from support.paths import RESOURCES, ROOT


PACKET=0x4c000000


def state():
    spec=json.loads((RESOURCES/'experiments/0166.json').read_text())
    regs=[0]*16;regs[0]=PACKET;regs[4]=0xffff0ddd
    return {'timebase': {'profile': 'qemu-virtual-v1', 'icount_shift': 6}, 'format_version':1,'entry':0x4490d1ae,'cpsr':0xf3,'sctlr':0x90078,
        'instruction_limit':3,'registers':regs,'synthetic_etx':spec['synthetic_etx'],
        'device_registers':[{'address':0xf9030004,'width':4,'initial_value':0,'read_or_mask':0,'allowed_writes':[0],'name':'fixture-status'}],
        'executable_ranges':[{'start':0x4490d1ae,'end':0x4490d1b2}],
        'regions':[{'name':'caller','address':0x4490d000,'size':4096,'readonly':True,
                    'seeds':[{'address':0x4490d1ae,'data':bytes.fromhex('a047fee7')}]},
                   {'name':'packet','address':PACKET,'size':4096,
                    'seeds':[{'address':PACKET,'data':bytes.fromhex(spec['synthetic_etx']['expected_request_hex'])+bytes([0xa5])*66}]}]}


def run(output):
    checks=[]
    def check(name,s,reason,status):
        r=qemu_run(s,output/name,trace=True)
        assert r['stop']['reason']==reason,(name,r['stop'])
        assert r['device_registers'][str(0xf9030004)]==status,name
        if reason=='instruction_limit':
            assert r['synthetic_etx']['completed_calls']==1 and r['registers'][0]==0
            assert r['registers'][1:14]==s['registers'][1:14] and r['cpsr']==s['cpsr']
        elif 'synthetic_etx'in s:assert r['synthetic_etx']['completed_calls']==0
        if 'synthetic_etx'in s and s['registers'][4]==0xffff0ddd:
            assert all(x['pc']!=0xffff0ddc for x in r['trace_records'])
            m=json.loads((artifact_path(output/name, 'producer.json')).read_text());assert m['synthetic_etx_observations']==m['synthetic_api_observations']==1
        checks.append({'name':name,'passed':True,'stop':r['stop']});return r
    s=state();check('native-success',s,'instruction_limit',1)
    s=state();s['device_registers'][0]['initial_value']=1;check('idempotent-status1',s,'instruction_limit',1)
    s=state();s['regions'][1]['seeds'][0]['data']=s['regions'][1]['seeds'][0]['data'][:82];check('unknown-unused-tail',s,'instruction_limit',1)
    for name,value in [('unknown-status',None),('other-status',6)]:
        s=state();s['device_registers'][0]['initial_value']=value;check(name,s,'synthetic_etx_status_unmodeled',value)
    s=state();s['device_registers'][0]['width']=2;check('wrong-status-width',s,'synthetic_etx_status_unmodeled',0)
    for name,offset in [('header-mismatch',0),('length-mismatch',20),('digest-mismatch',22)]:
        s=state();raw=bytearray(s['regions'][1]['seeds'][0]['data']);raw[offset]^=1;s['regions'][1]['seeds'][0]['data']=bytes(raw);check(name,s,'synthetic_etx_payload_unmodeled',0)
    s=state();s['regions'][1]['seeds'][0]['data']=s['regions'][1]['seeds'][0]['data'][:81];check('unknown-request-byte',s,'synthetic_etx_unknown_request',0)
    for name,addr in [('mmio-request',0xf9030004),('wrapping-request',0xfffffff0),('null-request',0)]:
        s=state();s['registers'][0]=addr;check(name,s,'synthetic_etx_request_unmodeled',0)
    s=state();s['regions'][1]['readonly']=True;check('readonly-request',s,'synthetic_etx_request_unmodeled',0)
    s=state();s['entry']-=2;s['regions'][0]['seeds'][0]['address']-=2;s['executable_ranges'][0]['start']-=2;check('wrong-caller',s,'synthetic_etx_caller_unmodeled',0)
    s=state();s['registers'][3]=0xffff0ddd;s['regions'][0]['seeds'][0]['data']=bytes.fromhex('9847fee7');check('wrong-caller-opcode',s,'synthetic_etx_caller_unmodeled',0)
    s=state();s.pop('synthetic_etx');check('disabled',s,'fetch_outside_executable_bytes',0)
    s=state();s['registers'][4]=0xffff0d51;check('other-ROM',s,'fetch_outside_executable_bytes',0)
    return {'count':len(checks),'checks':checks,'crypto_verification_established':False}
