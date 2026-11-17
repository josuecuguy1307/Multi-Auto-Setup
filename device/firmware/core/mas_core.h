/* MAS key firmware core: the approval state machine (spec: DECISIONS.md D-004, A-2). Static memory only. */
#ifndef MAS_CORE_H
#define MAS_CORE_H

#include <stdint.h>

/* The opcode allowlist. Nothing else is accepted; no opcode reads, writes or regenerates the key, the counter policy or any log. */
#define MAS_OP_GET_INFO 0x01
#define MAS_OP_REQUEST 0x02
#define MAS_OP_SIGN 0x03

#define ST_OK 0
#define ST_MALFORMED 1
#define ST_SUMMARY_NOT_PRINTABLE 2
#define ST_STALE_REQUEST 3
#define ST_NO_PRESS 4
#define ST_PRESS_WINDOW 5
#define ST_UNKNOWN_OPCODE 6
#define ST_BAD_FRAME 7
#define ST_NV_FAULT 8

void mas_init(void);
void mas_rx_byte(uint8_t b);
void mas_button_isr(void);

#endif
