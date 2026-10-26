/* Continuous upstream decoder versus bounded-state adapter; no firmware calls.
 * Code from FAAD2 is copyright (c) Nero AG, www.nero.com
 * SPDX-License-Identifier: GPL-2.0-or-later */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "db2010-aac-lc.h"
#include "db2010-faad2/libfaad/common.h"
#include "db2010-faad2/libfaad/structs.h"

int main(int argc, char **argv)
{
    if (argc != 3) { return 2; }
    FILE *input = fopen(argv[1], "rb"), *output = fopen(argv[2], "wb");
    if (!input || !output) { return 3; }
    NeAACDecStruct *decoder = NeAACDecOpen();
    unsigned char asc[] = {0x14, 0x10}, channels = 0;
    unsigned long rate = 0;
    if (NeAACDecInit2(decoder, asc, sizeof(asc), &rate, &channels) ||
        rate != 16000 || channels != 2) { return 4; }
    decoder->frame = 1; /* Same explicit retain-priming policy as native service. */
    DB2010AacLc bounded = {0};
    unsigned frames = 0;
    unsigned char length[4], raw[4096];
    while (fread(length, 1, 4, input) == 4) {
        unsigned bytes = length[0] | length[1] << 8 | length[2] << 16 | length[3] << 24;
        if (!bytes || bytes > sizeof(raw) || fread(raw, 1, bytes, input) != bytes) { return 5; }
        NeAACDecFrameInfo info = {0};
        const int16_t *pcm = NeAACDecDecode(decoder, &info, raw, bytes);
        int16_t recreated[2048];
        if (!pcm || info.error || info.samples != 2048 || info.bytesconsumed != bytes ||
            !db2010_aac_lc_decode(&bounded, raw, bytes, recreated) ||
            memcmp(pcm, recreated, sizeof(recreated))) {
            fprintf(stderr, "AAC frame%u differs; error%u bytes%lu/%u samples%lu\n",
                    frames, info.error, info.bytesconsumed, bytes, info.samples);
            return 6;
        }
        /* Portable little-endian reference output. */
        for (unsigned i = 0; i < 2048; i++) {
            fputc((uint16_t)pcm[i] & 255, output);
            fputc((uint16_t)pcm[i] >> 8, output);
        }
        frames++;
    }
    NeAACDecClose(decoder);
    fclose(input); fclose(output);
    printf("AAC access units%u; every bounded-state output equals continuous decode\n", frames);
    return 0;
}
