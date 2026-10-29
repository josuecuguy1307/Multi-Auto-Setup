# DATA_SOURCES

Public ID databases used for rung-3 fingerprinting. Fetched once by `scripts/fetch_data.py` (one GET per file, not scanning) into `data/downloads/`, which is **git-ignored: no data file is committed**. After the fetch, every lookup works offline. Unit tests use small labeled samples in the test files; each loader also has one test on the real file that is skipped when the file is absent.

Fetch time, size and sha256 are in `data/downloads/MANIFEST.json`. The same values as fetched here:

| File | Source URL | Fetched (UTC) | sha256 | License as stated on the source's own page | Status |
|---|---|---|---|---|---|
| `usb.ids` (730,605 B) | http://www.linux-usb.org/usb.ids | 2026-10-24 06:53 | `f5a48b0cc8dae1607c2f0bae6b8dc13f2ecef69dbeaeaf34b4be8e280d34dba4` | http://www.linux-usb.org/usb-ids.html: "under the terms of either the GNU General Public License (version 2 or later) or of the 3-clause BSD License". The file itself carries no license text. Data version in the file header: 2026.06.26 | license confirmed (dual); still not committed |
| `oui.csv` (3,863,642 B) | https://standards-oui.ieee.org/oui/oui.csv | 2026-10-24 06:53 | `71cf77fb3aeadb2fffb048de125434d26a431ad207ed7cc0eb3871ab912f755a` | none found in the file or on https://standards-oui.ieee.org/ | **license unconfirmed**: git-ignored, never committed |
| `bt_company_ids.json` (216,175 B) | https://raw.githubusercontent.com/NordicSemiconductor/bluetooth-numbers-database/master/v1/company_ids.json | 2026-10-24 06:53 | `6b2aff969cc5d1f7deb84ef45350034e26008ed65de3660e0dad929756052382` | that repo's LICENSE (saved as `bt_numbers_LICENSE.txt`, sha256 `1aa0bd572f4e3aa4d958687f5e114e888614e651c476d1657dd814f2f743f9ca`): 3-clause BSD, Nordic Semiconductor ASA. The data is derived from Bluetooth SIG assigned numbers, whose own license is **unconfirmed** | BSD-3-Clause for the compilation; SIG origin unconfirmed; git-ignored |
| `bt_service_uuids.json` (18,707 B) | https://raw.githubusercontent.com/NordicSemiconductor/bluetooth-numbers-database/master/v1/service_uuids.json | 2026-10-24 06:53 | `fec38d7a1d24f18a3241a4cc226949be699dccb8abe804df92017a170b041a82` | same as above | same as above |
| `iana_services.csv` (1,158,463 B) | https://www.iana.org/assignments/service-names-port-numbers/service-names-port-numbers.csv | 2026-10-24 06:53 | `3837683c470c65e5a7bdc681bb462a3f5d14b1444aa791cef774c291f6212121` | none found in the file or on the registry page | **license unconfirmed**: git-ignored, never committed |

Notes
- The research log said Nordic's database was MIT. Its LICENSE file says 3-clause BSD. The log was wrong.
- The Bluetooth SIG's own site (https://www.bluetooth.com/specifications/assigned-numbers/) was not used as a download source in this phase.
- mDNS service types come from the IANA service-name registry, which is the list mDNS service names are registered in. It is not an mDNS capture.
- Serial protocol signatures (NMEA, SCPI `*IDN?`, AT, Modbus RTU/ASCII, G-code) are patterns written for this project in `src/envrec/fingerprint/serial_signatures.py`. They use no third-party data.
- A vendor match identifies the vendor, never the model. Vendor and model are separate facts (`usb.vendor_name`, `usb.product_name`) and the model stays `unknown` when only the vendor matched.
