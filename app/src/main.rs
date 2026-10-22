//! The jura workbench in a native window. Adapted from evid-app (MIT, evidlabel).
//!
//!     workbench-app --url URL [--raise-file PATH]   # show a running workbench server (what `jura open` does)
//!     workbench-app                                 # start `jura open --headless` itself and stop it
//!                                                   # when the window closes (WORKBENCH_BIN overrides `jura`)
//!
//! Touching the raise file brings the window to the front.
//!
//! On Linux the window reloads the page when WebKit's web process dies (a renderer crash used to
//! leave it blank), and renders without WebKit's DMA-BUF path by default, which crashed that
//! process on Intel/Wayland. `WORKBENCH_WEBKIT_DMABUF=1` asks for the accelerated path again.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::net::{TcpListener, TcpStream};
use std::path::PathBuf;
use std::process::{exit, Child, Command};
use std::sync::Mutex;
use std::thread::sleep;
use std::time::{Duration, Instant, SystemTime};

use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};

struct Server(Mutex<Option<Child>>);

/// Desktop-file id. GNOME's app switcher looks up `<id>.desktop` for the icon.
const APP_ID: &str = "io.github.evidlabel.workbench";
const ICON_PNG: &[u8] = include_bytes!("../icons/icon.png");

/// Write the icon and a desktop entry so the switcher can find them.
/// GTK's own application id is left unset: registering one makes GTK refuse a
/// second workbench-app. The program name carries the id instead (see main).
#[cfg(target_os = "linux")]
fn publish_desktop_entry() -> Option<PathBuf> {
    let base = std::env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".local/share")))?;
    let icon = base.join(format!("icons/hicolor/512x512/apps/{APP_ID}.png"));
    let desktop = base.join(format!("applications/{APP_ID}.desktop"));
    std::fs::create_dir_all(icon.parent()?).ok()?;
    std::fs::create_dir_all(desktop.parent()?).ok()?;
    std::fs::write(&icon, ICON_PNG).ok()?;
    let exe = std::env::current_exe().unwrap_or_else(|_| PathBuf::from("workbench-app"));
    let exec = exe.display().to_string().replace('"', "\\\"");
    let text = format!(
        "[Desktop Entry]\n\
         Type=Application\n\
         Name=jura workbench\n\
         Comment=Write a case with agents, datasets and anonymization\n\
         Exec=\"{exec}\"\n\
         Icon={icon}\n\
         StartupWMClass={APP_ID}\n\
         Terminal=false\n\
         Categories=Office;\n",
        icon = icon.display(),
    );
    std::fs::write(&desktop, text).ok()?;
    Some(icon)
}

#[cfg(target_os = "linux")]
fn set_window_icon(window: &tauri::WebviewWindow, icon: Option<&std::path::Path>) {
    use gtk::prelude::*;

    if let (Ok(gtk_win), Some(path)) = (window.gtk_window(), icon) {
        let _ = gtk_win.set_icon_from_file(path);
    }
}

fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .and_then(|l| l.local_addr())
        .map(|a| a.port())
        .unwrap_or(8780)
}

/// WebKitGTK kills the web process when a page crashes it (a segfault, or the renderer's memory
/// limit) and then leaves the window blank. Everything the page shows lives on the server, so a
/// reload brings the same case and tab back: a renderer crash costs a flash, not the window.
/// A page that crashes every time is left alone after a few tries, so it cannot spin.
#[cfg(target_os = "linux")]
fn reload_after_renderer_crash(window: &tauri::WebviewWindow) {
    use webkit2gtk::WebViewExt;

    let seen: Mutex<Vec<Instant>> = Mutex::new(Vec::new());
    let _ = window.with_webview(move |wv| {
        let view = wv.inner();
        view.connect_web_process_terminated(move |view, reason| {
            let now = Instant::now();
            let mut log = seen.lock().unwrap();
            log.retain(|t| now.duration_since(*t) < Duration::from_secs(120));
            log.push(now);
            if log.len() > 3 {
                eprintln!("workbench-app: web process keeps dying ({reason:?}); leaving the window as it is");
                return;
            }
            eprintln!("workbench-app: web process terminated ({reason:?}); reloading the page");
            drop(log);
            view.reload();
        });
    });
}

fn start_server(bin: &str, args: &[String], port: u16, raise: &PathBuf) -> Child {
    let mut child = Command::new(bin)
        .args(args)
        .args(["open", "--headless", "-p", &port.to_string()])
        .env("WORKBENCH_IN_APP", "1")
        .env("WORKBENCH_RAISE_FILE", raise)
        .spawn()
        .unwrap_or_else(|e| {
            eprintln!("workbench-app: cannot start {bin}: {e}");
            exit(1)
        });
    let deadline = Instant::now() + Duration::from_secs(30);
    while TcpStream::connect(("127.0.0.1", port)).is_err() {
        if let Ok(Some(status)) = child.try_wait() {
            exit(status.code().unwrap_or(1));
        }
        if Instant::now() > deadline {
            let _ = child.kill();
            eprintln!("workbench-app: server did not come up on port {port}");
            exit(1);
        }
        sleep(Duration::from_millis(100));
    }
    child
}

fn mtime(p: &PathBuf) -> Option<SystemTime> {
    std::fs::metadata(p).and_then(|m| m.modified()).ok()
}

fn main() {
    // WebKitGTK 2.52's DMA-BUF renderer has crashed this web process on Intel/Wayland: a libc
    // segfault first, later "free(): corrupted unsorted chunks", and the window goes blank.
    // Render through the plain path unless WORKBENCH_WEBKIT_DMABUF asks for the accelerated one.
    // Set before GTK or WebKit is initialized, or it is read too late.
    #[cfg(target_os = "linux")]
    if std::env::var_os("WORKBENCH_WEBKIT_DMABUF").is_none() {
        std::env::set_var("WEBKIT_DISABLE_DMABUF_RENDERER", "1");
    }
    let args: Vec<String> = std::env::args().skip(1).collect();
    let flag = |name: &str| args.iter().position(|a| a == name).and_then(|i| args.get(i + 1)).cloned();
    let (url, raise, child) = match flag("--url") {
        Some(url) => (url, flag("--raise-file").map(PathBuf::from), None),
        None => {
            let bin = std::env::var("WORKBENCH_BIN").unwrap_or_else(|_| "jura".into());
            let port = free_port();
            let raise = std::env::temp_dir().join(format!("workbench-app-{}.raise", std::process::id()));
            let child = start_server(&bin, &args, port, &raise);
            (format!("http://127.0.0.1:{port}/"), Some(raise), Some(child))
        }
    };
    let url = url.parse().unwrap_or_else(|e| {
        eprintln!("workbench-app: bad url {url}: {e}");
        exit(2)
    });

    #[cfg(target_os = "linux")]
    let icon = publish_desktop_entry();
    // On Wayland the window's app id is the program name, set when the window
    // maps; the shell matches it to the desktop file for the switcher icon.
    // gtk::init only fills it from argv[0] when it is still unset.
    #[cfg(target_os = "linux")]
    gtk::glib::set_prgname(Some(APP_ID));

    tauri::Builder::default()
        .manage(Server(Mutex::new(child)))
        .setup(move |app| {
            let window = WebviewWindowBuilder::new(app, "main", WebviewUrl::External(url))
                .title("jura.sh workbench")
                .inner_size(1400.0, 900.0)
                .decorations(true)
                .maximized(true)
                .visible(false)
                .build()?;
            #[cfg(target_os = "linux")]
            set_window_icon(&window, icon.as_deref());
            #[cfg(target_os = "linux")]
            reload_after_renderer_crash(&window);
            // Maximized fills the work area and leaves the title bar. Clear any
            // remembered exclusive fullscreen so the close button stays.
            let _ = window.set_fullscreen(false);
            let _ = window.set_decorations(true);
            let _ = window.maximize();
            let _ = window.show();
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                let mut seen = raise.as_ref().and_then(mtime);
                loop {
                    sleep(Duration::from_millis(300));
                    // someone touched the raise file: come to the front
                    if let Some(p) = raise.as_ref() {
                        let now = mtime(p);
                        if now.is_some() && now != seen {
                            seen = now;
                            if let Some(w) = handle.get_webview_window("main") {
                                let _ = w.unminimize();
                                let _ = w.show();
                                let _ = w.set_focus();
                            }
                        }
                    }
                    // when the server we started exits (Ctrl+W / Ctrl+Q in the page), close the window too
                    let state = handle.state::<Server>();
                    let mut guard = state.0.lock().unwrap();
                    if let Some(c) = guard.as_mut() {
                        if !matches!(c.try_wait(), Ok(None)) {
                            guard.take();
                            drop(guard);
                            handle.exit(0);
                            break;
                        }
                    }
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building workbench-app")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                if let Some(mut c) = app.state::<Server>().0.lock().unwrap().take() {
                    let _ = c.kill();
                    let _ = c.wait();
                }
            }
        });
}
