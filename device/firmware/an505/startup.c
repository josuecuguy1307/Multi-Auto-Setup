/* Minimal Cortex-M33 startup for QEMU's mps2-an505 (EMULATED). Runs in the secure world from SSRAM at 0x10000000; no FPU, no heap.
 * Provides the C library pieces the freestanding build needs (memcpy, memset, memmove, memcmp). */
#include <stddef.h>
#include <stdint.h>

extern uint32_t _estack, _sdata, _edata, _sidata, _sbss, _ebss;
int main(void);

void Reset_Handler(void) {
    uint32_t *s = &_sidata, *d = &_sdata;
    while (d < &_edata) *d++ = *s++;
    for (d = &_sbss; d < &_ebss;) *d++ = 0;
    main();
    for (;;) {
    }
}

static void Default_Handler(void) {
    for (;;) {
    }
}

__attribute__((section(".vectors"), used)) const void *const vectors[16] = {
    &_estack, (const void *)Reset_Handler, (const void *)Default_Handler, (const void *)Default_Handler,
    (const void *)Default_Handler, (const void *)Default_Handler, (const void *)Default_Handler, (const void *)Default_Handler,
    0, 0, 0, 0, (const void *)Default_Handler, 0, (const void *)Default_Handler, (const void *)Default_Handler};

void *memcpy(void *dst, const void *src, size_t n) {
    uint8_t *d = dst;
    const uint8_t *s = src;
    while (n--) *d++ = *s++;
    return dst;
}

void *memmove(void *dst, const void *src, size_t n) {
    uint8_t *d = dst;
    const uint8_t *s = src;
    if (d < s) {
        while (n--) *d++ = *s++;
    } else {
        while (n--) d[n] = s[n];
    }
    return dst;
}

void *memset(void *dst, int c, size_t n) {
    uint8_t *d = dst;
    while (n--) *d++ = (uint8_t)c;
    return dst;
}

int memcmp(const void *a, const void *b, size_t n) {
    const uint8_t *x = a, *y = b;
    for (; n; n--, x++, y++)
        if (*x != *y) return *x < *y ? -1 : 1;
    return 0;
}
