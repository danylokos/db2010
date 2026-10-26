"""Observed startup MP3 setup; all other modes and data stay guarded."""
from support.mma_ring_fixtures import make as ring_state, stages
from support.fa_wire10_fixtures import frame

PROFILE = 'wire20-modexp1024-mma-setup-peer'
SETUP = (0x0e00, 8, 1, 2, 1, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
         0xffff, 0xffff, 0xffff, 0, 0, 1, 0, 0xcc02, 18)


def make(steps, latency=512):
    state = ring_state(steps, latency)
    state['dsp_peer']['profile'] = PROFILE
    return state


def setup(words=SETUP):
    return frame(words)
