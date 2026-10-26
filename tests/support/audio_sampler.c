/* Exercise the compiled sampler with known stereo values and virtual time. */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include "db2010-audio-codec.h"
static unsigned packets, frames;
static void receive(void *opaque, const int16_t *pcm, unsigned count,
                    uint64_t sequence, uint64_t first, uint64_t ns, bool gap, bool eos)
{
    if (count != 960 || sequence != packets || first != frames || ns != packets * 20000000ULL || eos) abort();
    for (unsigned i = 0; i < count; i++) {
        if (pcm[2*i] != (int16_t)(frames+i) || pcm[2*i+1] != (int16_t)~(frames+i)) abort();
    }
    frames += count; packets++;
}
int main(void)
{
    DB2010AudioRuntime a = {.active = true};
    DB2010AudioSink sink = {NULL, receive};
    for (unsigned block = 0; block < 42; block++) {
        a.pcm_frames = 1152; a.due = (block+1)*24000000ULL;
        for (unsigned i = 0; i < 1152; i++) {
            a.pcm[2*i] = (int16_t)(block*1152+i);
            a.pcm[2*i+1] = (int16_t)~(block*1152+i);
        }
        int64_t due;
        while ((due=db2010_audio_sample_deadline(&a)) <= 1000000000 && a.samples.emitted < 1152) {
            db2010_audio_sample_advance(&a, &sink, due);
        }
        if (a.samples.emitted < 1152) break;
        a.frames_played += 1152; a.samples.emitted = 0;
    }
    if (packets != 50 || frames != 48000 || a.samples.frames) abort();
    printf("50 packets, 48000 stereo frames at exactly 20 ms virtual intervals\n");
}
