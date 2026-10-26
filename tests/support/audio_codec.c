/* Exercise the production service with independently fragmented encoded input. */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include "db2010-audio-codec.h"

int main(int argc, char **argv)
{
    if (argc != 4 && argc != 5) { return 1; }
    DB2010AudioRuntime a = {0};
    a.source_rate = atoi(argv[1]);
    a.source_channels = atoi(argv[2]);
    if (argc == 5) { a.source_codec = atoi(argv[4]); }
    unsigned chunk = atoi(argv[3]);
    if (!chunk || chunk > 1588) { return 1; }
    while (!a.eof || a.input_bytes) {
        if (!a.eof && a.input_bytes < db2010_audio_frame_bytes(&a)) {
            unsigned n = fread(a.input + a.input_bytes, 1, chunk, stdin);
            a.input_bytes += n;
            a.eof = n < chunk;
        }
        unsigned before = a.input_bytes;
        const char *fault = db2010_audio_decode(&a);
        if (fault || (a.eof && a.input_bytes && before == a.input_bytes)) {
            fprintf(stderr, "%s\n", fault ? fault : "truncated");
            return 2;
        }
        if (fwrite(a.pcm, 4, a.pcm_frames, stdout) != a.pcm_frames) { return 1; }
        a.pcm_frames = 0;
    }
    /* EOF may coincide exactly with an encoded frame boundary. */
    const char *fault = db2010_audio_decode(&a);
    if (fault || fwrite(a.pcm, 4, a.pcm_frames, stdout) != a.pcm_frames) { return 2; }
    fprintf(stderr, "frames=%llu\n", (unsigned long long)a.frames_decoded);
    return 0;
}
