/* QEMU mps2-an505: the image is loaded into secure SSRAM at 0x10000000 and runs from RAM (EMULATED). */
MEMORY
{
    RAM (rwx) : ORIGIN = 0x10000000, LENGTH = 4M
}

SECTIONS
{
    .text : { KEEP(*(.vectors)) *(.text*) *(.rodata*) } > RAM
    .data : ALIGN(4) { _sdata = .; *(.data*) . = ALIGN(4); _edata = .; } > RAM
    _sidata = _sdata;
    .bss (NOLOAD) : ALIGN(4) { _sbss = .; *(.bss*) *(COMMON) . = ALIGN(4); _ebss = .; } > RAM
    . = ALIGN(8);
    . = . + 0x10000;
    _estack = .;
}
