#ifndef AN505_H
#define AN505_H
#include <stdint.h>

void uart_init(void);
int uart_getc(void);
void uart_putc(int c);
void emu_hal_init(const uint8_t seed[32]);
void emu_tick_add(uint32_t ms);
void emu_display_get(uint8_t summary[32], uint64_t *next);

#endif
