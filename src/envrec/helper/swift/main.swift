// envrec-helper: a tiny menu-less app that gives the envrec harness its OWN identity for macOS privacy prompts.
// It OBSERVES, with three narrow exceptions that each need a fresh owner approval on the harness side: `connect_paired` (connect or disconnect a device macOS ALREADY has paired,
// named by a salted hash; it never pairs a new device and writes nothing to it), `audio_set` (make one of the Mac's own output devices the default output) and `audio_get` (read them).
// It answers exactly seven requests on a local unix socket (mode 0600, same user only, per-run nonce) and exits when idle. Output is raw observations as JSON; the harness filters them.
import Foundation
import AppKit
import CoreBluetooth
import IOBluetooth
import CoreWLAN
import CoreLocation
import Network
import CoreAudio
import CryptoKit

let WHITELIST: [String: [String: (Double, Double)]] = [
    "ble_scan": ["seconds": (1, 20)],
    "ble_paired": [:],
    "wifi_info": [:],
    "mdns_browse": ["seconds": (1, 15)],
]
// text arguments: request -> argument -> the only shape it may have (a salted hash, a salt, an enumerated word)
let WHITELIST_TEXT: [String: [String: String]] = [
    "connect_paired": ["device": "^[0-9a-f]{8}$", "salt": "^[0-9a-f]{16,64}$", "action": "^(connect|disconnect)$"],
    "audio_get": ["salt": "^[0-9a-f]{16,64}$"],
    "audio_set": ["device": "^[0-9a-f]{8}$", "salt": "^[0-9a-f]{16,64}$"],
]
let IDLE_SECONDS = 20.0
let PERMISSION_WAIT = 90.0
var lastActivity = Date()
var busy = false  // a request in progress (a permission prompt can take a while) keeps the helper alive

func spin(until done: () -> Bool, timeout: Double) {
    let end = Date().addingTimeInterval(timeout)
    while !done() && Date() < end { RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.1)) }
}

// ---- Bluetooth LE: passive advertisement scan (scanForPeripherals only; no connect call exists in this file) ----
final class Scanner: NSObject, CBCentralManagerDelegate {
    // CoreBluetooth gets its OWN queue: the request itself runs on the main queue, so callbacks delivered there would never run while it waits.
    let queue = DispatchQueue(label: "envrec.helper.ble")
    let lock = NSLock()
    var mgr: CBCentralManager!
    var seen: [String: [String: Any]] = [:]
    var stateKnown = false
    override init() { super.init(); mgr = CBCentralManager(delegate: self, queue: queue) }
    func centralManagerDidUpdateState(_ c: CBCentralManager) { lock.lock(); stateKnown = true; lock.unlock() }
    func snapshot() -> (Bool, [[String: Any]]) { lock.lock(); defer { lock.unlock() }; return (stateKnown, Array(seen.values)) }
    func centralManager(_ c: CBCentralManager, didDiscover p: CBPeripheral, advertisementData ad: [String: Any], rssi RSSI: NSNumber) {
        var companies: [Int] = []
        if let m = ad[CBAdvertisementDataManufacturerDataKey] as? Data, m.count >= 2 { companies = [Int(m[0]) | (Int(m[1]) << 8)] }
        let services = ((ad[CBAdvertisementDataServiceUUIDsKey] as? [CBUUID]) ?? []).map { $0.uuidString.lowercased() }
        var rec: [String: Any] = ["id": p.identifier.uuidString, "rssi": RSSI.intValue, "company": companies, "services": services.sorted()]
        if let tx = ad[CBAdvertisementDataTxPowerLevelKey] as? NSNumber { rec["tx"] = tx.intValue }
        lock.lock(); defer { lock.unlock() }
        if let old = seen[p.identifier.uuidString], (old["rssi"] as? Int ?? -200) >= RSSI.intValue { return }
        seen[p.identifier.uuidString] = rec
    }
}

func bleScan(seconds: Double) -> [String: Any] {
    NSApp.activate(ignoringOtherApps: true)  // a permission prompt from a background app can stay hidden
    let s = Scanner()
    spin(until: { s.snapshot().0 && CBCentralManager.authorization != .notDetermined }, timeout: PERMISSION_WAIT)
    switch CBCentralManager.authorization {
    case .allowedAlways: break
    case .notDetermined: return ["ok": false, "error": "permission", "detail": "bluetooth: no answer to the prompt yet"]
    default: return ["ok": false, "error": "permission", "detail": "bluetooth: not allowed (System Settings > Privacy & Security > Bluetooth > envrec Helper)"]
    }
    spin(until: { s.mgr.state != .unknown && s.mgr.state != .resetting }, timeout: 5)
    if s.mgr.state != .poweredOn { return ["ok": false, "error": "unavailable", "detail": "bluetooth is off or unsupported"] }
    s.mgr.scanForPeripherals(withServices: nil, options: [CBCentralManagerScanOptionAllowDuplicatesKey: false])
    let end = Date().addingTimeInterval(seconds)
    spin(until: { Date() >= end }, timeout: seconds + 1)
    s.mgr.stopScan()
    return ["ok": true, "data": s.snapshot().1]
}

// ---- Bluetooth: the devices macOS already has paired (a read of its own list) ----
func blePaired() -> [String: Any] {
    let list = (IOBluetoothDevice.pairedDevices() as? [IOBluetoothDevice]) ?? []
    let recs: [[String: Any]] = list.map { d in
        ["name": d.name ?? "", "address": d.addressString ?? "", "connected": d.isConnected(),
         "major": Int(d.deviceClassMajor), "minor": Int(d.deviceClassMinor)]
    }
    return ["ok": true, "data": recs]
}

func shortHash(_ salt: String, _ ident: String) -> String {
    let d = SHA256.hash(data: Data((salt + "\u{1f}" + ident).utf8))
    return d.map { String(format: "%02x", $0) }.joined().prefix(8).description
}

func normalizedAddress(_ a: String) -> String { a.uppercased().replacingOccurrences(of: "-", with: ":") }
// CoreAudio uids of Bluetooth output devices start with the device address ("aa-bb-cc-dd-ee-ff:output")
func btAddress(inUID uid: String) -> String? {
    guard let r = uid.range(of: "^([0-9A-Fa-f]{2}[-:]){5}[0-9A-Fa-f]{2}", options: .regularExpression) else { return nil }
    return normalizedAddress(String(uid[r]))
}

// ---- Bluetooth: connect or disconnect a device macOS ALREADY has paired. The device must be in pairedDevices(); nothing here pairs, and nothing is written to the device. ----
func connectPaired(device: String, salt: String, action: String) -> [String: Any] {
    let list = (IOBluetoothDevice.pairedDevices() as? [IOBluetoothDevice]) ?? []
    guard let d = list.first(where: { shortHash(salt, normalizedAddress($0.addressString ?? "")) == device }) else {
        return ["ok": false, "error": "refused", "detail": "no already-paired device has that id; this helper never pairs a new device"]
    }
    if action == "disconnect" {
        let st = d.isConnected() ? d.closeConnection() : kIOReturnSuccess
        return ["ok": st == kIOReturnSuccess, "data": ["connected": d.isConnected()], "error": "unavailable", "detail": "closeConnection returned \(st)"]
    }
    if !d.isConnected() { _ = d.openConnection() }
    spin(until: { d.isConnected() }, timeout: 12)
    return ["ok": true, "data": ["connected": d.isConnected()]]
}

// ---- Audio output: read the Mac's output devices (ids are salted hashes of their CoreAudio uid; names are dropped) and set the default output ----
func audioDeviceIDs() -> [AudioObjectID] {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDevices, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size) == 0 else { return [] }
    var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &ids) == 0 else { return [] }
    return ids
}
func audioUID(_ id: AudioObjectID) -> String {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyDeviceUID, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    var uid: Unmanaged<CFString>? = nil
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard AudioObjectGetPropertyData(id, &addr, 0, nil, &size, &uid) == 0, let u = uid else { return "" }
    return u.takeRetainedValue() as String
}
func audioHasOutput(_ id: AudioObjectID) -> Bool {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreams, mScope: kAudioDevicePropertyScopeOutput, mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    return AudioObjectGetPropertyDataSize(id, &addr, 0, nil, &size) == 0 && size > 0
}
func audioTransport(_ id: AudioObjectID) -> String {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyTransportType, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    var t: UInt32 = 0
    var size = UInt32(MemoryLayout<UInt32>.size)
    guard AudioObjectGetPropertyData(id, &addr, 0, nil, &size, &t) == 0 else { return "other" }
    switch t {
    case kAudioDeviceTransportTypeBuiltIn: return "builtin"
    case kAudioDeviceTransportTypeBluetooth, kAudioDeviceTransportTypeBluetoothLE: return "bluetooth"
    case kAudioDeviceTransportTypeUSB: return "usb"
    default: return "other"
    }
}
func defaultOutput() -> AudioObjectID {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultOutputDevice, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    var id: AudioObjectID = 0
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    _ = AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &id)
    return id
}
func audioGet(salt: String) -> [String: Any] {
    let cur = defaultOutput()
    let outs: [[String: Any]] = audioDeviceIDs().filter(audioHasOutput).map { d in
        let uid = audioUID(d)
        var rec: [String: Any] = ["id": shortHash(salt, uid), "transport": audioTransport(d), "current": d == cur]
        if let a = btAddress(inUID: uid) { rec["bt"] = shortHash(salt, a) }  // the same hash connect_paired takes: it links an output to a paired device without a name
        return rec
    }
    return ["ok": true, "data": outs]
}
func audioSet(device: String, salt: String) -> [String: Any] {
    guard var id = audioDeviceIDs().filter(audioHasOutput).first(where: { shortHash(salt, audioUID($0)) == device || btAddress(inUID: audioUID($0)).map({ shortHash(salt, $0) }) == device }) else {
        return ["ok": false, "error": "refused", "detail": "no output device has that id"]
    }
    var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultOutputDevice, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    let st = AudioObjectSetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, UInt32(MemoryLayout<AudioObjectID>.size), &id)
    return ["ok": st == 0, "data": ["current": defaultOutput() == id], "error": "unavailable", "detail": "set default output returned \(st)"]
}

// ---- Wi-Fi: the interface this Mac is on (the network name needs Location permission) ----
final class Loc: NSObject, CLLocationManagerDelegate {
    let m = CLLocationManager()
    var answered = false
    override init() { super.init(); m.delegate = self }
    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) { if manager.authorizationStatus != .notDetermined { answered = true } }
}

func wifiInfo() -> [String: Any] {
    NSApp.activate(ignoringOtherApps: true)
    let loc = Loc()
    if loc.m.authorizationStatus == .notDetermined {
        loc.m.requestWhenInUseAuthorization()
        spin(until: { loc.answered }, timeout: PERMISSION_WAIT)
    }
    let allowed = loc.m.authorizationStatus == .authorizedAlways
    guard let i = CWWiFiClient.shared().interface() else { return ["ok": false, "error": "unavailable", "detail": "no Wi-Fi interface"] }
    var rec: [String: Any] = ["interface": i.interfaceName ?? "", "rssi": i.rssiValue(), "noise": i.noiseMeasurement(), "tx_rate": i.transmitRate(),
                              "power_on": i.powerOn(), "location_allowed": allowed]
    if let ch = i.wlanChannel() { rec["channel"] = ch.channelNumber }
    if allowed { rec["ssid"] = i.ssid() ?? "" ; rec["bssid"] = i.bssid() ?? "" }
    return ["ok": true, "data": rec]
}

// ---- mDNS: browse a FIXED list of service types (the same list is in Info.plist NSBonjourServices) ----
let BONJOUR = ["_airplay._tcp", "_raop._tcp", "_companion-link._tcp", "_apple-mobdev2._tcp", "_matter._tcp", "_spotify-connect._tcp", "_googlecast._tcp",
               "_hap._tcp", "_homekit._tcp", "_http._tcp", "_ipp._tcp", "_printer._tcp", "_smb._tcp", "_device-info._tcp", "_sleep-proxy._udp"]

func mdnsBrowse(seconds: Double) -> [String: Any] {
    var found: [[String: Any]] = []
    var denied = false
    var browsers: [NWBrowser] = []
    let q = DispatchQueue(label: "envrec.helper.mdns")
    let lock = NSLock()
    for t in BONJOUR {
        let b = NWBrowser(for: .bonjour(type: t, domain: nil), using: NWParameters())
        b.stateUpdateHandler = { st in if case .waiting(let e) = st, "\(e)".contains("PolicyDenied") { lock.lock(); denied = true; lock.unlock() } }
        b.browseResultsChangedHandler = { results, _ in
            lock.lock(); defer { lock.unlock() }
            for r in results { if case let .service(name, type, domain, _) = r.endpoint { found.append(["type": type, "name": name, "domain": domain]) } }
        }
        b.start(queue: q)
        browsers.append(b)
    }
    let end = Date().addingTimeInterval(seconds)
    spin(until: { Date() >= end }, timeout: seconds + 1)
    browsers.forEach { $0.cancel() }
    lock.lock(); defer { lock.unlock() }
    if denied && found.isEmpty { return ["ok": false, "error": "permission", "detail": "local network: not allowed (System Settings > Privacy & Security > Local Network > envrec Helper)"] }
    var uniq: [String: [String: Any]] = [:]
    for f in found { uniq["\(f["type"] ?? "")|\(f["name"] ?? "")"] = f }
    return ["ok": true, "data": Array(uniq.values)]
}

// ---- the request gate: only the four names above, only their numeric arguments, only with the nonce ----
func handle(_ line: Data, nonce: String) -> [String: Any] {
    guard let obj = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any] else { return ["ok": false, "error": "refused", "detail": "not a request"] }
    guard let given = obj["nonce"] as? String, given.utf8.count == nonce.utf8.count, zip(given.utf8, nonce.utf8).reduce(0, { $0 | ($1.0 ^ $1.1) }) == 0 else {
        return ["ok": false, "error": "refused", "detail": "bad nonce"]
    }
    guard let req = obj["req"] as? String, WHITELIST[req] != nil || WHITELIST_TEXT[req] != nil else { return ["ok": false, "error": "refused", "detail": "unknown request"] }
    var args: [String: Double] = [:]
    var text: [String: String] = [:]
    for (k, v) in obj where k != "nonce" && k != "req" {
        if let pattern = WHITELIST_TEXT[req]?[k], let s = v as? String, s.range(of: pattern, options: .regularExpression) != nil { text[k] = s; continue }
        guard let range = WHITELIST[req]?[k], let n = v as? NSNumber, n.doubleValue >= range.0, n.doubleValue <= range.1 else {
            return ["ok": false, "error": "refused", "detail": "argument not allowed: \(k)"]
        }
        args[k] = n.doubleValue
    }
    if let need = WHITELIST_TEXT[req], Set(need.keys) != Set(text.keys) { return ["ok": false, "error": "refused", "detail": "missing argument for \(req)"] }
    switch req {
    case "connect_paired": return connectPaired(device: text["device"]!, salt: text["salt"]!, action: text["action"]!)
    case "audio_get": return audioGet(salt: text["salt"]!)
    case "audio_set": return audioSet(device: text["device"]!, salt: text["salt"]!)
    case "ble_scan": return bleScan(seconds: args["seconds"] ?? 8)
    case "ble_paired": return blePaired()
    case "wifi_info": return wifiInfo()
    default: return mdnsBrowse(seconds: args["seconds"] ?? 6)
    }
}

// ---- the socket: same user only, mode 0600 ----
func serve(rundir: String, nonce: String) {
    let path = rundir + "/sock"
    unlink(path)
    umask(0o177)
    let fd = socket(AF_UNIX, SOCK_STREAM, 0)
    var addr = sockaddr_un()
    addr.sun_family = sa_family_t(AF_UNIX)
    withUnsafeMutableBytes(of: &addr.sun_path) { p in _ = path.withCString { strncpy(p.baseAddress!.assumingMemoryBound(to: CChar.self), $0, p.count - 1) } }
    let ok = withUnsafePointer(to: &addr) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) } }
    guard ok == 0, listen(fd, 4) == 0 else { exit(2) }
    chmod(path, 0o600)
    while true {
        let c = accept(fd, nil, nil)
        if c < 0 { continue }
        var uid: uid_t = 0, gid: gid_t = 0
        if getpeereid(c, &uid, &gid) != 0 || uid != getuid() { close(c); continue }
        lastActivity = Date()
        var buf = Data(), byte: UInt8 = 0
        while buf.count < 4096, read(c, &byte, 1) == 1, byte != 10 { buf.append(byte) }
        let sem = DispatchSemaphore(value: 0)
        var reply: [String: Any] = [:]
        DispatchQueue.main.async { busy = true; reply = handle(buf, nonce: nonce); busy = false; lastActivity = Date(); sem.signal() }
        sem.wait()
        let body: Data = JSONSerialization.isValidJSONObject(reply) ? ((try? JSONSerialization.data(withJSONObject: reply)) ?? Data())
            : ((try? JSONSerialization.data(withJSONObject: ["ok": false, "error": "unavailable", "detail": "the reply could not be encoded as JSON"])) ?? Data())
        var data = body
        data.append(10)
        _ = data.withUnsafeBytes { write(c, $0.baseAddress, data.count) }
        close(c)
    }
}

// ---- start: argv[1] is a private directory holding `nonce` (0600); the socket is created inside it ----
guard CommandLine.arguments.count >= 2 else { exit(64) }
let rundir = CommandLine.arguments[1]
guard let nonceText = try? String(contentsOfFile: rundir + "/nonce", encoding: .utf8) else { exit(65) }
let nonce = nonceText.trimmingCharacters(in: .whitespacesAndNewlines)
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
Thread.detachNewThread { serve(rundir: rundir, nonce: nonce) }
Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { _ in
    if !busy && Date().timeIntervalSince(lastActivity) > IDLE_SECONDS { exit(0) }
}
app.run()
