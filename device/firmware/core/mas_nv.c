/* Counter log and key page (DECISIONS A-2 §3, §4).
 *
 * Counter: pages A and B hold append-only 16-byte records `counter u64 LE | ~counter u64 LE`. NOR programming only clears bits, so a torn
 * record fails the complement check. Boot takes the maximum valid record of both pages. A commit writes the slot after the last
 * non-erased slot of the active page (never a non-erased slot); when the page is full it erases the other page and writes there, so the
 * maximum survives in the old page throughout the erase. A commit is durable only after it has been read back.
 *
 * Key page: seed[32] | pub[32] | crc16 BE. Never written by any opcode.
 * NOT VERIFIED on hardware: STM32U5 flash ECC behaviour on a torn word, program granularity (records are 16-byte aligned to be safe). */
#include <string.h>
#include "mas_nv.h"
#include "mas_hal.h"
#include "mas_frame.h"

#define REC 16u
#define SLOTS (MAS_PAGE_SIZE / REC)
#define KEY_LEN 66u

static uint64_t cur;
static int any, active;
static uint32_t next_slot;

static uint64_t rd64(const uint8_t *p) {
    uint64_t v = 0;
    for (int i = 7; i >= 0; i--) v = (v << 8) | p[i];
    return v;
}

static void wr64(uint8_t *p, uint64_t v) {
    for (int i = 0; i < 8; i++) p[i] = (uint8_t)(v >> (8 * i));
}

static int all_ff(const uint8_t *p, uint32_t n) {
    for (uint32_t i = 0; i < n; i++)
        if (p[i] != 0xFF) return 0;
    return 1;
}

int mas_nv_init(void) {
    static const int pages[2] = {MAS_PAGE_CTR_A, MAS_PAGE_CTR_B};
    cur = 0;
    any = 0;
    active = MAS_PAGE_CTR_A;
    for (int k = 0; k < 2; k++) {
        const uint8_t *p = hal_flash_ptr(pages[k]);
        for (uint32_t s = 0; s < SLOTS; s++) {
            uint64_t v = rd64(p + s * REC);
            if (rd64(p + s * REC + 8) == (uint64_t)~v && (!any || v > cur)) {
                cur = v;
                any = 1;
                active = pages[k];
            }
        }
    }
    const uint8_t *p = hal_flash_ptr(active);
    next_slot = 0;
    for (uint32_t s = SLOTS; s > 0; s--)
        if (!all_ff(p + (s - 1) * REC, REC)) {
            next_slot = s;
            break;
        }
    return 0;
}

uint64_t mas_nv_counter(void) { return cur; }

int mas_nv_log_empty(void) { return !any; }

int mas_nv_commit(uint64_t c) {
    uint8_t rec[REC];
    if (c == 0 || c == UINT64_MAX || (any && c <= cur)) return -1;
    wr64(rec, c);
    wr64(rec + 8, ~c);
    int page = active;
    uint32_t slot = next_slot;
    if (slot >= SLOTS) {
        page = (active == MAS_PAGE_CTR_A) ? MAS_PAGE_CTR_B : MAS_PAGE_CTR_A;
        if (hal_flash_erase(page) != 0) return -1;
        slot = 0;
    }
    active = page;
    next_slot = slot + 1; /* even a failed write leaves this slot used: it is never programmed twice */
    if (!all_ff(hal_flash_ptr(page) + slot * REC, REC)) return -1;
    if (hal_flash_program(page, slot * REC, rec, REC) != 0) return -1;
    if (memcmp(hal_flash_ptr(page) + slot * REC, rec, REC) != 0) return -1;
    cur = c;
    any = 1;
    return 0;
}

int mas_nv_key(uint8_t seed[32], uint8_t pub[32]) {
    const uint8_t *p = hal_flash_ptr(MAS_PAGE_KEY);
    if (all_ff(p, KEY_LEN)) return 1;
    uint16_t c = mas_crc16(p, 64);
    if (p[64] != (uint8_t)(c >> 8) || p[65] != (uint8_t)c) return -1;
    memcpy(seed, p, 32);
    memcpy(pub, p + 32, 32);
    return 0;
}

int mas_nv_write_key(const uint8_t seed[32], const uint8_t pub[32]) {
    uint8_t buf[KEY_LEN];
    memcpy(buf, seed, 32);
    memcpy(buf + 32, pub, 32);
    uint16_t c = mas_crc16(buf, 64);
    buf[64] = (uint8_t)(c >> 8);
    buf[65] = (uint8_t)c;
    int r = -1;
    if (all_ff(hal_flash_ptr(MAS_PAGE_KEY), KEY_LEN) && hal_flash_program(MAS_PAGE_KEY, 0, buf, KEY_LEN) == 0 &&
        memcmp(hal_flash_ptr(MAS_PAGE_KEY), buf, KEY_LEN) == 0)
        r = 0;
    memset(buf, 0, sizeof buf);
    return r;
}

int mas_nv_erase_key(void) { return hal_flash_erase(MAS_PAGE_KEY); }
