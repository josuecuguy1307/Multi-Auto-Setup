/* Host-only NV harness (SIMULATED): drives mas_nv_* directly so each guard of the counter log can be tested on its own.
 * One command per stdin line, one answer line each:
 *   init                 -> "<rc> <counter>"
 *   commit <n>           -> "<rc>"
 *   counter              -> "<counter>"
 *   poke <page> <off> <hex>   program raw bytes through the HAL (test setup only; never part of the firmware) -> "<rc>"
 * Flash and fault modes come from the host HAL's environment variables (see host/hal_host.c). */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "mas_hal.h"
#include "mas_nv.h"
#include "../host/hal_host.h"

int main(void) {
    char line[4096];
    host_hal_init();
    while (fgets(line, sizeof line, stdin)) {
        char cmd[16] = {0};
        if (sscanf(line, "%15s", cmd) != 1) continue;
        if (!strcmp(cmd, "init")) {
            int rc = mas_nv_init();
            printf("%d %llu\n", rc, (unsigned long long)mas_nv_counter());
        } else if (!strcmp(cmd, "commit")) {
            unsigned long long n = 0;
            sscanf(line + 6, "%llu", &n);
            printf("%d\n", mas_nv_commit((uint64_t)n));
        } else if (!strcmp(cmd, "counter")) {
            printf("%llu\n", (unsigned long long)mas_nv_counter());
        } else if (!strcmp(cmd, "poke")) {
            int page = 0;
            unsigned off = 0;
            char hex[2048] = {0};
            uint8_t buf[1024];
            size_t n = 0;
            if (sscanf(line + 4, "%d %u %2047s", &page, &off, hex) != 3) { printf("ERR\n"); continue; }
            for (; hex[2 * n] && hex[2 * n + 1] && n < sizeof buf; n++) {
                unsigned v;
                sscanf(hex + 2 * n, "%2x", &v);
                buf[n] = (uint8_t)v;
            }
            printf("%d\n", hal_flash_program(page, off, buf, n));
        } else {
            printf("ERR\n");
        }
        fflush(stdout);
    }
    return 0;
}
