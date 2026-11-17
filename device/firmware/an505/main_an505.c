/* Emulator driver (EMULATED, QEMU mps2-an505). Same tagged records as host/main_host.c, over UART0, after one seed record:
 *   S <32 bytes>  (once, first: the TRNG seed; then the firmware boots)
 *   F <u16 len> <bytes> -> (R <u16 len> <bytes>)* E      P -> k (button ISR)      T <u32 ms> -> k      D -> D <summary 32> <next u64>
 * Only F bytes reach the firmware's transport input; P is the button interrupt, T the tick. */
#include <stdint.h>
#include "mas_core.h"
#include "an505.h"

int main(void) {
    uint8_t seed[32];
    uart_init();
    while (uart_getc() != 'S') {
    }
    for (int i = 0; i < 32; i++) seed[i] = (uint8_t)uart_getc();
    emu_hal_init(seed);
    mas_init();
    uart_putc('B'); /* booted */
    for (;;) {
        int tag = uart_getc();
        if (tag == 'F') {
            unsigned n = ((unsigned)uart_getc() << 8) | (unsigned)uart_getc();
            for (unsigned i = 0; i < n; i++) mas_rx_byte((uint8_t)uart_getc());
            uart_putc('E');
        } else if (tag == 'P') {
            mas_button_isr();
            uart_putc('k');
        } else if (tag == 'T') {
            uint32_t ms = 0;
            for (int i = 0; i < 4; i++) ms = (ms << 8) | (uint32_t)uart_getc();
            emu_tick_add(ms);
            uart_putc('k');
        } else if (tag == 'D') {
            uint8_t s[32];
            uint64_t next;
            emu_display_get(s, &next);
            uart_putc('D');
            for (int i = 0; i < 32; i++) uart_putc(s[i]);
            for (int i = 7; i >= 0; i--) uart_putc((int)(next >> (8 * i)) & 0xFF);
        }
    }
}
