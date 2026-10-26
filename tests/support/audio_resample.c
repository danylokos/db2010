/* Drive the production converter with arbitrary input chunk boundaries. */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include "db2010-audio-resample.h"
int main(int argc, char **argv)
{
    if (argc != 2 && argc != 3) return 1;
    unsigned rate = argc == 3 ? atoi(argv[2]) : 44100;
    if (!db2010_audio_rate_supported(rate)) return 1;
    unsigned chunk = atoi(argv[1]);
    if (!chunk || chunk > 1152) return 1;
    int16_t input[2304], output[13824];
    DB2010AudioResampler r = {0};
    unsigned count;
    while ((count = fread(input, 4, chunk, stdin))) {
        unsigned n = db2010_audio_resample(&r, input, count, output, false, rate);
        if (fwrite(output, 4, n, stdout) != n) return 1;
    }
    count = db2010_audio_resample(&r, NULL, 0, output, true, rate);
    if (fwrite(output, 4, count, stdout) != count) return 1;
    return 0;
}
