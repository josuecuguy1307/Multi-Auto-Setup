"""Source of truth for the MAS key board (DESIGN-ONLY; spec: DECISIONS.md A-3, A-1 §6, D-007, D-008).

Everything else is generated from this file: the schematic (gen_sch.py), the netlist (kicad-cli), the board (gen_pcb.py, from the netlist),
the fabrication outputs and the BOM. MCU pins are named, never numbered: the numbers come from KiCad's stock symbol, so the human review
reduces to "stock symbol against the datasheet" (HUMAN_REVIEW_PCB.md item 1).

NO FABRICAR sin revisión humana.
"""

from __future__ import annotations

from dataclasses import dataclass, field

BOARD_W, BOARD_H = 20.0, 54.0          # mm; long axis = y, USB-C mouth at y = 0 (A-3 said 48; the OLED socket needs the length)
SILK_TEXT_X = 0.9                       # the stamp runs along this strip, 0.9 mm from the left edge (right edge on B.SilkS); keep parts out of it
STAMP = "NO FABRICAR sin revisión humana"
BOM_SNAPSHOT_DATE = "2026-11-17"  # parts list frozen on this date; no live distributor lookup was made (bom_snapshot.json)


@dataclass(frozen=True)
class Part:
    ref: str
    lib_id: str
    footprint: str
    value: str
    mpn: str
    manufacturer: str
    pins: dict = field(default_factory=dict)  # pin NAME (or number for passives/connectors) -> net; unlisted pins are no-connect
    at: tuple = (0.0, 0.0, 0.0)               # board placement (x, y, rotation) in mm, origin at the board's top-left corner
    dnp: bool = False


R0603, C0603 = "Resistor_SMD:R_0603_1608Metric", "Capacitor_SMD:C_0603_1608Metric"


def R(ref, value, a, b, at, mpn):
    return Part(ref, "Device:R", R0603, value, mpn, "Yageo", {"1": a, "2": b}, at)


def C(ref, value, a, b, at, mpn):
    return Part(ref, "Device:C", C0603, value, mpn, "Samsung", {"1": a, "2": b}, at)


# The MCU's pins by name. Names must match the stock symbol's pin names exactly; gen_sch.py refuses to build otherwise.
MCU_PINS = {
    "PA11": "USB_DM", "PA12": "USB_DP",
    "PA13": "SWDIO", "PA14": "SWCLK", "PB3": "SWO",
    "PB6": "I2C_SCL", "PB7": "I2C_SDA",
    "PA0": "BTN", "PA8": "LED_K_DRV",
    "PH3": "BOOT0", "NRST": "NRST",
    "VDD": "+3V3", "VDDA": "+3V3", "VBAT": "+3V3",  # the stock LQFP48 symbol names pin 36 VDD (no VDDUSB): UNVERIFIED
    "VSS": "GND", "VSSA": "GND",
    "VCAP": "VCAP",
}

PARTS = [
    # ---- USB-C, ESD, regulator (top of the board) ----
    Part("J1", "Connector:USB_C_Receptacle_USB2.0_16P", "Connector_USB:USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal",
         "USB-C", "USB4105-GF-A", "GCT",
         {"VBUS": "VBUS", "GND": "GND", "SHIELD": "GND", "CC1": "CC1", "CC2": "CC2", "D+": "USB_DP", "D-": "USB_DM"}, (10.0, 3.72, 180.0)),
    Part("U2", "Power_Protection:USBLC6-2SC6", "Package_TO_SOT_SMD:SOT-23-6", "USBLC6-2SC6", "USBLC6-2SC6", "STMicroelectronics",
         {"I/O1": "USB_DP", "I/O2": "USB_DM", "VBUS": "VBUS", "GND": "GND"}, (10.0, 10.5, 180.0)),  # turned so D- exits above D+:
    # the two chains J1 -> U2 -> U1 then nest without crossing on the top layer (U1's D+ pin is above its D- pin; board evaluator run 2)
    R("R1", "5.1k", "CC1", "GND", (3.0, 10.5, 90.0), "RC0603FR-075K1L"),
    R("R2", "5.1k", "CC2", "GND", (17.0, 10.5, 90.0), "RC0603FR-075K1L"),
    Part("U3", "Regulator_Linear:AP2112K-3.3", "Package_TO_SOT_SMD:SOT-23-5", "AP2112K-3.3", "AP2112K-3.3TRG1", "Diodes Inc",
         {"VIN": "VBUS", "EN": "VBUS", "GND": "GND", "VOUT": "+3V3"}, (6.5, 15.0, 0.0)),
    C("C1", "1uF", "VBUS", "GND", (3.0, 15.0, 90.0), "CL10A105KB8NNNC"),
    C("C2", "1uF", "+3V3", "GND", (11.0, 15.0, 90.0), "CL10A105KB8NNNC"),
    C("C3", "4.7uF", "+3V3", "GND", (14.0, 15.0, 90.0), "CL10A475KO8NNNC"),
    # ---- MCU and its supply pins (centre) ----
    Part("U1", "MCU_ST_STM32U5:STM32U585CITx", "Package_QFP:LQFP-48_7x7mm_P0.5mm", "STM32U585CIT6", "STM32U585CIT6",
         "STMicroelectronics", MCU_PINS, (10.0, 24.6, 0.0)),
    C("C4", "100nF", "+3V3", "GND", (3.0, 21.0, 90.0), "CL10B104KB8NNNC"),
    C("C5", "100nF", "+3V3", "GND", (17.0, 21.0, 90.0), "CL10B104KB8NNNC"),
    C("C6", "100nF", "+3V3", "GND", (3.0, 24.6, 90.0), "CL10B104KB8NNNC"),
    C("C7", "100nF", "+3V3", "GND", (13.0, 31.0, 0.0), "CL10B104KB8NNNC"),
    C("C8", "1uF", "+3V3", "GND", (3.0, 28.2, 90.0), "CL10A105KB8NNNC"),
    C("C9", "4.7uF", "VCAP", "GND", (9.6, 31.0, 0.0), "CL10A475KO8NNNC"),
    C("C12", "100nF", "+3V3", "GND", (7.0, 18.0, 0.0), "CL10B104KB8NNNC"),  # pin 48 VDD: one 100 nF per supply pin (A-3, evaluator P5 run 1)
    R("R3", "10k", "BOOT0", "GND", (10.3, 18.0, 0.0), "RC0603FR-0710KL"),
    C("C10", "100nF", "NRST", "GND", (13.6, 18.0, 0.0), "CL10B104KB8NNNC"),
    # ---- button with RC and pull-up ----
    Part("SW1", "Switch:SW_Push", "Button_Switch_SMD:SW_SPST_PTS645Sx43SMTR92", "PTS645", "PTS645SM43SMTR92LFS", "C&K",
         {"1": "BTN_RAW", "2": "GND"}, (10.0, 43.6, 0.0)),
    R("R4", "10k", "+3V3", "BTN_RAW", (3.0, 32.0, 90.0), "RC0603FR-0710KL"),
    R("R5", "1k", "BTN_RAW", "BTN", (3.0, 35.6, 90.0), "RC0603FR-071KL"),
    C("C11", "100nF", "BTN", "GND", (3.0, 39.2, 90.0), "CL10B104KB8NNNC"),
    # ---- I2C display socket, pull-ups, LED, SWD ----
    R("R6", "4.7k", "+3V3", "I2C_SCL", (17.0, 32.0, 90.0), "RC0603FR-074K7L"),
    R("R7", "4.7k", "+3V3", "I2C_SDA", (17.0, 35.6, 90.0), "RC0603FR-074K7L"),
    Part("J2", "Connector_Generic:Conn_01x04", "Connector_PinSocket_2.54mm:PinSocket_1x04_P2.54mm_Vertical", "OLED SSD1306",
         "PPTC041LFBN-RC", "Sullins", {"1": "GND", "2": "+3V3", "3": "I2C_SCL", "4": "I2C_SDA"}, (6.19, 51.3, 90.0)),
    Part("D1", "Device:LED", "LED_SMD:LED_0603_1608Metric", "green", "LTST-C191KGKT", "Lite-On", {"K": "LED_K_DRV", "A": "LED_A"}, (3.0, 42.8, 90.0)),
    R("R8", "1k", "+3V3", "LED_A", (3.0, 46.4, 90.0), "RC0603FR-071KL"),
    Part("J3", "Connector_Generic:Conn_02x05_Odd_Even", "Connector_PinHeader_1.27mm:PinHeader_2x05_P1.27mm_Vertical_SMD", "SWD",
         "FTSH-105-01-L-DV-K", "Samtec",
         {"1": "+3V3", "2": "SWDIO", "3": "GND", "4": "SWCLK", "5": "GND", "6": "SWO", "9": "GND", "10": "NRST"}, (10.0, 36.0, 0.0)),
]

# Nets that only exist as power and that PWR_FLAG must mark as driven (their source is outside the schematic: the USB host).
POWER_FLAGS = ["VBUS", "GND"]

# MCU pins that must exist on the symbol and stay unconnected (no crystal: HSI48 + CRS).
EXPECTED_NO_CONNECT = ["PH0", "PH1", "PC14", "PC15"]

UNVERIFIED = [
    "Every STM32U585CIT6 LQFP48 pin number (taken from KiCad's stock symbol, not checked against DS13086).",
    "VCAP on the LDO (non-Q) STM32U585: presence, count and value (AN5373).",
    "Crystal-less USB FS with HSI48 + CRS on STM32U585 (advisor: confident, not checked against RM0456).",
    "USB-C J1 symbol/footprint names and the GCT USB4105 land pattern.",
    "OLED module pin order (GND/VCC order varies by vendor) and whether the module already carries I2C pull-ups.",
    "AP2112K-3.3 dropout and thermal at worst-case VBUS; VDDA tied to +3V3 without a ferrite (ADC unused).",
    "USB D+/D- routed without impedance control.",
    "Pin 36: the stock LQFP48 symbol names it VDD (no VDDUSB pin); check DS13086 whether it is VDDUSB. Here it is tied to +3V3 with its own 100 nF.",
]
