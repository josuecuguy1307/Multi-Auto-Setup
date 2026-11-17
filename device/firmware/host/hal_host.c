/* Host HAL mock (SIMULATED). Flash = a file of 3 pages with NOR semantics (program ANDs bits; erase sets 0xFF), written through per byte.
 * TRNG = 32 bytes from MAS_TRNG_FILE. Tick and button are driven by the harness.
 * Fault injection for the power-cut sweep:
 *   MAS_CUT_AFTER=k        exit(77) when flash step k+1 would start (each programmed byte is a step, each erase is a step)
 *   MAS_CUT_ERASE=0|1|2    what a cut erase leaves: 0 untouched, 1 fully erased, 2 deterministic garbage
 *   MAS_FLASH_FAIL_AFTER=k program calls fail (write nothing) once k bytes have been programmed
 *   MAS_FLASH_STUCK=1      program calls report success but no bit changes (tests the read-back)
 *   MAS_FLASH_FAIL_CALLS=n the first n program calls fail (write nothing); later calls work
 *   MAS_ERASE_STUCK=1      erase calls report success but the page does not change (tests the erase verify)
 *   MAS_PRESS_IN_DISPLAY=1 the button is pressed once while the next summary is being drawn */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "mas_hal.h"
#include "hal_host.h"
#include "mas_core.h"

#define PAGES 3
static uint8_t flash[PAGES * MAS_PAGE_SIZE];
static FILE *ff;
static uint64_t tick;
static long cut_after = -1, steps, fail_after = -1, programmed;
static int erase_mode, stuck, erase_stuck, press_in_display;
static long fail_calls;
static uint8_t disp[32];
static uint64_t disp_next;

static long env_long(const char *name, long dflt) {
    const char *v = getenv(name);
    return (v && *v) ? strtol(v, NULL, 10) : dflt;
}

void host_hal_init(void) {
    const char *path = getenv("MAS_FLASH_FILE");
    if (!path) exit(3);
    memset(flash, 0xFF, sizeof flash);
    ff = fopen(path, "r+b");
    if (ff) {
        size_t got = fread(flash, 1, sizeof flash, ff);
        (void)got;
    } else {
        ff = fopen(path, "w+b");
        if (!ff) exit(3);
    }
    fseek(ff, 0, SEEK_SET);
    if (fwrite(flash, 1, sizeof flash, ff) != sizeof flash) exit(3);
    fflush(ff);
    cut_after = env_long("MAS_CUT_AFTER", -1);
    erase_mode = (int)env_long("MAS_CUT_ERASE", 0);
    fail_after = env_long("MAS_FLASH_FAIL_AFTER", -1);
    stuck = (int)env_long("MAS_FLASH_STUCK", 0);
    fail_calls = env_long("MAS_FLASH_FAIL_CALLS", 0);
    erase_stuck = (int)env_long("MAS_ERASE_STUCK", 0);
    press_in_display = (int)env_long("MAS_PRESS_IN_DISPLAY", 0);
}

static void persist(size_t off, size_t len) {
    fseek(ff, (long)off, SEEK_SET);
    fwrite(flash + off, 1, len, ff);
    fflush(ff);
}

static void step(void) {
    if (cut_after >= 0 && steps >= cut_after) {
        fflush(stdout);
        _Exit(77);
    }
    steps++;
}

uint64_t hal_tick_ms(void) { return tick; }
void host_tick_add(uint32_t ms) { tick += ms; }

void hal_tx(const uint8_t *buf, size_t len) {
    putchar('R');
    putchar((int)(len >> 8) & 0xFF);
    putchar((int)len & 0xFF);
    fwrite(buf, 1, len, stdout);
    fflush(stdout); /* out the door immediately: a later power cut cannot take a released token back */
}

void hal_display(const uint8_t summary[32], uint64_t next) {
    static const uint8_t blank[32];
    if (press_in_display && memcmp(summary, blank, 32) != 0) { /* the button ISR fires while the screen is being redrawn */
        press_in_display = 0;
        mas_button_isr();
    }
    memcpy(disp, summary, 32);
    disp_next = next;
}

void host_display_get(uint8_t summary[32], uint64_t *next) {
    memcpy(summary, disp, 32);
    *next = disp_next;
}

int hal_trng(uint8_t out[32]) {
    const char *path = getenv("MAS_TRNG_FILE");
    FILE *f = path ? fopen(path, "rb") : NULL;
    if (!f) return -1;
    size_t n = fread(out, 1, 32, f);
    fclose(f);
    return n == 32 ? 0 : -1;
}

const uint8_t *hal_flash_ptr(int page) { return flash + (size_t)page * MAS_PAGE_SIZE; }

int hal_flash_program(int page, uint32_t off, const uint8_t *buf, size_t len) {
    if (page < 0 || page >= PAGES || off + len > MAS_PAGE_SIZE) return -1;
    size_t base = (size_t)page * MAS_PAGE_SIZE + off;
    if (fail_calls > 0) {
        fail_calls--;
        return -1;
    }
    for (size_t i = 0; i < len; i++) {
        if (fail_after >= 0 && programmed >= fail_after) return -1;
        step();
        if (stuck) continue; /* reports success, bits do not change */
        flash[base + i] &= buf[i];
        persist(base + i, 1);
        programmed++;
    }
    return 0;
}

int hal_flash_erase(int page) {
    if (page < 0 || page >= PAGES) return -1;
    size_t base = (size_t)page * MAS_PAGE_SIZE;
    if (cut_after >= 0 && steps >= cut_after) {
        if (erase_mode == 1) memset(flash + base, 0xFF, MAS_PAGE_SIZE);
        if (erase_mode == 2) {
            uint32_t x = 0x12345678u;
            for (size_t i = 0; i < MAS_PAGE_SIZE; i++) {
                x = x * 1103515245u + 12345u;
                flash[base + i] = (uint8_t)(x >> 16);
            }
        }
        persist(base, MAS_PAGE_SIZE);
    }
    step();
    if (erase_stuck) return 0; /* reports success, page unchanged */
    memset(flash + base, 0xFF, MAS_PAGE_SIZE);
    persist(base, MAS_PAGE_SIZE);
    return 0;
}

void hal_irq_disable(void) {}
void hal_irq_enable(void) {}
