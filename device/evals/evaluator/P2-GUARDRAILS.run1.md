# Evaluator report — P2-GUARDRAILS, run 1 (2026-11-18, evaluator agent, fresh context). Evaluated commit 2299003. Verbatim.

UNIT: P2-GUARDRAILS (evaluator run 1, commit 2299003, clean copy runs/eval/pgr1/tree)
VERDICT: FAIL

RAN:
git archive HEAD | tar -x -C runs/eval/pgr1/tree
PYTHONPATH=runs/eval/pgr1/tree/src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 .venv/Scripts/python -m pytest -c device/acceptance/pytest.ini --rootdir=runs/eval/pgr1/tree -q -p no:cacheprovider runs/eval/pgr1/tree/device/tests/test_guardrails.py   -> 26 passed (mas_device.__file__ = the copy)
.venv/Scripts/python scripts/device_accept.py status ; ... verify   (read-only)
runs/eval/pgr1/atk/a1_creds.py      credential names through the real daemon's RPC (read + plan_write)
runs/eval/pgr1/atk/a2_rpc.py        RPC fuzz: BOM, CRLF, id 0/-1/2^53, duplicate keys at the top and nested, two objects on one line, u-escapes, lone surrogates, 1e400, 5000-digit int, nesting 900/2500 deep, non-canonical base64, uppercase hex, NUL prefix, 8 MiB padding just under and over the limit, 300 refusals
runs/eval/pgr1/atk/a3_hook.py       weakened daemons against the battery's own audit-hook detector (check())
runs/eval/pgr1/atk/a4_static.py     evasions of import_violations / marker_violations
runs/eval/pgr1/atk/a5_inside.py, a9_m8.py   --root/--state overlap with 8.3, case, trailing dot, slash aliases (tree and mutant)
runs/eval/pgr1/atk/a6_ctrl.py       detectors that have no positive control (state_extra, log_broken, daemon_dead, socket connect, os.system)
runs/eval/pgr1/atk/a7_misc.py, a8_junction_read.py   partial line then EOF, 70 begins, junctions inside root pointing outside and at the state folder
runs/eval/pgr1/mutate.py + runmut.sh   14 mutants of gated.py, each in its own copy, full battery run with PYTHONPATH=<mutant>/src (mas_device.__file__ checked); results in runs/eval/pgr1/mut/summary.txt

BROKE:
1. Rule 8, the credential list D-025 itself declares is not the one the code applies. D-025 lists `credentials*` and `.env*`. The code takes only the exact names
   credentials / credentials.json / credentials.db, and `.env` or `.env.` + anything. Through the daemon's stdin (model side, nothing else held):
     {"id":1,"method":"read","params":{"path":"credentials.csv"}}     -> ok, content_b64 = the file's bytes
   The same for credentials.xml, credentials.yaml, credentials.txt, aws/credentials.csv, .envrc, .envrc.local, .env-prod, .env_local.
   plan_write leaks them as well, without any approval:
     {"id":2,"method":"plan_write","params":{"files":{".envrc":"eA=="}}}  -> action.undo.files[0].content_b64 = the old bytes
   (credentials.csv is the file name AWS gives the access-key download, and .envrc is where direnv keeps secrets.)

WEAK TESTS (each mutant passes all 26 tests and fails open):
1. m1_dirs: CREDENTIAL_DIRS cut down to {.ssh,.gnupg,.aws}. After that, .azure, .kube and .password-store (all three listed in D-025) are read with the suite green.
   a1_creds checks that the tree still refuses them, so the test is the only thing that misses this.
2. m2_suffix: CREDENTIAL_SUFFIXES cut down to (.pem,.key,.pfx,.kdbx). Then *.p12 (listed in D-025), .ppk, .jks, .keystore, .kdb and .keychain are readable, suite green.
3. m3_names: "cookies", "key4.db", "shadow", "ntds.dit" (all four listed in D-025) and local state, web data, key3.db, gshadow and .pgpass are dropped, suite green.
   CRED_FILES holds 16 fixtures. Every list entry it does not exercise is an untested guard.
4. m6_readlog: the read record becomes {"path":"?"}, so path and sha256 are gone, and the suite stays green. D-025 says successful reads are logged with path
   and sha256. Rule 5 asks for a complete log, and the tests only check that a record of type "read" exists.
5. m7_reqlog: approval.requested loses action_hash and the suite stays green. D-025 says begin is logged with class, action hash and summary.
6. m8_inside: _inside uses abspath instead of realpath+normcase, and the suite stays green. With that mutant, a --state written as the 8.3 short name or in
   another case of a folder inside --root returns inside=False (a9_m8.py), so the daemon starts with its log where approved writes can reach it.
   The tree is correct here. Only the test is missing: test_daemon_refuses_a_log_the_model_could_write passes only exact resolved paths.
Killed, recorded so they are not re-run: m4 (catch-all removed; only the native_code control kills it, by accident), m10 (anonymous refusals unlogged),
m11 (method not logged), m12 (bool/huge ids), m14 (.env.* dropped). Also the 5 saboteurs in criteria.yaml, per status.
Detectors that have no positive control: state_extra, log_broken, daemon_dead/daemon_unhealthy and anonymous_refusal_unlogged. I checked by hand that the first
three fire (a6_ctrl). D-025 says "each detector is shown to fire", and these four are not shown.

LABELS:
1. D-025 says this: "any file write/rename/delete/mkdir outside the root and state folders [is] recorded and blocked ... native code loading ... so even a weakened
   build cannot touch the dev machine". The battery's docstring makes the same claim. Neither is true. Each of these weakened daemons ran under the battery's launcher,
   tg.check() returned [] (no finding at all), and the effect happened outside the root (a3_hook.py):
   - _winapi.CreateJunction(TMP/outside_dir, root/j), then open(root/j/pwned.txt, "w"), then os.rmdir(root/j). A file is created outside the root and the root is
     byte-identical. The event is not in BLOCK, and inside() compares paths as text without resolving links.
   - os.open(TMP/victim.txt, os.O_RDONLY | os.O_TEMPORARY), then close. The file outside the root is deleted, because O_TEMPORARY is not in WRITE_FLAGS.
   - os.kill(pid, 9) kills any process of the same user. Neither "os.kill" nor "_winapi.OpenProcess"/"_winapi.TerminateProcess" is blocked, and that matters for
     rule 8 (killing security software).
   - import __main__; __main__.busy[0] = True switches the hook off, after which an outside write goes through.
   - Only ctypes.* is blocked as "native code loading". Extension-module imports (the "import" event) are not checked.
   An audit hook is not a sandbox. Either harden the hook or say "detects accidental weakening; not a containment boundary".
2. The static checks are described as imports limited to an allowlist "with no ... subprocess". Device code can start processes through the allowed os module
   (os.system, os.popen, os.startfile, os.spawnv), or reach __import__ through __builtins__["__imp" + "ort__"] or getattr(sys.modules["builtins"], ...).
   All of these give import_violations == [] (a4_static). The markers are beaten by splitting a string ("sch" + "tasks", "CurrentVersion" + "/Run") or by
   using forward slashes in the Run key path. This is not reachable from the model, so it is a label/strength issue, not a break.
3. SIMULATED labels in gated.py, the battery and criteria.yaml are fine.

NOTES (no verdict weight):
- The RPC surface held against everything in a2_rpc: daemon alive, log intact, one rpc.refused per refusal, oversized lines dropped with the stream kept in
  sync, duplicate keys refused at every depth. Junctions inside root that point outside or at the state folder are refused for read and plan_write (a8).
  _inside in the tree handles 8.3, case, trailing-dot and forward-slash aliases.
- Equivalent or fail-closed mutants: m5 (no duplicate-key hook; what gets approved is still what gets executed, both being the parsed object), m13 (extra
  top-level request keys ignored), m9 (no readline limit, so memory DoS only).
- These are outside D-025's declared list (name-only limit, declared) but readable from the pipe: .docker/config.json, .npmrc, .pypirc,
  gcloud application_default_credentials.json, client_secret.json, token.json, .config/gh/hosts.yml, the SYSTEM and SECURITY hives (SYSTEM is the key to SAM),
  SAM.LOG1, Firefox cookies.sqlite, id_rsa.bak/id_ed25519.old, known_hosts, .bash_history, DPAPI Microsoft/Protect, NTUSER.DAT. Worth extending.
- rpc.refused does not record the path, so the owner sees "read credential_path" without knowing which store was tried. That matches the spec, but the log
  is thinner than it could be.
- The model can fill the 64 pending slots (too_many_pending) and grow the log without limit (about 30 refusals/s). DoS only, fail-closed.
- If the model closes its read end, out.write raises and the daemon dies with a traceback. Fail-closed.

REQUIRED BEFORE PASS:
1. Implement D-025's credential list as declared: `credentials*` and `.env*` as prefixes (or narrow D-025 to what the code does, and justify that). Add
   credentials.csv, .envrc and .env-prod style fixtures.
2. Test every CREDENTIAL_DIRS, CREDENTIAL_NAMES and CREDENTIAL_SUFFIXES entry through the daemon (read and plan_write), so that m1, m2 and m3 are killed.
3. Assert the read record's path and sha256 and the approval.requested record's class, action_hash and summary, so that m6 and m7 are killed.
4. Add a --root/--state overlap case using an 8.3 short name and a case variant (and a junction), so that m8 is killed.
5. Either harden the audit-hook launcher or correct the claim in D-025 and in the battery docstring. Hardening means: block _winapi.CreateJunction, os.kill,
   _winapi.OpenProcess/TerminateProcess; treat O_TEMPORARY as a write; resolve realpath in inside(); check the import event for extension modules outside
   the stdlib; keep busy out of reach. Correcting the claim means dropping "even a weakened build cannot touch the dev machine" and stating what the hook
   misses. Add positive controls for each event you add, and for state_extra, log_broken and daemon_dead.
6. Either extend the static check to flag os.system/popen/spawn*/exec*/startfile/kill and dynamic __builtins__/sys.modules["builtins"] access, or reword
   D-025's "no subprocess" static claim.
