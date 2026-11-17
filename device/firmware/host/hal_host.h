#ifndef HAL_HOST_H
#define HAL_HOST_H
#include <stdint.h>

void host_hal_init(void);
void host_tick_add(uint32_t ms);
void host_display_get(uint8_t summary[32], uint64_t *next);

#endif
