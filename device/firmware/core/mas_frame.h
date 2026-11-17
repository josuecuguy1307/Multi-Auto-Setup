/* Framing and hashing helpers: COBS, CRC-16/CCITT-FALSE, SHA-256 (Monocypher has no SHA-256; key_id = sha256(pub)[:8]). */
#ifndef MAS_FRAME_H
#define MAS_FRAME_H

#include <stddef.h>
#include <stdint.h>

void mas_sha256(const uint8_t *data, size_t n, uint8_t out[32]);
uint16_t mas_crc16(const uint8_t *data, size_t n);
size_t mas_cobs_encode(const uint8_t *in, size_t n, uint8_t *out);          /* out needs n + n/254 + 1 bytes; no terminator */
int mas_cobs_decode(const uint8_t *in, size_t n, uint8_t *out, size_t cap);  /* -1 on malformed input or overflow */

#endif
