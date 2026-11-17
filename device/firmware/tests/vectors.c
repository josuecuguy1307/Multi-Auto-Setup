/* Host-only vector tool for the firmware's own primitives. Reads one hex string on stdin, prints one hex result (or ERR).
 *   vectors sha256 | crc16 | cobs_enc | cobs_dec
 * Driven by device/tests/test_firmware_host.py against hashlib, binascii.crc_hqx and an independent Python COBS. */
#include <stdio.h>
#include <string.h>
#include "mas_frame.h"

static uint8_t in[8192], out[8192 + 64];

static int unhex(const char *s, size_t *n) {
    size_t len = strlen(s), i;
    while (len && (s[len - 1] == '\n' || s[len - 1] == '\r')) len--;
    if (len % 2 || len / 2 > sizeof in) return -1;
    for (i = 0; i < len / 2; i++) {
        unsigned v;
        if (sscanf(s + 2 * i, "%2x", &v) != 1) return -1;
        in[i] = (uint8_t)v;
    }
    *n = len / 2;
    return 0;
}

static void hex(const uint8_t *b, size_t n) {
    for (size_t i = 0; i < n; i++) printf("%02x", b[i]);
    printf("\n");
}

int main(int argc, char **argv) {
    static char line[2 * sizeof in + 8];
    size_t n = 0;
    if (argc != 2 || !fgets(line, sizeof line, stdin) || unhex(line, &n)) { printf("ERR\n"); return 2; }
    if (!strcmp(argv[1], "sha256")) { mas_sha256(in, n, out); hex(out, 32); }
    else if (!strcmp(argv[1], "crc16")) { uint16_t c = mas_crc16(in, n); out[0] = (uint8_t)(c >> 8); out[1] = (uint8_t)c; hex(out, 2); }
    else if (!strcmp(argv[1], "cobs_enc")) { hex(out, mas_cobs_encode(in, n, out)); }
    else if (!strcmp(argv[1], "cobs_dec")) { int r = mas_cobs_decode(in, n, out, sizeof out); if (r < 0) printf("ERR\n"); else hex(out, (size_t)r); }
    else { printf("ERR\n"); return 2; }
    return 0;
}
