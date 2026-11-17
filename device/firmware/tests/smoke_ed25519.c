/* Smoke check: Monocypher's optional Ed25519 (SHA-512) agrees with RFC 8032 as implemented by Python's `cryptography`.
 * Prints: <public key hex> <signature hex> <check result>. Driven by device/tests/test_firmware_host.py. */
#include <stdio.h>
#include <string.h>
#include "monocypher-ed25519.h"

int main(void) {
    uint8_t seed[32], sk[64], pk[32], sig[64];
    const uint8_t msg[3] = {1, 2, 3};
    memset(seed, 7, sizeof seed);
    crypto_ed25519_key_pair(sk, pk, seed);
    crypto_ed25519_sign(sig, sk, msg, sizeof msg);
    for (int i = 0; i < 32; i++) printf("%02x", pk[i]);
    printf(" ");
    for (int i = 0; i < 64; i++) printf("%02x", sig[i]);
    printf(" %d\n", crypto_ed25519_check(sig, pk, msg, sizeof msg));
    return 0;
}
