/* Hardware abstraction for the MAS key firmware. The core calls only these; each target (host mock, QEMU, Renode, STM32U585) implements them.
 * The button is not here: the target's button ISR calls mas_button_isr() directly, so nothing on the transport path can press it. */
#ifndef MAS_HAL_H
#define MAS_HAL_H

#include <stddef.h>
#include <stdint.h>

#define MAS_PAGE_SIZE 8192u
#define MAS_PAGE_KEY 0
#define MAS_PAGE_CTR_A 1
#define MAS_PAGE_CTR_B 2

uint32_t hal_tick_ms(void);                                   /* monotonic, wraps at 2^32 */
void hal_tx(const uint8_t *buf, size_t len);                  /* bytes out on the transport */
void hal_display(const uint8_t summary[32], uint64_t next);  /* all-zero summary clears the screen */
int hal_trng(uint8_t out[32]);                                /* 0 on success */
const uint8_t *hal_flash_ptr(int page);                       /* memory-mapped read */
int hal_flash_program(int page, uint32_t off, const uint8_t *buf, size_t len); /* only clears bits; 0 on success */
int hal_flash_erase(int page);                                /* 0 on success */
void hal_irq_disable(void);
void hal_irq_enable(void);

#endif
