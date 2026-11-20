# SECURE_BOOT.md — secure boot and lock design for the MAS key (STM32U585)

> **DESIGN-ONLY.** This design has not been run on hardware: no STM32U585 was flashed, locked or attacked. Option-byte names and
> their behaviour are written from memory of ST's reference manual RM0456 and the STM32U5 security application notes and are
> **UNVERIFIED** until a person checks them against the exact documents and a real part (NEEDS_HUMAN.md). Emulation (QEMU mps2-an505,
> Renode) runs the same firmware core but models none of this: no RDP, no option bytes, no TrustZone partitioning (DECISIONS D-007).
> `device/tests/test_secboot_doc.py` checks that every assumption below matches the firmware source; it cannot check that a chip is locked.

## 1. What the lock must protect

The key's job (MISSION 4.1 rules 2-4) is to sign one request per physical press. That holds only if:

1. only this firmware runs on the key (no other code can sign, read the seed or set the press latch);
2. the signing seed never leaves the chip (not by an opcode, not by a debugger, not by booting other code);
3. the counter only moves forward for a given key identity;
4. a press can come only from the button.

The key protects the host from the model and from injected data (D-005). It does not protect against an attacker with the key in hand,
a lab and time: glitching, side channels and decapping are out of scope for v0 (D-007, D-008) and are mitigated by revoking the key_id.

## 2. Firmware assumptions and what enforces each

Each row names where the firmware relies on the assumption (checked by the test to exist), what on the STM32U585 is meant to make it
true, and how far it is verified today.

| ID | Firmware assumption | Where the firmware relies on it | Mechanism on the STM32U585 | Verified |
|---|---|---|---|---|
| F1 | Only this firmware runs after reset. | `mas_init` in `core/mas_core.c` | TrustZone on (TZEN=1); boot address SECBOOTADD0 at the secure image; BOOT_LOCK=1 so the boot address cannot move; boot pins/option bytes (nSWBOOT0, nBOOT0) fixed to user flash; RDP level 2 so neither the system bootloader nor SRAM boot nor debug can run other code. | UNVERIFIED |
| F2 | No command reads, writes or regenerates the key; only three opcodes exist. | `MAS_OP_GET_INFO` in `core/mas_core.h`, `MAS_OP_SIGN` in `core/mas_core.h` | Opcode allowlist in the core (tested); RDP level 2 removes debugger access to flash and SRAM. | SIMULATED+EMULATED |
| F3 | The seed and public key live in the key page and are read once at boot. | `MAS_PAGE_KEY` in `core/mas_hal.h`, `mas_nv_write_key` in `core/mas_nv.c` | Key page inside the secure flash watermark (SECWM1/SECWM2 cover all flash); HDP (hide protection) over the key page, with access disabled by the firmware right after `mas_init` has derived the key (HDPxACCDIS), so later code cannot read it again. | UNVERIFIED |
| F4 | Only the non-volatile module writes flash. | `hal_flash_program` in `core/mas_nv.c`, `mas_nv_commit` in `core/mas_nv.c` | All flash secure-only; WRP (write protection) over the code region so even a firmware bug cannot rewrite the image; key and counter pages left writable by secure code only. | SIMULATED |
| F5 | A press reaches the core only through the button's interrupt. | `mas_button_isr` in `core/mas_core.c` | Button GPIO and its EXTI line assigned to the secure world (GPIO/EXTI secure configuration, GTZC); no other driver on that net (A-1 §6 item 4); the transport has no path to the ISR (tested). | UNVERIFIED |
| F6 | A failed TRNG read never yields a key: the key latches a fault. | `hal_trng` in `core/mas_core.c` | STM32U5 RNG with its health tests; the board HAL returns an error on a seed or clock error; the core then answers NV_FAULT to everything. | SIMULATED |
| F7 | A corrupted key page after the key has signed is a fault, never a new identity. | `mas_nv_log_empty` in `core/mas_core.c` | The counter log proves the key was used; reflashing is only possible through the mass erase of RDP regression (F8), which also erases the counters. | SIMULATED |
| F8 | Reflashing the key makes a new key identity. | `key_id` in `core/mas_core.c` | Moving RDP from level 1 to 0 forces a mass erase of the user flash, key and counter pages included; at level 2 there is no regression (unless an OEM2 password regression is provisioned, which this design does not use). The next boot draws a new seed, so key_id changes and the old key_id must be revoked at the fleet. | UNVERIFIED |
| F9 | A torn counter write never reuses a counter. | `mas_nv_commit` in `core/mas_nv.c` | Append-only records with a complement check (tested under power cuts on the host build); on the STM32U5 a torn ECC word can raise an NMI on read, so the NMI handler must latch the same fault (to be confirmed on hardware). | SIMULATED |
| F10 | key_id = sha256(pub)[:8], pinned by the gate at pairing. | `key_id` in `core/mas_core.c`, `mas_init` in `core/mas_core.c` | Identity is the public key; the host side pins it (P2-PAIRING); a stolen or reflashed key is revoked by key_id. | SIMULATED |

Opcode allowlist: 0x01 GET_INFO, 0x02 REQUEST, 0x03 SIGN

`mas_nv.c` is the only flash writer in the core: the counter log and the key page are programmed or erased nowhere else (tested).
A TRNG failure at first boot latches a fault (fail closed): no key is made and every command answers NV_FAULT.
The button is wired to `mas_button_isr`, which only a target's board layer calls; the core's receive path cannot press it.

## 3. Flash layout (as the firmware names it)

The firmware addresses three 8 KiB pages through the HAL, never by address:

| Name | Value | Content |
|---|---|---|
| `MAS_PAGE_SIZE` = 8192 | bytes per page | STM32U585 flash page size (8 KiB) |
| `MAS_PAGE_KEY` = 0 | key page | seed[32] \| pub[32] \| crc16, written once at first boot |
| `MAS_PAGE_CTR_A` = 1 | counter page A | append-only 16-byte records counter \| ~counter |
| `MAS_PAGE_CTR_B` = 2 | counter page B | the other half of the counter log |

On the STM32U585CI (2 MiB, two banks of 1 MiB) the board HAL maps these to the last three pages of bank 2, inside the secure watermark
and inside the HDP area of F3 (key page only), outside the WRP area of F4. Exact page numbers: UNVERIFIED (to fix when the board HAL
is written).

## 4. Option bytes at provisioning

| Option byte | Value | Why |
|---|---|---|
| TZEN | 1 | TrustZone on; the whole firmware runs in the secure world (v0 has no non-secure image) |
| SECBOOTADD0 | start of the secure image | the only entry point after reset |
| BOOT_LOCK | 1 | the boot address can no longer be changed |
| SECWM1 / SECWM2 | all of bank 1 and bank 2 | every flash page is secure |
| WRP | the code region | the image cannot be rewritten, even by its own bug |
| HDP | the key page | hidden after `mas_init` derives the key |
| nSWBOOT0 / nBOOT0 | boot from user flash | no system-bootloader boot |
| RDP | level 1 while testing, level 2 when sealed | level 2 is permanent: no debug, no bootloader, no regression |

Order matters: everything else first, RDP level 2 last, because level 2 freezes the option bytes. A mistake before level 2 is
recoverable by a mass erase (RDP 1 to 0); after it, the part is final.

## 5. Lifecycle

1. **Bring-up (RDP level 0).** SWD open on J3; test seeds allowed; nothing produced here is a real key.
2. **Provisioning.** Full mass erase, flash the release image, set the option bytes of section 4 except RDP, set RDP level 1, power-cycle
   and run the self-test (first boot draws the seed from the TRNG; GET_INFO shows the new key_id).
3. **Sealing.** Set RDP level 2. From here on there is no debugger and no firmware update: the opcode set has no update command (F2).
4. **Use.** The owner pairs the key (its key_id is pinned by the host gate, P2-PAIRING).
5. **Loss, theft or replacement.** Revoke the key_id at the fleet. A level-1 key that is reflashed comes back as a different key_id (F8).

## 6. Key identity

The key identity is generated on the device at first boot and never leaves it; key_id = sha256(pub)[:8]. A reflash erases it (F8), so a
key never keeps its identity across firmware it did not boot with: the spirit of TKey's "the identity depends on the firmware", without
TKey's firmware-hash derivation (the STM32U585 has no unique device secret in this design; DICE-style derivation from a hardware secret
would be the next step and is not done here).

## 7. Not verifiable here, and the human review

Not verifiable without hardware: that RDP level 2 really closes debug and the bootloader; that BOOT_LOCK, SECWM, WRP and HDP behave as
described; TrustZone partitioning of the button GPIO and EXTI; RNG health behaviour; the NMI on a torn ECC word; glitch and side-channel
resistance. A person must check, against RM0456 and the STM32U5 security application notes, every option-byte name and value in
section 4 and every mechanism in section 2, then provision one sacrificial part and try to read it with a debugger at each RDP level.
