"""Native FM transmit framing, independent PMIC state and unsupported-input guards."""
import argparse,copy,json,subprocess
from pathlib import Path
from support.fixtures import ROOT,qemu_run,freeze_state
from support.i2c_interleave_fixtures import state,write,read
FRAME=bytes.fromhex('0080000bd20a0040100000')
def frame(payload=FRAME,address=0x20):
 steps=[('w',2,0x84),('w',4,0x60),('w',2,0xa4),('w',0,address),('w',2,0x80)]
 for value in payload:steps += [('w',0,value),('w',2,0x80)]
 return steps+[('w',2,0x90)]
def configured(steps,allowed=None):
 s=state(steps);s['fm_radio']={'profile':'i2c-frame-sink','wire_write_address':0x20,'allowed_writes_hex':allowed or [FRAME.hex()]};return s

def run(output):
 checks=[]
 def check(name,s,reason='instruction_limit'):
  r=qemu_run(s,output/name,trace=True);assert r['stop']['reason']==reason,(name,r['stop'])
  checks.append({'name':name,'passed':True,'stop':r['stop'],'fm_radio':r.get('fm_radio')});return r
 r=check('complete-frame',configured(frame()));assert r['fm_radio']['completed_frames']==1 and r['fm_radio']['latest_frame_hex']==FRAME.hex()
 assert r['fm_radio']['completed_bytes']==11 and not r['fm_radio']['active']
 assert r['i2c']['power_writes']==27 and r['i2c']['post_startup_completed']==0 and r['i2c']['extra_writes_completed']==0
 assert '00'not in r['i2c']['last_write_registers']
 r=check('repeated-frame',configured(frame()+frame()));assert r['fm_radio']['completed_frames']==2 and r['fm_radio']['completed_bytes']==22
 r=check('PMIC-after-FM',configured(frame()+write(0x70,0xab)+read(0x71)));assert r['i2c']['extra_writes_completed']==1 and r['i2c']['extra_reads_completed']==1 and r['registers'][2]==64
 assert r['i2c']['post_startup_completed']==0 and r['fm_radio']['completed_frames']==1
 r=check('disabled',state(frame()),'i2c_async_power_byte_unmodeled');assert 'fm_radio'not in r
 r=check('wrong-first-byte',configured(frame(b'\xff'+FRAME[1:])),'fm_i2c_payload_unmodeled');assert r['fm_radio']['completed_frames']==0 and r['fm_radio']['pending_frame_hex']==''
 check('wrong-later-byte',configured(frame(FRAME[:-1]+b'\xff')),'fm_i2c_payload_unmodeled')
 check('short-frame',configured(frame(FRAME[:-1])),'fm_i2c_short_frame_unmodeled')
 check('extra-byte',configured(frame(FRAME+b'\x00')),'fm_i2c_payload_unmodeled')
 check('empty-frame',configured(frame(b'')),'fm_i2c_short_frame_unmodeled')
 check('unknown-read',configured(frame(address=0x21)),'fm_i2c_read_unmodeled')
 check('other-slave',configured(frame(address=0x22)),'i2c_async_power_byte_unmodeled')
 alt=FRAME[:-1]+b'\x01';r=check('two-shared-prefix-frames',configured(frame()+frame(alt),[FRAME.hex(),alt.hex()]));assert r['fm_radio']['latest_frame_hex']==alt.hex()
 r=check('uncommitted-until-STOP',configured(frame()[:-1]));assert r['fm_radio']['completed_frames']==0 and r['fm_radio']['pending_frame_hex']==FRAME.hex()
 for name,mutation in [('invalid-profile',lambda f:f.update(profile='all-I2C')),
                       ('invalid-address',lambda f:f.update(wire_write_address=0x22)),
                       ('invalid-hex',lambda f:f.update(allowed_writes_hex=['xx'])),
                       ('oversized-frame',lambda f:f.update(allowed_writes_hex=['00'*33])),
                       ('empty-allowlist',lambda f:f.update(allowed_writes_hex=[]))]:
  s=configured([]);mutation(s['fm_radio']);d=output/name;d.mkdir();path=freeze_state(s,d)
  p=subprocess.run([str(ROOT/'qemu/build/qemu-system-arm'),'-machine',f'db2010,research-guards=on,state={path}','-display','none','-nodefaults'],capture_output=True,text=True,timeout=5)
  assert p.returncode!=0 and 'db2010:'in p.stderr
  checks.append({'name':name,'passed':True,'rejected':True,'stderr':p.stderr})
 return {'count':len(checks),'checks':checks,'no_RF_or_read_data':True}
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False);r=run(a.output);(a.output/'verification.json').write_text(json.dumps(r,indent=2)+'\n');print(json.dumps({'passed':r['count']}))
