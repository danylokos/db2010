"""Observed image-viewer selector, three-byte pixels and native stream migration."""
import copy

import pytest

from support.fixtures import qemu_run
from support.lcd_partial_fixtures import dma, setup, state, w
from test_native_checkpoint import comparable, engine, migration, stopped


def rgb666_state(variant='normal', count=116160, cursor=0):
    # Deliberately cross triplets over sixteen-byte bursts, using low bits too.
    raw=(bytes.fromhex('ff000000ff000000ff153f7babcdef')*7744)[:count]
    assert len(raw)==count
    operations=setup(mode=0xd230)+[w(0xf7000304,0x320)]+dma(cursor,count)
    operations=[(op,a,0xffff1c if a==0xf70001fc else v,width) for op,a,v,width in operations]
    if variant=='selector':
        operations=[(op,a,0xff1c1c if a==0xf70001fc else v,width) for op,a,v,width in operations]
    elif variant=='program':
        operations=[(op,a,v^1 if a==0xf700011c else v,width) for op,a,v,width in operations]
    elif variant=='stream':
        operations=[(op,a,0x120 if a==0xf7000304 else v,width) for op,a,v,width in operations]
    elif variant=='mode':
        operations=setup(mode=0x7230)+operations[len(setup(mode=0xd230)):]
    elif variant=='count':
        operations=[(op,a,(v&0xffff0000)|3 if a==0xf7000138 else v,width) for op,a,v,width in operations]
    before=len(operations)*3+1
    s=state(operations,raw[:16] if variant=='unknown' else raw,limit=12000)
    expected=bytearray()
    for r,g,b in zip(raw[0::3],raw[1::3],raw[2::3]):
        # Independent component packing for the existing RGB565 display view.
        expected.extend(((r&0xf8)|((g>>5)&7),((g&0x1c)<<3)|(b>>3)))
    return s,raw,bytes(expected),before


def test_native_rgb666_full_frame_and_triplet_burst_boundaries(tmp_path):
    s,raw,expected,_=rgb666_state()
    r=qemu_run(s,tmp_path/'full',trace=False)
    assert r['stop']['reason']=='instruction_limit',r['stop']
    frame=r['pdi']['frames'][0]
    assert frame['input_pixel_format']=='rgb666-in-rgb888'
    assert frame['update_hex']==raw.hex()
    assert frame['pixels_hex']==expected.hex()
    assert frame['panel_known_pixels']==176*220 and frame['cursor_end']==0
    assert r['pl080']['raw_tc']==32
    assert r['pl080']['channels'][5]['source']==0x4c000000+len(raw)


def test_rgb565_return_with_retained_stream_and_disabled_unused_selectors(tmp_path):
    # Native image-viewer timeout restores R03=7230 while F7000304 stays320.
    # Panel mode, rather than that retained stream bit, owns pixel depth.
    raw=(bytes.fromhex('f80007e0001f')*12907)[:77440]
    operations=setup()+[w(0xf7000304,0x320)]+dma(0,77440)
    operations=[(op,a,0xffff1c if a==0xf70001fc else v,width) for op,a,v,width in operations]
    r=qemu_run(state(operations,raw,limit=12000),tmp_path/'rgb565',trace=False)
    assert r['stop']['reason']=='instruction_limit',r['stop']
    frame=r['pdi']['frames'][0]
    assert frame['input_pixel_format']=='rgb565-be' and frame['pixels_hex']==raw.hex()


@pytest.mark.parametrize('count,cursor', [(48, 0), (11616, 0xc400)])
def test_rgb666_partial_preserves_other_pixels_and_triplet_phase(tmp_path, count, cursor):
    background = bytes.fromhex('19a7') * (176 * 220)
    raw = (bytes.fromhex('ff000000ff000000ff153f7babcdef') * count)[:count]
    stop = [w(0xf2000008, 32), w(0xf70001f8, 0x180208), w(0xf70001dc, 5)]
    steps = setup() + dma(0, len(background)) + [('wait', 0, 0, 4)] + stop
    steps += setup(mode=0xd230) + [w(0xf7000304, 0x320)]
    steps += dma(cursor, count, 0x4c012e80)
    steps = [(op, a, 0xffff1c if a == 0xf70001fc else v, width)
             for op, a, v, width in steps]
    r = qemu_run(state(steps, background + raw, limit=18000), tmp_path/'partial', trace=False)
    assert r['stop']['reason'] == 'instruction_limit', r['stop']
    frame = r['pdi']['frames'][-1]
    assert frame['input_pixel_format'] == 'rgb666-in-rgb888'
    assert frame['update_hex'] == raw.hex()
    projected = bytearray()
    for red, green, blue in zip(raw[0::3], raw[1::3], raw[2::3]):
        projected.extend(((red & 0xf8) | (green >> 5), ((green & 0x1c) << 3) | (blue >> 3)))
    start = ((cursor >> 8) * 176 + (cursor & 255)) * 2
    expected = bytearray(background)
    expected[start:start+len(projected)] = projected
    assert bytes.fromhex(frame['pixels_hex']) == expected
    assert frame['cursor_start'] == cursor
    assert frame['panel_known_pixels'] == 176 * 220
    assert r['pl080']['channels'][5]['source'] == 0x4c012e80 + count


@pytest.mark.parametrize('count', [16, 32, 64, 11600])
def test_rgb666_partial_rejects_incomplete_triplets(tmp_path, count):
    steps = setup(mode=0xd230) + [w(0xf7000304, 0x320)] + dma(0, count)
    steps = [(op, a, 0xffff1c if a == 0xf70001fc else v, width)
             for op, a, v, width in steps]
    r = qemu_run(state(steps, bytes(count)), tmp_path/'bad-count', trace=False)
    assert r['stop']['reason'] == 'display_dma_pixel_format_unmodeled'
    assert r['pdi']['panel']['known_pixels'] == 0
    assert r['pl080']['raw_tc'] == 0


@pytest.mark.parametrize('variant,reason',[
    ('selector','pdi_dma_selector_unmodeled'),('program','pdi_dma_program_unmodeled'),
    ('count','pdi_dma_count_unmodeled'),('stream','display_dma_pixel_format_unmodeled'),
    ('mode','display_dma_pixel_format_unmodeled'),('unknown','display_dma_source_unknown'),
])
def test_rgb666_guards(tmp_path,variant,reason):
    s,_,_,_=rgb666_state(variant)
    r=qemu_run(s,tmp_path/variant,trace=False)
    assert r['stop']['reason']==reason,r['stop']
    assert r['pdi']['completed_frames']==0 and r['pl080']['raw_tc']==0
    if variant=='unknown':
        assert r['pdi']['frame']['bytes']==16
        assert r['pdi']['panel']['known_pixels']==5


@pytest.mark.parametrize('horizon,count',[('triplet',116160),('large-buffer',116160),('triplet',11616)])
def test_rgb666_native_checkpoint_keeps_triplet_phase_and_large_raw_buffer(tmp_path,horizon,count):
    s,_,_,before=rgb666_state(count=count,cursor=0xc400 if count==11616 else 0)
    s['record_diagnostics']=False
    prefix=before if horizon=='triplet' else 5500
    first=copy.deepcopy(s);first['instruction_limit']=prefix
    checkpoint=tmp_path/'display.vmstate'
    with engine(first,tmp_path/'prefix') as vm:
        initial=stopped(vm)
        n=initial['pdi']['frame']['bytes']
        assert 0<n<count and initial['pdi']['completed_frames']==0
        if horizon=='triplet':assert n%3!=0
        else:assert n>77440
        migration(vm,'migrate',checkpoint)
    resumed=copy.deepcopy(s);resumed['instruction_limit']-=prefix
    with engine(resumed,tmp_path/'resumed',incoming=True) as vm:
        migration(vm,'migrate-incoming',checkpoint)
        final=stopped(vm)
    with engine(s,tmp_path/'whole') as vm:whole=stopped(vm)
    assert comparable(final)==comparable(whole)
