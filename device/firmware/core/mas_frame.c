#include "mas_frame.h"

uint16_t mas_crc16(const uint8_t *data, size_t n) {
    uint16_t c = 0xFFFF;
    for (size_t i = 0; i < n; i++) {
        c ^= (uint16_t)((uint16_t)data[i] << 8);
        for (int b = 0; b < 8; b++)
            c = (c & 0x8000u) ? (uint16_t)((c << 1) ^ 0x1021u) : (uint16_t)(c << 1);
    }
    return c;
}

size_t mas_cobs_encode(const uint8_t *in, size_t n, uint8_t *out) {
    size_t o = 1, code_at = 0;
    uint8_t code = 1;
    for (size_t i = 0; i < n; i++) {
        if (in[i] == 0) {
            out[code_at] = code;
            code_at = o++;
            code = 1;
        } else {
            out[o++] = in[i];
            if (++code == 0xFF) {
                out[code_at] = code;
                code_at = o++;
                code = 1;
            }
        }
    }
    out[code_at] = code;
    return o;
}

int mas_cobs_decode(const uint8_t *in, size_t n, uint8_t *out, size_t cap) {
    size_t i = 0, o = 0;
    while (i < n) {
        size_t code = in[i];
        if (code == 0 || i + code > n) return -1;
        for (size_t j = i + 1; j < i + code; j++) {
            if (in[j] == 0 || o >= cap) return -1;
            out[o++] = in[j];
        }
        i += code;
        if (code < 0xFF && i < n) {
            if (o >= cap) return -1;
            out[o++] = 0;
        }
    }
    return (int)o;
}
