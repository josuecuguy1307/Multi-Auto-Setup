/* Fixture program written for the envrec tests. Compiled once, inspected statically, never run by the tests. */
#include <stdio.h>
#include <string.h>

static int checksum(const char *s) {
    int sum = 0;
    for (; *s; s++) sum = (sum * 31 + *s) & 0xFFFF;
    return sum;
}

int main(int argc, char **argv) {
    const char *name = argc > 1 ? argv[1] : "world";
    printf("hello, %s (checksum %d)\n", name, checksum(name));
    return 0;
}
