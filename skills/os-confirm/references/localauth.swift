import Foundation
import LocalAuthentication

// `--probe` only asks whether this Mac can verify its owner (Touch ID or the account password); nothing is shown. Otherwise the first argument is the reason shown by the system prompt.
let ctx = LAContext()
var err: NSError?
let can = ctx.canEvaluatePolicy(.deviceOwnerAuthentication, error: &err)
if CommandLine.arguments.count > 1 && CommandLine.arguments[1] == "--probe" {
    print(can ? "available" : "unavailable")
    exit(can ? 0 : 3)
}
guard can else { print("unavailable"); exit(3) }
let reason = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "Approve"
let sem = DispatchSemaphore(value: 0)
var ok = false
ctx.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: reason) { success, _ in ok = success; sem.signal() }
sem.wait()
print(ok ? "approved" : "declined")
exit(ok ? 0 : 1)
