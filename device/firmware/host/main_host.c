/* Host driver for the key firmware (SIMULATED). Reads tagged records on stdin; only F bytes reach the firmware's transport input.
 *   F <u16 len> <bytes> -> (R <u16 len> <bytes>)* E      P -> k (button ISR)      T <u32 ms> -> k      D -> D <summary 32> <next u64> */
#include <stdio.h>
#include "mas_core.h"
#include "hal_host.h"
#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#endif

static int get(void) { return getchar(); }

int main(void) {
#ifdef _WIN32
    _setmode(_fileno(stdin), _O_BINARY);
    _setmode(_fileno(stdout), _O_BINARY);
#endif
    host_hal_init();
    mas_init();
    for (;;) {
        int tag = get();
        if (tag == EOF) return 0;
        if (tag == 'F') {
            int a = get(), b = get();
            if (b == EOF) return 0;
            unsigned n = ((unsigned)a << 8) | (unsigned)b;
            for (unsigned i = 0; i < n; i++) {
                int c = get();
                if (c == EOF) return 0;
                mas_rx_byte((uint8_t)c);
            }
            putchar('E');
        } else if (tag == 'P') {
            mas_button_isr();
            putchar('k');
        } else if (tag == 'T') {
            uint32_t ms = 0;
            for (int i = 0; i < 4; i++) {
                int c = get();
                if (c == EOF) return 0;
                ms = (ms << 8) | (uint32_t)c;
            }
            host_tick_add(ms);
            putchar('k');
        } else if (tag == 'D') {
            uint8_t s[32];
            uint64_t next;
            host_display_get(s, &next);
            putchar('D');
            fwrite(s, 1, 32, stdout);
            for (int i = 7; i >= 0; i--) putchar((int)(next >> (8 * i)) & 0xFF);
        } else {
            return 2;
        }
        fflush(stdout);
    }
}
