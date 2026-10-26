/* Exercise the production renderer without firmware or a host audio device. */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include "db2010-midi-synth.h"
int main(void)
{
    DB2010MidiSynth s = {0};
    db2010_midi_synth_init(&s);
    char op;
    while (scanf(" %c", &op) == 1) {
        if (op == 'e') {
            unsigned status, a, b;
            if (scanf("%u %u %u", &status, &a, &b) != 3) { return 1; }
            const char *fault = db2010_midi_event(&s, status, a, b);
            if (fault) { fprintf(stderr, "%s\n", fault); return 2; }
        } else if (op == 'r') {
            unsigned n;
            if (scanf("%u", &n) != 1) { return 1; }
            while (n) {
                unsigned count = n > 960 ? 960 : n;
                int16_t pcm[1920];
                db2010_midi_render(&s, pcm, count);
                if (fwrite(pcm, 4, count, stdout) != count) { return 1; }
                n -= count;
            }
        } else if (op == 's') {
            fprintf(stderr, "%llu %llu", (unsigned long long)s.notes,
                    (unsigned long long)s.stolen);
            for (unsigned i = 0; i < DB2010_MIDI_VOICES; i++) {
                DB2010MidiVoice *v = &s.voices[i];
                if (v->velocity) {
                    fprintf(stderr, " %u:%u:%u:%u", i, v->channel, v->note, v->held);
                }
            }
            fputc('\n', stderr);
        } else { return 1; }
    }
    return 0;
}
