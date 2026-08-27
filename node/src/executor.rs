use std::time::Duration;

use rand::SeedableRng;
use rand::rngs::StdRng;
use wasmtime::component::{Component, Linker as ComponentLinker, Val};
use wasmtime::{Config, Engine, Linker, Module, Store};
use wasmtime_wasi::preview1::WasiP1Ctx;
use wasmtime_wasi::{
    HostMonotonicClock, HostWallClock, IoView, ResourceTable, WasiCtx, WasiCtxBuilder, WasiView,
};

use crate::crypto;

/// The result of a successful WASM execution.
pub struct ExecutionResult {
    /// Raw bytes captured from the guest's stdout.
    pub output: Vec<u8>,
    /// Number of fuel units consumed (proxy for instruction count).
    pub instruction_count: u64,
    /// SHA-256 hex digest of the guest's linear memory after execution.
    pub memory_hash: String,
}

/// Default fuel budget when no timeout is specified (1 billion units).
const DEFAULT_FUEL: u64 = 1_000_000_000;

/// The version/layer bytes that follow `\0asm` in a component, as opposed to
/// `01 00 00 00` in a core module.
const COMPONENT_VERSION: [u8; 4] = [0x0d, 0x00, 0x01, 0x00];

/// Execute a WASM binary in a sandboxed Wasmtime runtime.
///
/// `payload` is the (decrypted) TNDM-framed payload: the WASM bytes followed by
/// the JSON input fed to the guest. `timeout_ms` becomes a fuel budget of
/// `timeout_ms × 10 000`.
pub fn execute_wasm(
    payload: &[u8],
    timeout_ms: Option<u64>,
) -> Result<ExecutionResult, Box<dyn std::error::Error>> {
    let (wasm_bytes, input_bytes): (&[u8], &[u8]) =
        if payload.starts_with(b"TNDM") && payload.len() >= 8 {
            let mut len_bytes = [0u8; 4];
            len_bytes.copy_from_slice(&payload[4..8]);
            let wasm_len = u32::from_le_bytes(len_bytes) as usize;
            if payload.len() >= 8 + wasm_len {
                (&payload[8..8 + wasm_len], &payload[8 + wasm_len..])
            } else {
                (payload, &[])
            }
        } else {
            (payload, &[])
        };

    let fuel_budget = timeout_ms.map_or(DEFAULT_FUEL, |ms| ms * 10_000);

    if is_component(wasm_bytes) {
        run_component(wasm_bytes, input_bytes, fuel_budget)
    } else {
        run_core_module(wasm_bytes, input_bytes, fuel_budget)
    }
}

fn is_component(wasm_bytes: &[u8]) -> bool {
    wasm_bytes.len() >= 8 && wasm_bytes[4..8] == COMPONENT_VERSION
}

/// A clock frozen at the Unix epoch, so a guest can't read the real time.
///
/// Verification reruns a task on three nodes and bans whoever disagrees with
/// the majority, so a guest that sees the host clock gets honest nodes banned
/// for running a second apart.
struct FrozenClock;

impl HostWallClock for FrozenClock {
    fn resolution(&self) -> Duration {
        Duration::from_secs(1)
    }

    fn now(&self) -> Duration {
        Duration::ZERO
    }
}

impl HostMonotonicClock for FrozenClock {
    fn resolution(&self) -> u64 {
        1_000_000_000
    }

    // ponytail: frozen rather than ticking, so a guest spinning on elapsed time
    // burns its fuel budget. Swap in an atomic counter if a task needs it.
    fn now(&self) -> u64 {
        0
    }
}

/// A WASI context that behaves identically on every node: frozen clocks, and
/// both random interfaces seeded from the task input.
///
/// Wasmtime's default wires the guest to the host clock and entropy pool, which
/// redundant execution can't live with. Seeding from the input keeps two nodes
/// running the same task in step while different tasks still get different
/// streams.
fn deterministic_wasi_builder(input_bytes: &[u8]) -> WasiCtxBuilder {
    let secure_seed = crypto::sha256_bytes(input_bytes);
    // Separate seed, so reading both interfaces doesn't hand back the same bytes.
    let insecure_seed = crypto::sha256_bytes(&secure_seed);

    let mut builder = WasiCtxBuilder::new();
    builder
        .wall_clock(FrozenClock)
        .monotonic_clock(FrozenClock)
        .secure_random(StdRng::from_seed(secure_seed))
        .insecure_random(StdRng::from_seed(insecure_seed))
        .insecure_random_seed(u128::from_le_bytes(
            insecure_seed[..16].try_into().expect("16 of 32 bytes"),
        ));
    builder
}

/// Turn a wasmtime run error into our own error, treating a clean WASI exit as
/// success: a normal WASI program finishes by calling `proc_exit`, which comes
/// back as an `I32Exit` rather than a plain return.
fn interpret_run_error(err: wasmtime::Error) -> Result<(), Box<dyn std::error::Error>> {
    if let Some(exit) = err.downcast_ref::<wasmtime_wasi::I32Exit>() {
        if exit.0 == 0 {
            return Ok(());
        }
        return Err(format!("guest exited with status {}", exit.0).into());
    }

    let msg = format!("{err}");
    if msg.contains("fuel") {
        return Err("Fuel exhausted: execution exceeded instruction budget".into());
    }
    Err(format!("WASM trap: {msg}").into())
}

/// Run a classic core WASM module through WASI preview1: stdin in, stdout
/// captured, no filesystem or sockets.
fn run_core_module(
    wasm_bytes: &[u8],
    input_bytes: &[u8],
    fuel_budget: u64,
) -> Result<ExecutionResult, Box<dyn std::error::Error>> {
    let mut engine_config = Config::new();
    engine_config.consume_fuel(true);
    let engine = Engine::new(&engine_config)?;

    let module = Module::from_binary(&engine, wasm_bytes)?;

    let stdout_buf = wasmtime_wasi::pipe::MemoryOutputPipe::new(1024 * 1024); // 1 MiB cap
    let stdin_buf = wasmtime_wasi::pipe::MemoryInputPipe::new(input_bytes.to_vec());

    let wasi_ctx = deterministic_wasi_builder(input_bytes)
        .stdin(stdin_buf)
        .stdout(stdout_buf.clone())
        .build_p1();

    let mut store = Store::new(&engine, wasi_ctx);
    store.set_fuel(fuel_budget)?;

    let mut linker: Linker<WasiP1Ctx> = Linker::new(&engine);
    wasmtime_wasi::preview1::add_to_linker_sync(&mut linker, |ctx| ctx)?;

    let instance = linker.instantiate(&mut store, &module)?;

    // `_start` is the WASI command convention, `tandem_entry` the Python SDK's.
    let start = instance
        .get_typed_func::<(), ()>(&mut store, "_start")
        .or_else(|_| instance.get_typed_func::<(), ()>(&mut store, "tandem_entry"))
        .map_err(|_| "module does not export a `_start` or `tandem_entry` function")?;

    if let Err(err) = start.call(&mut store, ()) {
        interpret_run_error(err)?;
    }

    let fuel_remaining = store.get_fuel()?;
    let instruction_count = fuel_budget.saturating_sub(fuel_remaining);

    let memory_hash = if let Some(memory) = instance.get_memory(&mut store, "memory") {
        let data = memory.data(&store);
        crypto::sha256_hex(data)
    } else {
        crypto::sha256_hex(&[])
    };

    // Dropping the store releases the WASI pipes, so stdout can be taken out.
    drop(store);
    let output: Vec<u8> = stdout_buf.try_into_inner().unwrap_or_default().into();

    Ok(ExecutionResult {
        output,
        instruction_count,
        memory_hash,
    })
}

/// The host state a component's WASI imports run against.
struct ComponentHost {
    ctx: WasiCtx,
    table: ResourceTable,
}

impl IoView for ComponentHost {
    fn table(&mut self) -> &mut ResourceTable {
        &mut self.table
    }
}

impl WasiView for ComponentHost {
    fn ctx(&mut self) -> &mut WasiCtx {
        &mut self.ctx
    }
}

/// Run a WASM component (the wasip2 world) by calling its `run` export.
///
/// Every Tandem task component exports the same contract whatever language it
/// came from: `run(list<u8>) -> list<u8>`, JSON in, JSON out. We call it by name
/// through the dynamic component API so the node needs no WIT at build time.
fn run_component(
    wasm_bytes: &[u8],
    input_bytes: &[u8],
    fuel_budget: u64,
) -> Result<ExecutionResult, Box<dyn std::error::Error>> {
    let mut engine_config = Config::new();
    engine_config.consume_fuel(true);
    let engine = Engine::new(&engine_config)?;

    let component = Component::from_binary(&engine, wasm_bytes)?;

    // stderr is captured rather than inherited: our WIT contract has no error
    // variant, so a traceback here is all we can report on a trap.
    let stderr_buf = wasmtime_wasi::pipe::MemoryOutputPipe::new(64 * 1024);
    let ctx = deterministic_wasi_builder(input_bytes)
        .stderr(stderr_buf.clone())
        .build();
    let host = ComponentHost {
        ctx,
        table: ResourceTable::new(),
    };
    let mut store = Store::new(&engine, host);
    store.set_fuel(fuel_budget)?;

    let mut linker: ComponentLinker<ComponentHost> = ComponentLinker::new(&engine);
    wasmtime_wasi::add_to_linker_sync(&mut linker)?;

    let instance = linker.instantiate(&mut store, &component)?;

    let run = instance
        .get_func(&mut store, "run")
        .ok_or("component does not export a `run` function")?;

    let input_val = Val::List(input_bytes.iter().map(|byte| Val::U8(*byte)).collect());
    let mut results = [Val::Bool(false)];

    if let Err(err) = run.call(&mut store, &[input_val], &mut results) {
        let stderr_text = String::from_utf8_lossy(&stderr_buf.contents()).into_owned();
        interpret_run_error(err).map_err(|e| -> Box<dyn std::error::Error> {
            if stderr_text.trim().is_empty() {
                e
            } else {
                format!("{e}\nguest stderr:\n{}", stderr_text.trim()).into()
            }
        })?;
        return Err("component `run` did not return a result".into());
    }
    run.post_return(&mut store)?;

    let output: Vec<u8> = match &results[0] {
        Val::List(items) => items
            .iter()
            .map(|value| match value {
                Val::U8(byte) => *byte,
                _ => 0,
            })
            .collect(),
        _ => return Err("component `run` returned an unexpected type".into()),
    };

    let fuel_remaining = store.get_fuel()?;
    let instruction_count = fuel_budget.saturating_sub(fuel_remaining);

    drop(store);

    // Components expose no single linear memory, so there's nothing to hash. The
    // output hash, fuel count, and signed receipt are what guard against tampering.
    let memory_hash = crypto::sha256_hex(&[]);

    Ok(ExecutionResult {
        output,
        instruction_count,
        memory_hash,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    // A wasip1 core module that echoes stdin.
    const ECHO_MODULE: &[u8] = include_bytes!("../tests/fixtures/echo_module.wasm");

    // A wasip2 component that echoes through Tandem's `run` contract.
    const TASK_COMPONENT: &[u8] = include_bytes!("../tests/fixtures/task_run_component.wasm");

    // Reads randomness and the wall clock into linear memory, so the memory hash
    // `run_core_module` computes tells us whether two runs saw the same values.
    const NONDETERMINISM_PROBE: &str = r#"
        (module
          (import "wasi_snapshot_preview1" "random_get"
            (func $random_get (param i32 i32) (result i32)))
          (import "wasi_snapshot_preview1" "clock_time_get"
            (func $clock_time_get (param i32 i64 i32) (result i32)))
          (memory (export "memory") 1)
          (func (export "_start")
            (drop (call $random_get (i32.const 0) (i32.const 32)))
            (drop (call $clock_time_get (i32.const 0) (i64.const 0) (i32.const 64)))))
    "#;

    // "TNDM" magic, little-endian wasm length, the wasm, then the input.
    fn frame(wasm: &[u8], input: &[u8]) -> Vec<u8> {
        let mut payload = Vec::new();
        payload.extend_from_slice(b"TNDM");
        payload.extend_from_slice(&(wasm.len() as u32).to_le_bytes());
        payload.extend_from_slice(wasm);
        payload.extend_from_slice(input);
        payload
    }

    #[test]
    fn detects_component_vs_core_module() {
        assert!(is_component(TASK_COMPONENT));
        assert!(!is_component(ECHO_MODULE));
    }

    #[test]
    fn runs_a_core_module_and_captures_stdout() {
        let payload = frame(ECHO_MODULE, b"hello from a core module");
        let result = execute_wasm(&payload, None).expect("core module should run");
        assert_eq!(result.output, b"hello from a core module");
        assert!(result.instruction_count > 0);
    }

    #[test]
    fn runs_a_component_via_run_export() {
        let payload = frame(TASK_COMPONENT, b"hello from a component");
        let result = execute_wasm(&payload, None).expect("component should run");
        assert_eq!(result.output, b"hello from a component");
        assert!(result.instruction_count > 0);
    }

    #[test]
    fn the_guest_sees_the_same_clock_and_randomness_every_run() {
        let probe = wat::parse_str(NONDETERMINISM_PROBE).expect("probe should assemble");

        // Same task twice: what verification compares across three nodes.
        let first = execute_wasm(&frame(&probe, b"task input"), None).expect("probe should run");
        let second = execute_wasm(&frame(&probe, b"task input"), None).expect("probe should run");
        assert_eq!(first.memory_hash, second.memory_hash);

        // A different task still gets its own randomness, or every shard of a
        // split would compute with the same numbers.
        let other = execute_wasm(&frame(&probe, b"other input"), None).expect("probe should run");
        assert_ne!(first.memory_hash, other.memory_hash);
    }
}
