/* MAS key firmware core (DECISIONS D-004, D-007, A-2). The approval state machine, all static memory.
 *
 * One request at a time. REQUEST shows the summary and the next counter and forgets any press latched before it. SIGN releases a token
 * only if a physical press arrived after that request and inside its press window (measured at the press, on the key's own tick); the
 * counter is committed to flash and read back before the signature is computed. One press, one signature.
 *
 * Token = body[177] | Ed25519(body)[64], body = "MAS-APPROVE-v1\0" | key_id[8] | counter u64 BE | request[146] (approval.py pack_body).
 * SIMULATED in the host build (HAL mocks). QEMU/Renode runs are planned (P4-FW-EMU, not built yet); nothing has run on real hardware.
 * Fail closed: any failed counter commit latches a fault until reboot; an erased or inconsistent key page after the key has signed is a
 * fault, never a new identity. */
#include <string.h>
#include "mas_core.h"
#include "mas_hal.h"
#include "mas_frame.h"
#include "mas_nv.h"
#include "monocypher.h"
#include "monocypher-ed25519.h"

#define RX_CAP 160u
#define REQ_LEN 146u
#define BODY_LEN 177u
#define TOKEN_LEN 241u
#define SUMMARY_AT 112u
#define CLS_AT 144u
#define PW_AT 145u
#define MAX_WINDOW_S 30u

static const uint8_t DOMAIN[15] = "MAS-APPROVE-v1";
static const uint8_t ZERO32[32];

static uint8_t rx[RX_CAP], dec[RX_CAP];
static size_t rx_n;
static int rx_overflow;
static uint8_t tx_plain[1 + TOKEN_LEN + 2], tx_enc[1 + TOKEN_LEN + 2 + 4];
static uint8_t sk[64], pub[32], key_id[8];
static int fault;
static uint32_t seq;
static struct {
    int valid;
    uint32_t seq;
    uint64_t t_req;
    uint8_t payload[REQ_LEN];
} pending;
static volatile int press_latched;
static volatile uint64_t press_t;

static void wr_be(uint8_t *p, uint64_t v, int n) {
    for (int i = 0; i < n; i++) p[i] = (uint8_t)(v >> (8 * (n - 1 - i)));
}

static void reply(uint8_t status, const uint8_t *p, size_t n) {
    tx_plain[0] = status;
    if (n) memcpy(tx_plain + 1, p, n);
    uint16_t c = mas_crc16(tx_plain, n + 1);
    tx_plain[n + 1] = (uint8_t)(c >> 8);
    tx_plain[n + 2] = (uint8_t)c;
    size_t m = mas_cobs_encode(tx_plain, n + 3, tx_enc);
    tx_enc[m++] = 0;
    hal_tx(tx_enc, m);
}

static int printable(const uint8_t *s) {
    for (int i = 0; i < 32; i++)
        if (s[i] < 0x20 || s[i] > 0x7E) return 0;
    return 1;
}

void mas_init(void) {
    uint8_t seed[32], tmp[32], chk[32], h[32];
    rx_n = 0;
    rx_overflow = 0;
    pending.valid = 0;
    press_latched = 0;
    seq = 0;
    fault = 0;
    hal_display(ZERO32, 0);
    if (mas_nv_init() != 0) { fault = 1; return; }
    int k = mas_nv_key(seed, pub);
    if (k == 0) {
        /* A CRC can pass on a torn write (CRC bytes still FF FF over a page whose CRC happens to be 0xFFFF): the seed must also derive the
         * stored public key, or the page counts as corrupted. */
        memcpy(tmp, seed, 32);
        crypto_ed25519_key_pair(sk, chk, tmp);
        if (memcmp(chk, pub, 32) != 0) k = -1;
    }
    if (k != 0) {
        /* A corrupted key page is replaced only if this key never signed anything (empty counter log): a power cut during first-boot
         * key generation. Once a counter exists, the identity is never silently changed. */
        if (!mas_nv_log_empty()) { fault = 1; return; }
        if (k < 0 && mas_nv_erase_key() != 0) { fault = 1; return; }
        if (hal_trng(seed) != 0) { fault = 1; return; }
        memcpy(tmp, seed, 32);
        crypto_ed25519_key_pair(sk, pub, tmp); /* wipes tmp */
        if (mas_nv_write_key(seed, pub) != 0) { crypto_wipe(seed, 32); fault = 1; return; }
    }
    memcpy(tmp, seed, 32);
    crypto_ed25519_key_pair(sk, chk, tmp);
    crypto_wipe(seed, 32);
    if (memcmp(chk, pub, 32) != 0) { fault = 1; return; }
    mas_sha256(pub, 32, h);
    memcpy(key_id, h, 8);
}

void mas_button_isr(void) {
    press_t = hal_tick_ms();
    press_latched = 1;
}

static void op_info(size_t n) {
    uint8_t out[49];
    if (n) { reply(ST_MALFORMED, 0, 0); return; }
    out[0] = 1;
    memcpy(out + 1, key_id, 8);
    memcpy(out + 9, pub, 32);
    wr_be(out + 41, mas_nv_counter(), 8);
    reply(ST_OK, out, sizeof out);
}

static void op_request(const uint8_t *p, size_t n) {
    uint8_t out[12];
    if (n != REQ_LEN) { reply(ST_MALFORMED, 0, 0); return; }
    if (mas_nv_counter() >= UINT64_MAX - 1) { reply(ST_NV_FAULT, 0, 0); return; } /* no next counter to sign: never show "#0" */
    if (!printable(p + 112)) /* SABOTEUR-ANCHOR: printable */ { reply(ST_SUMMARY_NOT_PRINTABLE, 0, 0); return; }
    if (p[CLS_AT] < 1 || p[CLS_AT] > 7 || p[PW_AT] == 0) { reply(ST_MALFORMED, 0, 0); return; }
    seq++;
    hal_irq_disable();
    press_latched = 0; /* SABOTEUR-ANCHOR: latch clear */
    hal_irq_enable();
    pending.valid = 1;
    pending.seq = seq;
    pending.t_req = hal_tick_ms();
    memcpy(pending.payload, p, REQ_LEN);
    hal_display(p + SUMMARY_AT, mas_nv_counter() + 1);
    wr_be(out, seq, 4);
    wr_be(out + 4, mas_nv_counter() + 1, 8);
    reply(ST_OK, out, sizeof out);
}

static void send_token(uint64_t counter) {
    static uint8_t tok[TOKEN_LEN];
    memcpy(tok, DOMAIN, 15);
    memcpy(tok + 15, key_id, 8);
    wr_be(tok + 23, counter, 8);
    memcpy(tok + 31, pending.payload, REQ_LEN);
    crypto_ed25519_sign(tok + BODY_LEN, sk, tok, BODY_LEN);
    reply(ST_OK, tok, TOKEN_LEN);
}

static void op_sign(const uint8_t *p, size_t n) {
    if (n != 4) { reply(ST_MALFORMED, 0, 0); return; }
    uint32_t s = (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 | (uint32_t)p[2] << 8 | p[3];
    if (!pending.valid || s != pending.seq) { reply(ST_STALE_REQUEST, 0, 0); return; }
    hal_irq_disable();
    int pressed = press_latched;
    uint64_t t = press_t;
    press_latched = 0;
    hal_irq_enable();
    if (!pressed) { reply(ST_NO_PRESS, 0, 0); return; }
    pending.valid = 0; /* one press, one request */
    hal_display(ZERO32, 0);
    uint32_t pw = pending.payload[PW_AT] < MAX_WINDOW_S ? pending.payload[PW_AT] : MAX_WINDOW_S;
    if (t - pending.t_req > (uint64_t)pw * 1000u) { reply(ST_PRESS_WINDOW, 0, 0); return; }
    uint64_t next = mas_nv_counter() + 1;
    /* SABOTEUR-ANCHOR: commit-then-sign */ if (mas_nv_commit(next) != 0) { fault = 1; reply(ST_NV_FAULT, 0, 0); return; } send_token(next);
}

static void handle(uint8_t op, const uint8_t *p, size_t n) {
    if (op != MAS_OP_GET_INFO && op != MAS_OP_REQUEST && op != MAS_OP_SIGN) { reply(ST_UNKNOWN_OPCODE, 0, 0); return; }
    if (fault) { reply(ST_NV_FAULT, 0, 0); return; }
    switch (op) {
    case MAS_OP_GET_INFO: op_info(n); break;
    case MAS_OP_REQUEST: op_request(p, n); break;
    default: op_sign(p, n); break;
    }
}

void mas_rx_byte(uint8_t b) {
    if (b != 0) {
        if (rx_n < RX_CAP) rx[rx_n++] = b;
        else rx_overflow = 1;
        return;
    }
    size_t n = rx_n;
    int ovf = rx_overflow;
    rx_n = 0;
    rx_overflow = 0;
    if (ovf) { reply(ST_BAD_FRAME, 0, 0); return; }
    int m = mas_cobs_decode(rx, n, dec, sizeof dec);
    if (m < 3) { reply(ST_BAD_FRAME, 0, 0); return; }
    uint16_t c = mas_crc16(dec, (size_t)m - 2);
    if (dec[m - 2] != (uint8_t)(c >> 8) || dec[m - 1] != (uint8_t)c) { reply(ST_BAD_FRAME, 0, 0); return; }
    handle(dec[0], dec + 1, (size_t)m - 3);
}
