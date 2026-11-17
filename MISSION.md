# MISSION.md — MAS portátil: el aparato (hardware + software) de punta a punta

> Para Claude Code. Modelo principal: Opus 5.5. Advisor: Fable 5.2 mientras esté disponible.
> Dueño del proyecto: Martín Josué Arcos.
>
> Este archivo es la fuente de verdad de la misión. Cada sesión nueva empieza leyéndolo, junto con `PROGRESS.md`, `DECISIONS.md` y `STEER.md`.
> Todo lo marcado como "punto de partida" lo puedes cambiar con razones. La sección 4 (Reglas fijas) no se cambia.
> Este archivo puede tener datos desactualizados: verifica comandos, versiones, precios y licencias por tu cuenta. Donde el código o la documentación oficial contradigan este archivo, mandan ellos.

---

## 1. Qué construimos

Un aparato portátil, con hardware y software propios, que lleva la IA de su dueño: modelo local, harness de agentes y memoria. Al conectarlo a otra máquina, y solo con permiso del dueño de esa máquina, hace tres cosas:

1. **Conecta tu IA allí.** La máquina anfitriona puede usarla sin instalar nada.
2. **Inspecciona ese hardware y software con IA**, en solo lectura.
3. **Facilita el auto-setup de MAS** (Multi Auto Setup) en esa máquina para sumarla a la flota.

Los límites los impone el aparato, no el modelo.

**Forma:** toma la de productos que ya existen y mejora hardware y software: Tiiny AI Pocket Lab, PocketLLM (USB), Project NOMAD, MacPaw Portable Memory.

**Base:** el repo `josuecuguy1307/Multi-Auto-Setup`. El aparato es, en esencia, MAS empaquetado en una placa portátil más un botón físico de aprobación.

**Contexto mayor** (no lo construyas ahora, pero deja interfaces limpias): MAS debe llegar a mejorarse a sí mismo, desplegarse y coordinar varias IAs. Este aparato es su pieza física.

## 2. Qué significa "terminado"

Todo verificado por pruebas deterministas, no por tu palabra:

- **A. Software v0:** tu IA corre desde un medio portátil en una máquina anfitriona, sin instalar nada y sin dejar rastro.
- **B. Inspección con permiso:** inventario e informe de la máquina anfitriona, en solo lectura.
- **C. Auto-setup de MAS:** con aprobación del dueño, escritura por escritura, y con deshacer.
- **D. Firmware de la llave:** el botón físico es la única forma de aprobar una escritura. Probado en emulador.
- **E. Hardware:** esquemático, PCB, lista de materiales y carcasa, con chequeos automáticos en verde y lista de revisión humana.
- **F. Evals y artículo de investigación**, reproducibles con un comando.
- **G. Repo ordenado:** README, `THIRD_PARTY.md` e instrucciones para reproducir todo.

## 3. Tu libertad

- Tú decides arquitectura, lenguajes, estructura, herramientas, qué repos reutilizar y cuántos subagentes lanzar.
- Investiga antes de escribir: lee MAS entero, busca en la web, clona y estudia repos. Reutiliza antes que reescribir.
- Si encuentras algo mejor que lo listado aquí, úsalo y anota el porqué en `DECISIONS.md`.
- No hay calendario ni tramos de tiempo. Trabaja sin parar, unidad tras unidad, hasta terminar.
- No pidas permiso para avanzar. Si algo requiere a Josué, anótalo en `NEEDS_HUMAN.md` y sigue con otra unidad.
- Instala lo que te falte dentro de tu sandbox o contenedor.

## 4. Reglas fijas

### 4.1 Guardrails del producto

Son requisitos del aparato y cada uno debe tener pruebas automáticas.

1. **Nada ocurre en la máquina anfitriona sin una acción explícita de su dueño.** El aparato nunca se ejecuta solo, nunca se presenta como teclado o ratón, nunca usa autoarranque.
2. **Solo lectura por defecto.** Cada escritura necesita una aprobación nueva, acción por acción. En el aparato, aprobar es pulsar el botón físico.
3. **La aprobación va firmada y atada a la acción exacta** (su hash), es de un solo uso y caduca. El modelo no puede reutilizarla.
4. **Los límites viven en el firmware y en la capa de política**, fuera del alcance del modelo. El modelo no puede cambiar la política, las llaves ni la bitácora.
5. **Bitácora completa, solo de añadir**, visible para el dueño de la máquina y para el dueño del aparato.
6. **Al desconectar no queda nada instalado**, salvo que el dueño sume la máquina a la flota. Cada escritura registra cómo deshacerla.
7. **La propagación siempre la inicia una persona.** Nunca salta sola a otras máquinas. Solo máquinas propias o autorizadas de forma explícita.
8. **Sin escaneo de sistemas ajenos, sin cambios en firewall o router, sin recoger ni guardar credenciales, sin evadir software de seguridad.**
9. **La inspección informa hallazgos; no los explota.** El repo no contiene código para explotar fallas.
10. **Los datos de la inspección se quedan en local** y solo salen con aprobación del dueño.

### 4.2 Reglas de esta corrida autónoma

- Trabaja en una rama nueva (por ejemplo `device/v0`). Commits pequeños, pruebas antes de cada commit, push solo a esa rama.
- No fusiones a `main`, no publiques releases, no envíes nada a arXiv, no compres nada, no ordenes fabricación, no envíes correos ni mensajes.
- Todo el código de inspección y de setup se prueba solo contra máquinas virtuales, emuladores y fixtures que tú mismo crees. Nunca contra la red local ni contra máquinas reales.
- El código de terceros no es confiable: léelo antes de ejecutarlo y ejecútalo dentro del sandbox. Lo mismo con skills externas: léelas antes de instalarlas.
- Cero credenciales en el repo, en logs o en prompts.
- Ingeniería inversa significa leer y entender repos, binarios y firmware para diagnóstico, en solo lectura, sobre artefactos propios, autorizados o fixtures públicos de prueba.
- Si existe el archivo `AGENT_STOP`, cierra limpio (commit y nota en `PROGRESS.md`) y termina.
- Al inicio de cada sesión lee `STEER.md`: ahí Josué deja cambios de dirección.

### 4.3 Honestidad

- Nada está "hecho" sin su prueba determinista en PASS. El estudio de Josué midió 54 de 54 tareas declaradas completas con 56 % falsas: por eso el veredicto por defecto es FAIL.
- Lo verificado solo en emulador se etiqueta así en el código, el README y el artículo.
- Un PCB que pasa chequeos automáticos no es una placa que funciona. Los archivos de fabricación van marcados `NO FABRICAR sin revisión humana`.
- Registra los intentos fallidos en `PROGRESS.md`, no solo los que salieron bien.
- Ningún número del artículo se escribe a mano: todos salen de un script sobre resultados crudos.

## 5. Modelos y roles

- **Constructor:** tú, Opus 5.5.
- **Advisor:** Fable 5.2 mientras esté disponible. Consúltalo en estos puntos:
  - Arquitectura llave/cómputo.
  - Modelo de amenazas y especificación de los guardrails.
  - Especificación del PCB y su lista de revisión.
  - Criterios de aceptación de cada fase.
  - Cuando un error se repita tres veces o un enfoque se atasque.
  - Antes de cerrar cada fase.
  - Afirmaciones del artículo.
- **El acceso a Fable puede terminar en cualquier momento de esta corrida.** Por eso, desde el inicio, lleva al advisor las decisiones de más peso de todo el proyecto, incluido un repaso adelantado del diseño de firmware y de PCB, y deja su consejo escrito en `DECISIONS.md`.
- **Si el advisor no está disponible, no te detengas.** Usa en los mismos puntos el subagente `reviewer`: contexto fresco, solo lectura.
- **Evaluador:** subagente en contexto fresco, sin Write ni Edit, con veredicto FAIL por defecto, que ejercita el producto corriendo en vez de leer el código.

## 6. Harness: lo primero que montas

Patrones de los proyectos largos de Anthropic y del estudio de Josué. Adáptalos y quita lo que no cargue peso.

**Archivos de continuidad**
- `CLAUDE.md`: corto. Cómo se trabaja aquí, comandos, dónde está cada cosa. Apunta a este archivo.
- `PROGRESS.md`: bitácora de laboratorio, con intentos fallidos. Es la nota que una sesión le deja a la siguiente.
- `DECISIONS.md`: decisiones, alternativas descartadas y consejo del advisor.
- `ACCEPTANCE.md` y `test-results.json`: criterios por fase, todos en FAIL al inicio. Solo un script determinista los pasa a PASS.
- `NEEDS_HUMAN.md`, `STEER.md`, `THIRD_PARTY.md`.

**Mecanismos**
- Bucle de continuidad: sesiones nuevas con contexto fresco, una tras otra, dentro de un contenedor, hasta que todo `ACCEPTANCE.md` esté en PASS o exista `AGENT_STOP`. Punto de partida: un `run.sh` con `claude -p`, o `/goal`. Verifica tus propias opciones con `claude --help`.
- Hooks: commit al parar, compuerta que impide declarar terminada una unidad sin su prueba en PASS, interruptor `AGENT_STOP`, lectura de `STEER.md`. Un hook Stop que bloquea deja de aplicarse tras 8 bloqueos seguidos: no dependas solo de él.
- Subagentes en `.claude/agents/`: `evaluator`, `reviewer` y los que decidas.
- Skills en `.claude/skills/` que te des a ti mismo: PCB como código y chequeos de KiCad, recetas de emulación, inspección en solo lectura, lectura de repos y firmware desconocidos, licencias.

**Disciplina**
- Oráculo de pruebas antes que código. El verificador tiene que ser casi perfecto, porque optimizarás contra él.
- Muchos intentos baratos contra un verificador externo: fue lo que mejor rindió en el estudio de Josué.
- Paralelismo solo para tareas independientes. El trabajo acoplado lo hace un agente en secuencia. En el estudio, multiagente fue igual o peor en 74 % de 516 comparaciones.
- Si paralelizas, usa bloqueos por archivo en `current_tasks/` vía git, sin orquestador central.
- Higiene de contexto: imprime pocas líneas, manda los logs a archivos, usa `ERROR` buscable con grep.
- No tienes noción del tiempo: ten un subconjunto rápido de pruebas para cada ciclo y la suite completa para cerrar fase.

## 7. Fases

Es un orden, no un calendario. Avanza sin esperar a nadie.

### Fase 1 — Reconocimiento, decisiones y harness
- Lee el repo MAS entero. Detecta la máquina donde corres: sistema, RAM, GPU, herramientas.
- Clona y estudia los repos de la sección 8. Escribe `THIRD_PARTY.md`.
- Decide con el advisor: arquitectura, modelo de amenazas, especificación de guardrails, especificación del PCB, criterios de aceptación de todas las fases.
- Monta el harness.
- **Aceptación:** una unidad de prueba recorre el ciclo completo (prueba en FAIL, construcción, evaluador, PASS, commit). `DECISIONS.md` y `ACCEPTANCE.md` completos y revisados.

### Fase 2 — Software v0: tu IA en una memoria
- Arranque con un comando desde el medio portátil, sin instalar nada en la máquina anfitriona.
- Dos modos, elegidos por un perfilador de la máquina: el cómputo lo pone el aparato o lo pone la máquina anfitriona.
- Modelo local, harness de agentes y memoria portable en formato abierto.
- Flujo de permiso de MAS: emparejamiento, aprobación por escritura, deshacer.
- Auto-setup de MAS en la máquina anfitriona con esas aprobaciones.
- **Aceptación:** funciona en máquinas virtuales limpias. Al desconectar, el diff del sistema de archivos queda vacío fuera de lo aprobado. El flujo corre entre dos o más máquinas virtuales en red aislada. La suite de guardrails da cero violaciones.

### Fase 3 — Inspección con permiso
- Inventario de hardware, sistema, firmware y software; lista de componentes de software; vulnerabilidades conocidas.
- Informe legible para el dueño y consumible por el agente.
- **Aceptación:** precisión y recobrado medidos contra máquinas virtuales y fixtures con verdad conocida. Cero escrituras en la máquina inspeccionada, verificado por diff y por montaje de solo lectura.

### Fase 4 — Firmware de la llave
- El botón físico como única vía de aprobación, con la firma descrita en 4.1.
- Diseño de arranque seguro documentado.
- Camino de pruebas: simulacros de la capa de hardware, luego QEMU, luego Renode.
- **Aceptación:** una aprobación repetida, alterada o vencida se rechaza. Ningún camino del modelo cambia política, llaves ni bitácora.

### Fase 5 — Hardware
- Esquemático y PCB como código, lista de materiales con disponibilidad verificada, carcasa.
- Flujo de referencia (Anthropic con Diode): especificación, esquemático, colocación, ruteo, carcasa, firmware y materiales, chequeos de fabricabilidad y de abastecimiento.
- **Aceptación:** chequeos eléctricos, de reglas de diseño y de fabricabilidad en verde. Archivos de fabricación generados y marcados `NO FABRICAR`. `HUMAN_REVIEW_PCB.md` con la lista de revisión.

### Fase 6 — Evals y artículo
Ver sección 9.

## 8. Stack y repos de partida

Punto de partida, no cerca. Confirma cada licencia al clonar. En su repo, Josué ya tiene forks de listas útiles: `awesome-agent-harness` y `awesome-local-ai-agents`.

### 8.1 El repo MAS, según su página (el código manda)
- Paquete Python `envrec`, Python 3.11 o superior, con uv.
- Un coordinador en un puerto de la red local (TCP 7443) y agentes de nodo que marcan hacia afuera sin puerto abierto.
- Emparejamiento por código de 6 dígitos de un solo uso (SPAKE2) con aprobación del dueño. Mensajes firmados sobre TLS mutuo con llaves fijadas y protección contra repetición.
- Directorios: `src/envrec_coordinator`, `envrec_node`, `envrec_net`, `envrec_mas` (comando `mas`), `envrec_os`, `envrec`, más `skills/`, `conformance/`, `tests/`.
- Sin verificar allí: Windows, flujo completo entre dos máquinas físicas, corridas de modelo de punta a punta.
- No muestra licencia: propón una en `NEEDS_HUMAN.md`, no la decidas.

### 8.2 Repos de terceros

| Capa | Repo | Qué aporta | Licencia |
|---|---|---|---|
| IA local portátil | `vraj00222/pocketllm` | Lanzador de un comando desde USB, sin instalación | MIT |
| | `Looming41/pocketllm` | Perfilador de la máquina anfitriona; la USB solo almacena | MIT |
| | `ggml-org/llama.cpp` | Motor de inferencia | MIT |
| | `Crosstalk-Solutions/project-nomad` | Panel que instala y actualiza servicios en contenedores; no trae autenticación | Apache 2.0 |
| Memoria portable | `MacPaw/portable-memory` | Formato `.mem`, SDK en Python | MIT |
| | `portable-ai-memory/portable-ai-memory`, ApertoMemory | Alternativas: intercambio en JSON; memoria cifrada del lado del cliente | Apache 2.0 / MIT |
| Harness de agentes | `earendil-works/pi` | Núcleo mínimo y extensible | MIT |
| | `pi-pod/pi-continual`, `PrimeIntellect-ai/prime-agent` | Subagentes persistentes; estado del harness que se refina | MIT |
| Llave con botón | `tillitis/tillitis-key1`, `tillitis/tk1-pcba` | Llave USB abierta con sensor táctil y arranque medido; planos KiCad | BSD-2-Clause; hardware CERN-OHL |
| | SoloKeys | Llave abierta con botón de confirmación; planos KiCad | Apache 2.0 y MIT; hardware CERN OHL / CC BY-SA |
| | `usbarmory/usbarmory`, `tamago`, `armory-boot` | Computadora USB con arranque seguro y TrustZone; firmware en Go sin sistema operativo | Hardware abierto; tipo BSD |
| Aparecer como USB | `raspberrypi/rpi-usb-gadget` | La Pi como adaptador Ethernet por USB (solo Ethernet) | Por confirmar |
| Inspección | Syft y Grype (Anchore), fwupd, osquery | Componentes de software, vulnerabilidades conocidas, firmware, inventario | Por confirmar |
| | `e-m-b-a/emba`, binwalk, Ghidra | Análisis de firmware y binarios | GPL-3.0 por confirmar; MIT; por confirmar |
| PCB como código | `diodeinc/pcb` (Zener), skill de KiCad de Diode, kicad-happy, servidores MCP de KiCad, atopile, tscircuit, ngspice | Diseño, revisión y simulación | Por confirmar |
| Emulación | QEMU, Renode, Wokwi | Sistema completo; firmware con periféricos | Por confirmar |
| Bucle largo | `anthropics/claude-quickstarts` (autonomous-coding), `anthropics/cwc-long-running-agents` | Código de ejemplo del harness | El segundo no declara licencia: úsalo solo como referencia |

**Reglas de licencias**
- Permisivas (MIT, Apache, BSD): puedes copiar código con atribución.
- GPL: úsalas como herramientas externas; no copies su código dentro de MAS.
- Hardware con licencia recíproca: si derivas sus planos, los tuyos pueden tener que publicarse bajo la misma licencia. Decídelo y anótalo.

### 8.3 Plataforma física (propuesta de partida)
- Separar la **llave** (botón y arranque seguro, pequeña y barata) del **cómputo** (placa propia o la máquina anfitriona).
- Cómputo base: Raspberry Pi 5 de 16 GB. Opción: Jetson Orin Nano Super. Vuelve a verificar precios y rendimiento.
- Referencia de llave segura: USB Armory y TKey.

## 9. Evals y artículo

**Reglas de los evals** (del estudio de Josué): calificador externo y determinista, nunca el mismo modelo como juez, línea base, barras de error, y éxito en k corridas seguidas.

**Evals sugeridos** (cámbialos si encuentras mejores):
1. Guardrails: batería de intentos guionados de escribir sin aprobación, reutilizar una aprobación o alterar política y bitácora. Meta: cero violaciones.
2. Inspección: precisión y recobrado contra fixtures con verdad conocida.
3. Auto-setup: tasa de éxito en máquinas virtuales limpias, contra la instalación manual del README como línea base.
4. Rastro cero: diff del sistema de archivos antes y después.
5. Rendimiento del modelo local: solo donde haya hardware real; lo demás se declara no medido.
6. PCB: chequeos automáticos y lista de revisión, dejando claro que no hay validación eléctrica.

**Artículo**
- Fuente LaTeX y PDF en el repo, regenerables con un comando.
- Autores en el PDF del repo: Martín Josué Arcos y Claude Code.
- Prepara también una variante para arXiv con Claude Code en la declaración de métodos, porque arXiv no acepta IA como autor. El envío lo hace Josué.
- Sección de limitaciones obligatoria: qué se probó solo en emulador y qué no se probó.

## 10. Lo que depende de Josué

Anótalo en `NEEDS_HUMAN.md` con instrucciones exactas y sigue trabajando:
- Prueba real entre dos máquinas físicas.
- Compra de placas.
- Revisión humana del PCB antes de fabricar.
- Licencia del repo MAS.
- Envío a arXiv y aval de un autor establecido.

## 11. Referencias investigadas

Localiza por título las que no traen enlace.

- Anthropic, "Effective harnesses for long-running agents": https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents
- Anthropic, "Harness design for long-running application development": https://www.anthropic.com/engineering/harness-design-long-running-apps
- Anthropic, ejemplo de código: https://github.com/anthropics/claude-quickstarts/tree/main/autonomous-coding
- Anthropic, proyecto del compilador de C con agentes en paralelo (sesiones nuevas en bucle, GCC como oráculo, bloqueos por archivo).
- Anthropic con Diode Computers, "Claude can design electronic prototypes from start to finish" (9 de noviembre de 2026).
- Claude Code, advisor: https://code.claude.com/docs/en/advisor
- Claude Code, subagentes: https://code.claude.com/docs/en/sub-agents
- Raspberry Pi, modo gadget USB: https://www.raspberrypi.com/news/usb-gadget-mode-in-raspberry-pi-os-ssh-over-usb/
- Repo MAS: https://github.com/josuecuguy1307/Multi-Auto-Setup
- Estudio de Josué: "MAS post-hackathon: estudio y conclusiones, 12–16 nov 2026".
