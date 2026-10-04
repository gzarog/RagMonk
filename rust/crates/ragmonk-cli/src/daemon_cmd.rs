//! `ragmonk daemon start|stop|restart|status|run` (`ragmonk.cli.daemon`).
//!
//! `start` spawns `ragmonk daemon run` detached, logging to
//! `logs/daemon.out.log`, records its PID and waits for its first health
//! snapshot. `stop` signals it and waits. `status` reads the PID and health
//! files. Nothing talks to the daemon process directly.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use clap::Subcommand;
use ragmonk_core::errors::{ErrorKind, RagMonkError};
use ragmonk_core::paths::Home;
use ragmonk_indexing::coordinator::Options;
use ragmonk_indexing::daemon::{
    format_uptime, health, pid, CoordinatorRunner, Daemon, DaemonOptions,
};
use ragmonk_indexing::status::is_process_alive;
use ragmonk_storage::control::ControlPlane;
use ragmonk_storage::V2Layout;

use crate::{load, prepared_home, print_json};

const START_TIMEOUT: Duration = Duration::from_secs(30);
const STOP_TIMEOUT: Duration = Duration::from_secs(15);
const POLL: Duration = Duration::from_millis(100);

#[derive(Subcommand)]
pub enum DaemonCommand {
    /// Start the daemon in the background.
    Start,
    /// Stop the running daemon.
    Stop,
    /// Stop, then start.
    Restart,
    /// Report on the daemon from its PID and health files.
    Status {
        #[arg(long = "json")]
        json: bool,
    },
    /// Run the daemon in the foreground (what `start` spawns).
    Run,
}

pub fn run(cmd: DaemonCommand) -> Result<(), RagMonkError> {
    let home = prepared_home()?;
    match cmd {
        DaemonCommand::Start => start(&home),
        DaemonCommand::Stop => stop(&home),
        DaemonCommand::Restart => {
            stop(&home)?;
            start(&home)
        }
        DaemonCommand::Status { json } => status(&home, json),
        DaemonCommand::Run => run_foreground(&home),
    }
}

fn generic(e: impl std::fmt::Display) -> RagMonkError {
    RagMonkError::new(ErrorKind::Generic, e.to_string())
}

fn spawn(home: &Home) -> Result<u32, RagMonkError> {
    let log_path = home.logs_dir().join("daemon.out.log");
    std::fs::create_dir_all(home.logs_dir()).map_err(generic)?;
    let log = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .map_err(generic)?;
    let mut cmd = std::process::Command::new(std::env::current_exe().map_err(generic)?);
    cmd.args(["daemon", "run"])
        .stdin(std::process::Stdio::null())
        .stdout(log.try_clone().map_err(generic)?)
        .stderr(log);
    #[cfg(unix)]
    {
        // Own process group: survives this CLI exiting and a Ctrl+C sent
        // to the shell's group.
        use std::os::unix::process::CommandExt;
        cmd.process_group(0);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW);
    }
    Ok(cmd.spawn().map_err(generic)?.id())
}

fn start(home: &Home) -> Result<(), RagMonkError> {
    if let Some(existing) = pid::running_daemon(home) {
        println!("Daemon already running (pid {})", existing.pid);
        return Ok(());
    }
    // A stale snapshot from an earlier run must not count as "healthy".
    let _ = std::fs::remove_file(home.daemon_health());
    let child = i64::from(spawn(home)?);
    pid::write_pid_file(home, child).map_err(generic)?;
    let deadline = Instant::now() + START_TIMEOUT;
    let mut started = false;
    while Instant::now() < deadline {
        if !is_process_alive(child) {
            break;
        }
        if health::read_health(home).is_some() {
            started = true;
            break;
        }
        std::thread::sleep(POLL);
    }
    if !started {
        pid::remove_pid_file(home);
        return Err(RagMonkError::usage(format!(
            "daemon did not report healthy within {:.0}s; see {}",
            START_TIMEOUT.as_secs_f64(),
            home.logs_dir().join("daemon.out.log").display()
        )));
    }
    println!("Daemon started (pid {child})");
    println!("  Monitor via: ragmonk daemon status");
    Ok(())
}

fn stop(home: &Home) -> Result<(), RagMonkError> {
    let Some(info) = pid::running_daemon(home) else {
        pid::remove_pid_file(home);
        println!("Daemon is not running");
        return Ok(());
    };
    pid::signal_stop(info.pid);
    let deadline = Instant::now() + STOP_TIMEOUT;
    while Instant::now() < deadline && is_process_alive(info.pid) {
        std::thread::sleep(POLL);
    }
    if is_process_alive(info.pid) {
        return Err(RagMonkError::usage(format!(
            "daemon (pid {}) did not stop within {:.0}s",
            info.pid,
            STOP_TIMEOUT.as_secs_f64()
        )));
    }
    pid::remove_pid_file(home);
    println!("Daemon stopped (pid {})", info.pid);
    Ok(())
}

fn status(home: &Home, json: bool) -> Result<(), RagMonkError> {
    let data = ragmonk_indexing::daemon::status_payload(home);
    if json {
        return print_json(&data);
    }
    if data["running"].as_bool() == Some(true) {
        let uptime = data["uptime_seconds"].as_f64().unwrap_or(0.0);
        let uptime = if uptime == 0.0 {
            "0s".to_owned()
        } else {
            format_uptime(uptime)
        };
        println!("RUNNING pid={} uptime={uptime}", data["pid"]);
    } else {
        println!("STOPPED");
    }
    println!(
        "Last reconciliation: {}",
        data["last_reconciliation_at"].as_str().unwrap_or("-")
    );
    for s in data["sources"].as_array().into_iter().flatten() {
        let state = if s["online"].as_bool() == Some(true) {
            "online"
        } else {
            "OFFLINE"
        };
        println!(
            "  {} ({}) {state} last_pass={}",
            s["source_id"].as_str().unwrap_or_default(),
            s["source_type"].as_str().unwrap_or_default(),
            s["last_pass_at"].as_str().unwrap_or("-")
        );
    }
    Ok(())
}

fn stop_flag() -> Result<Arc<AtomicBool>, RagMonkError> {
    let flag = Arc::new(AtomicBool::new(false));
    #[cfg(unix)]
    for sig in [signal_hook::consts::SIGTERM, signal_hook::consts::SIGINT] {
        signal_hook::flag::register(sig, Arc::clone(&flag)).map_err(generic)?;
    }
    Ok(flag)
}

fn run_foreground(home: &Home) -> Result<(), RagMonkError> {
    let me = i64::from(std::process::id());
    if let Some(other) = pid::running_daemon(home).filter(|i| i.pid != me) {
        return Err(RagMonkError::usage(format!(
            "daemon already running (pid {})",
            other.pid
        )));
    }
    // `start` records the child's PID itself. A bare foreground run
    // records its own so that `status` and `stop` find it.
    if pid::read_pid_file(home).is_none_or(|i| i.pid != me) {
        pid::write_pid_file(home, me).map_err(generic)?;
    }
    let cfg = load(home)?;
    let _ = ragmonk_telemetry::logging::configure_logging(
        &home.logs_dir(),
        &cfg.runtime.log_level,
        "text",
    );
    let stop = stop_flag()?;
    let layout = V2Layout::new(home);
    let cache = cfg.runtime.sqlite_cache_size_mb;
    let open = || {
        ControlPlane::open(&layout, cache)
            .map(|(c, _)| c)
            .map_err(generic)
    };
    // The daemon's catalog connection and the worker's own connection.
    let (catalog, control) = (open()?, open()?);
    let runner = CoordinatorRunner {
        layout: layout.clone(),
        control,
        registry: ragmonk_convert::registry_with(
            &cfg,
            &ragmonk_convert::RegistryOptions::for_home(home),
        ),
        opts: Options::from_config(&cfg),
    };
    let daemon = Daemon::start(
        home.clone(),
        DaemonOptions::from_config(&cfg),
        Box::new(catalog),
        Box::new(runner),
    )?;
    println!("Daemon running (pid {me}); stop with: ragmonk daemon stop");
    while !stop.load(Ordering::Relaxed) {
        std::thread::sleep(Duration::from_millis(200));
    }
    daemon.stop();
    if pid::read_pid_file(home).is_some_and(|i| i.pid == me) {
        pid::remove_pid_file(home);
    }
    Ok(())
}
