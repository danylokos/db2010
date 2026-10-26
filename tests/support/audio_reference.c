/* Independent whole-file decode, without the service's framing/converter. */
#define MINIMP3_IMPLEMENTATION
#define MINIMP3_ONLY_MP3
#define MINIMP3_NO_SIMD
#include <stdio.h>
#include <stdlib.h>
#include "db2010-minimp3/minimp3.h"

int main(int argc, char **argv)
{
    if (argc != 4) { return 1; }
    FILE *f = fopen(argv[1], "rb");
    if (!f) { return 1; }
    fseek(f, 0, SEEK_END); long length = ftell(f); rewind(f);
    unsigned char *data = malloc(length);
    if (fread(data, 1, length, f) != length) { return 1; }
    mp3dec_t decoder = {0};
    for (long i = 0; i < length;) {
        mp3dec_frame_info_t info = {0}; short pcm[2304];
        int n = mp3dec_decode_frame(&decoder, data + i, length - i, pcm, &info);
        if (!n || info.frame_offset || info.hz != atoi(argv[2]) ||
            info.channels != atoi(argv[3]) || info.layer != 3) { return 2; }
        if (fwrite(pcm, 2 * info.channels, n, stdout) != n) { return 3; }
        i += info.frame_bytes;
    }
    free(data); fclose(f); return 0;
}
