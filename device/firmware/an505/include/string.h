/* Freestanding string.h for the an505 build: only what the core and Monocypher use; defined in an505/startup.c. */
#ifndef MAS_AN505_STRING_H
#define MAS_AN505_STRING_H
#include <stddef.h>

void *memcpy(void *dst, const void *src, size_t n);
void *memmove(void *dst, const void *src, size_t n);
void *memset(void *dst, int c, size_t n);
int memcmp(const void *a, const void *b, size_t n);

#endif
