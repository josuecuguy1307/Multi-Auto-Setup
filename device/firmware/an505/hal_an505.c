/* HAL for QEMU's mps2-an505 (EMULATED). UART0 (CMSDK APB UART, secure alias) carries the same tagged test records as the host build;
 * flash is RAM with NOR semantics for the length of one emulator run; the tick and the button are driven by the test harness (they are
 * SysTick and a GPIO interrupt on the real board); the TRNG seed is sent by the harness before boot (there is no real entropy here). */
#include <string.h>
#include "mas_hal.h"
#include "an505.h"

#define UART_BASE 0x50200000u
#define UART_DATA (*(volatile uint32_t *)(UART_BASE + 0x00))
#define UART_STATE (*(volatile uint32_t *)(UART_BASE + 0x04))
#define UART_CTRL (*(volatile uint32_t *)(UART_BASE + 0x08))
#define UART_BAUDDIV (*(volatile uint32_t *)(UART_BASE + 0x10))
#define PAGES 3

static uint8_t flash[PAGES * MAS_PAGE_SIZE];
static uint64_t tick;
static uint8_t disp[32], seed[32];
static uint64_t disp_next;
static int seeded;

void uart_init(void) {
    UART_BAUDDIV = 16;
    UART_CTRL = 3; /* TX and RX enable */
}

int uart_getc(void) {
    while (!(UART_STATE & 2u)) {
    }
    return (int)(UART_DATA & 0xFFu);
}

void uart_putc(int c) {
    while (UART_STATE & 1u) {
    }
    UART_DATA = (uint32_t)(c & 0xFF);
}

void emu_hal_init(const uint8_t s[32]) {
    memset(flash, 0xFF, sizeof flash);
    memcpy(seed, s, 32);
    seeded = 1;
}

void emu_tick_add(uint32_t ms) { tick += ms; }

void emu_display_get(uint8_t summary[32], uint64_t *next) {
    memcpy(summary, disp, 32);
    *next = disp_next;
}

uint64_t hal_tick_ms(void) { return tick; }

void hal_tx(const uint8_t *buf, size_t len) {
    uart_putc('R');
    uart_putc((int)(len >> 8));
    uart_putc((int)len);
    for (size_t i = 0; i < len; i++) uart_putc(buf[i]);
}

void hal_display(const uint8_t summary[32], uint64_t next) {
    memcpy(disp, summary, 32);
    disp_next = next;
}

int hal_trng(uint8_t out[32]) {
    if (!seeded) return -1;
    memcpy(out, seed, 32);
    return 0;
}

const uint8_t *hal_flash_ptr(int page) { return flash + (size_t)page * MAS_PAGE_SIZE; }

int hal_flash_program(int page, uint32_t off, const uint8_t *buf, size_t len) {
    if (page < 0 || page >= PAGES || off + len > MAS_PAGE_SIZE) return -1;
    uint8_t *p = flash + (size_t)page * MAS_PAGE_SIZE + off;
    for (size_t i = 0; i < len; i++) p[i] &= buf[i];
    return 0;
}

int hal_flash_erase(int page) {
    if (page < 0 || page >= PAGES) return -1;
    memset(flash + (size_t)page * MAS_PAGE_SIZE, 0xFF, MAS_PAGE_SIZE);
    return 0;
}

void hal_irq_disable(void) { __asm volatile("cpsid i" ::: "memory"); }
void hal_irq_enable(void) { __asm volatile("cpsie i" ::: "memory"); }
