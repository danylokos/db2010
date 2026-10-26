"""QEMU bus-level PMIC same-register interleaving and ordered-tail guards."""
from support.paths import RESOURCES
from db2010.diagnostics.artifacts import artifact as artifact_path, read_json
import argparse
import copy
import gzip
import json
from pathlib import Path
from support.fixtures import ROOT,base_state,qemu_run,words

def write(reg,value):
    return [('w',2,0x84),('w',4,0x60),('w',2,0xa4),('w',0,0x94),('w',2,0x80),
            ('w',0,reg),('w',2,0x80),('w',0,value),('w',2,0x80),('w',2,0x90)]

def read(reg):
    return [('w',2,0x84),('w',4,0x60),('w',2,0xa4),('w',0,0x94),('w',2,0x80),
            ('w',0,reg),('w',2,0x80),('w',2,0xa4),('w',0,0x95),('w',2,0x80),
            ('w',2,0x80),('r',0,0),('w',2,0x90)]

def burst(reg,count):
    steps=read(reg)[:10]
    for i in range(count):steps += [('w',2,0x84 if i<count-1 else 0x80),('r',0,0)]
    return steps+[('w',2,0x90)]


def state(suffix,allowed=True,burst_values=None,extra_padding=0):
    cfg=json.loads((RESOURCES/'experiments/0174.json').read_text())
    i2c=copy.deepcopy(cfg['i2c_controller'])
    i2c['completion_ns']=64*i2c.pop('completion_instructions')
    i2c['register_reads']=[{'register':0x20,'value':0xe3}]
    i2c['post_startup']=[{'operation':'write','register':0x70,'value':0xa8},
                          {'operation':'read','register':0x71,'value':64}]
    i2c.pop('post_startup_repeat_from')
    i2c['post_startup_writes']=[{'register':0x80+i,'value':i}for i in range(extra_padding)]+[{'register':0x70,'value':0xab if allowed else 0xac}]
    i2c['post_startup_reads']=[{'register':0x71,'value':64}]
    if burst_values is not None:i2c['post_startup_reads'] += [{'register':0x92+i,'value':v}for i,v in enumerate(burst_values)]
    boot=json.loads((RESOURCES/'power-init-writes.json').read_text())['writes']
    assert len(boot)==27 and boot[-1]['register']==0x91
    steps=[('w',4,0x60),('w',2,4),('w',0,0),('w',2,0),('w',0,0),('w',2,0),
           ('w',2,0x84),('w',4,0x60),('w',2,0xa4),('w',0,1),('w',2,0x80),('w',2,0x90)]
    for pair in boot:steps+=write(pair['register'],pair['value'])
    for reg in [0x21,0x22,0x23,0x20]:steps+=read(reg)
    steps+=suffix
    code=[]
    for op,off,value in steps:
        code += [0xe59f0008,0xe59f1008,0xe1c100b0 if op=='w'else 0xe1d120b0,
                 0xea000001,value,0xf9040000+off]
    code.append(0xeafffffe);raw=words(*code)
    s=base_state(raw,limit=len(steps)*4+16)
    s['regions'][0]['size']=(len(raw)+4095)&~4095
    s['executable_ranges'][0]['end']=0x1000+len(raw)
    s['recording_history']={'profile':'startup64-recent64-irq512'}
    s['i2c_controller']=i2c;return s

def run(output):
    checks=[]
    def check(name,s,reason):
        r=qemu_run(s,output/name,trace=True);assert r['stop']['reason']==reason,(name,r['stop'])
        checks.append({'name':name,'passed':True,'stop':r['stop']});return r
    r=check('independent-70AB',state(write(0x70,0xab)),'instruction_limit')
    assert r['i2c']['post_startup_completed']==0 and r['i2c']['extra_writes_completed']==1
    assert r['i2c']['last_write_registers']['70']==0xab
    r=check('independent-read-does-not-consume-tail',state(write(0x70,0xab)+read(0x71)),'instruction_limit')
    assert r['i2c']['post_startup_completed']==0 and r['i2c']['extra_reads_completed']==1
    assert r['registers'][2]==64
    r=check('ordered-tail-after-independent',state(write(0x70,0xab)+read(0x71)+write(0x70,0xa8)+read(0x71)),'instruction_limit')
    assert r['i2c']['post_startup_completed']==2 and r['i2c']['extra_writes_completed']==1
    assert r['i2c']['last_write_registers']['70']==0xa8
    r=check('undeclared-value',state(write(0x70,0xab),allowed=False),'i2c_async_power_byte_unmodeled')
    assert r['i2c']['post_startup_completed']==r['i2c']['extra_writes_completed']==0
    assert '70'not in r['i2c']['last_write_registers']
    r=check('last-of128-declared-writes',state(write(0x70,0xab),extra_padding=127),'instruction_limit')
    assert r['i2c']['extra_write_declared']==128 and r['i2c']['extra_writes_completed']==1
    for count in [1,2,3]:
        r=check('burst-'+str(count),state(burst(0x92,count),burst_values=[0x12,0x34,0x56]),'instruction_limit')
        assert r['registers'][2]==[0x12,0x34,0x56][count-1]
        assert r['i2c']['extra_reads_completed']==1 and r['i2c']['post_startup_completed']==0
    check('unknown-middle-byte',state(burst(0x92,3),burst_values=[0x12,None,0x56]),'i2c_extra_read_input_unknown')
    check('burst-beyond94',state(burst(0x92,4),burst_values=[0x12,0x34,0x56]),'i2c_multibyte_register_unmodeled')
    incomplete=read(0x92)[:10]+[('w',2,0x84),('r',0,0),('w',2,0x90)]
    check('stop-before-final-NACK',state(incomplete,burst_values=[0x12,0x34,0x56]),'i2c_multibyte_stop_before_nack')
    return {'count':len(checks),'checks':checks,'new_ADC_values_supplied':False}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False);r=run(a.output)
    (a.output/'verification.json').write_text(json.dumps(r,indent=2)+'\n');print(json.dumps({'passed':r['count']}))
