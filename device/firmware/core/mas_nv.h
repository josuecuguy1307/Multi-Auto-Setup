/* Non-volatile state: the monotonic counter log (two pages) and the key page. The only module that programs or erases flash. */
#ifndef MAS_NV_H
#define MAS_NV_H

#include <stdint.h>

int mas_nv_init(void);                                   /* scan the counter log; 0 on success */
uint64_t mas_nv_counter(void);                           /* highest committed counter, 0 if none */
int mas_nv_log_empty(void);                              /* no counter was ever committed with this key */
int mas_nv_commit(uint64_t c);                           /* program, read back, compare; 0 only if c is durable */
int mas_nv_key(uint8_t seed[32], uint8_t pub[32]);       /* 0 valid, 1 erased, -1 corrupted */
int mas_nv_write_key(const uint8_t seed[32], const uint8_t pub[32]);
int mas_nv_erase_key(void);

#endif
