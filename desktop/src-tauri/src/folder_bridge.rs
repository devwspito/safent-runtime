//! Session-only folder capabilities. The web page never supplies a host path.
use base64::{engine::general_purpose, Engine};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::io::Read;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tauri::{Manager, WebviewWindow};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons};

const FAILED: &str = "No se pudo acceder a la carpeta. Vuelve a seleccionarla.";
const LIMIT: &str = "Límite de carpeta: 2.000 archivos, 25 MB por archivo y 256 MB en total. Elige una subcarpeta; no se ha importado parcialmente.";
const UNSAFE: &str = "La carpeta contiene enlaces simbólicos, archivos especiales o rutas no admitidas. Elige una carpeta sin enlaces.";
const CONFLICT: &str =
    "Un archivo cambió en tu carpeta. No se ha sobrescrito; vuelve a seleccionar la carpeta.";
const MAX_FILES: usize = 2000;
const MAX_FILE: u64 = 25 * 1024 * 1024;
const MAX_TOTAL: u64 = 256 * 1024 * 1024;

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Entry {
    path: String,
    size: u64,
}
#[derive(Serialize)]
pub struct Selection {
    id: String,
    name: String,
    files: Vec<Entry>,
    total_bytes: u64,
}
struct Batch {
    id: String,
    expires: Instant,
    files: HashMap<String, u64>,
}
struct Grant {
    origin: String,
    name: String,
    folder: scoped::Folder,
    batch: Option<Batch>,
}
#[derive(Clone, Default)]
pub struct FolderBridge(Arc<Mutex<HashMap<String, Grant>>>);

fn opaque_id() -> Result<String, String> {
    let mut bytes = [0_u8; 24];
    std::fs::File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut bytes))
        .map_err(|_| FAILED.to_string())?;
    Ok(general_purpose::URL_SAFE_NO_PAD.encode(bytes))
}
fn caller(window: &WebviewWindow) -> Result<String, String> {
    let url = window.url().map_err(|_| FAILED.to_string())?;
    if !window
        .state::<crate::window_policy::WindowPolicy>()
        .allows_host_folder(window.label(), &url)
    {
        return Err(FAILED.into());
    }
    Ok(url.origin().ascii_serialization())
}
fn valid_path(path: &str) -> bool {
    !path.is_empty()
        && path.len() <= 1024
        && !path.contains('\\')
        && path.split('/').count() <= 20
        && path.split('/').all(|p| {
            !p.is_empty() && p != "." && p != ".." && !p.bytes().any(|b| b < 32 || b == 127)
        })
}
fn validate_batch(files: &[Entry]) -> Result<HashMap<String, u64>, String> {
    let mut out = HashMap::new();
    let mut total = 0_u64;
    if files.len() > MAX_FILES {
        return Err(LIMIT.into());
    }
    for entry in files {
        if !valid_path(&entry.path) {
            return Err(UNSAFE.into());
        }
        total = total.checked_add(entry.size).ok_or(LIMIT)?;
        if entry.size > MAX_FILE || total > MAX_TOTAL {
            return Err(LIMIT.into());
        }
        if out.insert(entry.path.clone(), entry.size).is_some() {
            return Err(UNSAFE.into());
        }
    }
    Ok(out)
}
fn consume_write(batch: &mut Batch, id: &str, path: &str, size: u64) -> Result<(), String> {
    if batch.id != id || Instant::now() >= batch.expires {
        return Err(FAILED.into());
    }
    if batch.files.remove(path) != Some(size) {
        return Err(FAILED.into());
    }
    Ok(())
}

#[tauri::command]
pub async fn pick_host_folder(window: WebviewWindow) -> Result<Option<Selection>, String> {
    let origin = caller(&window)?;
    let app = window.app_handle().clone();
    let state = window.state::<FolderBridge>().inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let Some(chosen) = app
            .dialog()
            .file()
            .set_title("Elegir carpeta para Safent · lectura; guardar requiere confirmación")
            .blocking_pick_folder()
        else {
            return Ok(None);
        };
        let path = chosen.into_path().map_err(|_| FAILED.to_string())?;
        let name = path
            .file_name()
            .and_then(|s| s.to_str())
            .ok_or(UNSAFE)?
            .to_string();
        let (folder, files) = scoped::Folder::open(&path)?;
        let total_bytes = files.iter().map(|e| e.size).sum();
        let id = opaque_id()?;
        let mut grants = state.0.lock().map_err(|_| FAILED.to_string())?;
        if grants.len() >= 8 {
            return Err(
                "Ya hay 8 carpetas abiertas. Reinicia Safent para liberar los permisos.".into(),
            );
        }
        grants.insert(
            id.clone(),
            Grant {
                origin,
                name: name.clone(),
                folder,
                batch: None,
            },
        );
        Ok(Some(Selection {
            id,
            name,
            files,
            total_bytes,
        }))
    })
    .await
    .map_err(|_| FAILED.to_string())?
}

#[tauri::command]
pub async fn read_host_folder_file(
    window: WebviewWindow,
    id: String,
    path: String,
) -> Result<String, String> {
    let origin = caller(&window)?;
    let state = window.state::<FolderBridge>().inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let grants = state.0.lock().map_err(|_| FAILED.to_string())?;
        let grant = grants
            .get(&id)
            .filter(|g| g.origin == origin)
            .ok_or(FAILED)?;
        Ok(general_purpose::STANDARD.encode(grant.folder.read(&path)?))
    })
    .await
    .map_err(|_| FAILED.to_string())?
}

#[tauri::command]
pub async fn approve_host_folder_write(
    window: WebviewWindow,
    id: String,
    files: Vec<Entry>,
) -> Result<Option<String>, String> {
    let origin = caller(&window)?;
    let files = validate_batch(&files)?;
    let state = window.state::<FolderBridge>().inner().clone();
    let app = window.app_handle().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let name = {
            let grants = state.0.lock().map_err(|_| FAILED.to_string())?;
            grants.get(&id).filter(|g| g.origin == origin).ok_or(FAILED)?.name.clone()
        };
        let mut paths: Vec<_> = files.keys().cloned().collect(); paths.sort();
        let preview = paths.iter().take(12).cloned().collect::<Vec<_>>().join("\n");
        let approved = app.dialog().message(format!(
            "Guardar {} archivos en «{}» ({} MB). Se reemplazarán los archivos del mismo nombre; no se eliminarán otros archivos.\n\n{}{}",
            files.len(), name, files.values().sum::<u64>() / (1024 * 1024), preview,
            if files.len() > 12 { "\n…" } else { "" }
        )).title("Guardar cambios en tu carpeta").buttons(MessageDialogButtons::OkCancel).blocking_show();
        if !approved { return Ok(None); }
        let batch_id = opaque_id()?;
        let mut grants = state.0.lock().map_err(|_| FAILED.to_string())?;
        let grant = grants.get_mut(&id).filter(|g| g.origin == origin).ok_or(FAILED)?;
        grant.batch = Some(Batch { id: batch_id.clone(), expires: Instant::now() + Duration::from_secs(600), files });
        Ok(Some(batch_id))
    }).await.map_err(|_| FAILED.to_string())?
}

#[tauri::command]
pub async fn write_host_folder_file(
    window: WebviewWindow,
    id: String,
    batch_id: String,
    path: String,
    data: String,
) -> Result<(), String> {
    let origin = caller(&window)?;
    if data.len() > (MAX_FILE as usize + 2) / 3 * 4 {
        return Err(LIMIT.into());
    }
    let state = window.state::<FolderBridge>().inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let bytes = general_purpose::STANDARD
            .decode(data)
            .map_err(|_| FAILED.to_string())?;
        let mut grants = state.0.lock().map_err(|_| FAILED.to_string())?;
        let grant = grants
            .get_mut(&id)
            .filter(|g| g.origin == origin)
            .ok_or(FAILED)?;
        let batch = grant.batch.as_mut().ok_or(FAILED)?;
        consume_write(batch, &batch_id, &path, bytes.len() as u64)?;
        grant.folder.write(&path, &bytes)
    })
    .await
    .map_err(|_| FAILED.to_string())?
}

#[cfg(unix)]
mod scoped {
    use super::*;
    use std::ffi::{CStr, CString};
    use std::fs::{File, OpenOptions};
    use std::io::Write;
    use std::os::fd::{AsRawFd, FromRawFd};
    use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
    use std::path::Path;

    #[derive(Clone, PartialEq, Eq)]
    struct Stamp {
        inode: u64,
        device: u64,
        size: u64,
        mtime: i64,
        nanos: i64,
    }
    fn stamp(file: &File) -> Result<Stamp, String> {
        let m = file.metadata().map_err(|_| FAILED.to_string())?;
        if !m.is_file() || m.nlink() != 1 {
            return Err(UNSAFE.into());
        }
        Ok(Stamp {
            inode: m.ino(),
            device: m.dev(),
            size: m.len(),
            mtime: m.mtime(),
            nanos: m.mtime_nsec(),
        })
    }
    fn open_at(parent: &File, name: &str, directory: bool) -> Result<File, String> {
        let name = CString::new(name).map_err(|_| UNSAFE.to_string())?;
        let flags = libc::O_RDONLY
            | libc::O_CLOEXEC
            | libc::O_NOFOLLOW
            | libc::O_NONBLOCK
            | if directory { libc::O_DIRECTORY } else { 0 };
        let fd = unsafe { libc::openat(parent.as_raw_fd(), name.as_ptr(), flags) };
        if fd < 0 {
            return Err(UNSAFE.into());
        }
        Ok(unsafe { File::from_raw_fd(fd) })
    }
    pub struct Folder {
        root: File,
        originals: HashMap<String, Stamp>,
    }
    fn errno() -> *mut libc::c_int {
        #[cfg(target_os = "linux")]
        return unsafe { libc::__errno_location() };
        #[cfg(not(target_os = "linux"))]
        return unsafe { libc::__error() };
    }
    impl Folder {
        pub fn open(path: &Path) -> Result<(Self, Vec<Entry>), String> {
            let root = OpenOptions::new()
                .read(true)
                .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
                .open(path)
                .map_err(|_| UNSAFE.to_string())?;
            let mut originals = HashMap::new();
            Self::scan(&root, "", &mut originals, &mut 0, &mut 0)?;
            let mut files: Vec<_> = originals
                .iter()
                .map(|(path, s)| Entry {
                    path: path.clone(),
                    size: s.size,
                })
                .collect();
            files.sort_by(|a, b| a.path.cmp(&b.path));
            Ok((Self { root, originals }, files))
        }
        fn scan(
            dir: &File,
            prefix: &str,
            out: &mut HashMap<String, Stamp>,
            total: &mut u64,
            visited: &mut usize,
        ) -> Result<(), String> {
            let fd = unsafe { libc::fcntl(dir.as_raw_fd(), libc::F_DUPFD_CLOEXEC, 0) };
            if fd < 0 {
                return Err(FAILED.into());
            }
            let stream = unsafe { libc::fdopendir(fd) };
            if stream.is_null() {
                unsafe {
                    libc::close(fd);
                }
                return Err(FAILED.into());
            }
            let result = (|| {
                loop {
                    unsafe {
                        *errno() = 0;
                    }
                    let entry = unsafe { libc::readdir(stream) };
                    if entry.is_null() {
                        if unsafe { *errno() } != 0 {
                            return Err(FAILED.into());
                        }
                        break;
                    }
                    let name = unsafe { CStr::from_ptr((*entry).d_name.as_ptr()) }
                        .to_str()
                        .map_err(|_| UNSAFE.to_string())?;
                    if name == "." || name == ".." {
                        continue;
                    }
                    *visited += 1;
                    if *visited > 10000 {
                        return Err(LIMIT.into());
                    }
                    let rel = if prefix.is_empty() {
                        name.to_string()
                    } else {
                        format!("{prefix}/{name}")
                    };
                    if !valid_path(&rel) {
                        return Err(UNSAFE.into());
                    }
                    let file = open_at(dir, name, false)?;
                    if file.metadata().map_err(|_| FAILED.to_string())?.is_dir() {
                        Self::scan(&file, &rel, out, total, visited)?;
                    } else {
                        let s = stamp(&file)?;
                        *total = total.checked_add(s.size).ok_or(LIMIT)?;
                        if out.len() >= MAX_FILES || s.size > MAX_FILE || *total > MAX_TOTAL {
                            return Err(LIMIT.into());
                        }
                        out.insert(rel, s);
                    }
                }
                Ok(())
            })();
            unsafe {
                libc::closedir(stream);
            }
            result
        }
        fn parent(&self, path: &str, create: bool) -> Result<(File, String), String> {
            if !valid_path(path) {
                return Err(UNSAFE.into());
            }
            let mut parts: Vec<_> = path.split('/').collect();
            let name = parts.pop().ok_or(UNSAFE)?.to_string();
            let mut parent = self.root.try_clone().map_err(|_| FAILED.to_string())?;
            for part in parts {
                if create {
                    let c = CString::new(part).map_err(|_| UNSAFE.to_string())?;
                    // mkdirat never follows a final symlink; openat below rejects it.
                    unsafe {
                        libc::mkdirat(parent.as_raw_fd(), c.as_ptr(), 0o700);
                    }
                }
                parent = open_at(&parent, part, true)?;
            }
            Ok((parent, name))
        }
        pub fn read(&self, path: &str) -> Result<Vec<u8>, String> {
            let expected = self.originals.get(path).ok_or(FAILED)?;
            let (parent, name) = self.parent(path, false)?;
            let mut file = open_at(&parent, &name, false)?;
            if stamp(&file)? != *expected {
                return Err(CONFLICT.into());
            }
            let mut bytes = Vec::new();
            (&mut file)
                .take(MAX_FILE + 1)
                .read_to_end(&mut bytes)
                .map_err(|_| FAILED.to_string())?;
            if bytes.len() as u64 != expected.size || stamp(&file)? != *expected {
                return Err(CONFLICT.into());
            }
            Ok(bytes)
        }
        pub fn write(&mut self, path: &str, bytes: &[u8]) -> Result<(), String> {
            let (parent, name) = self.parent(path, true)?;
            let name_c = CString::new(name.as_str()).map_err(|_| UNSAFE.to_string())?;
            // lstat (not following links) distinguishes absent from unsafe/unreadable.
            let mut metadata: libc::stat = unsafe { std::mem::zeroed() };
            let stat_result = unsafe {
                libc::fstatat(
                    parent.as_raw_fd(),
                    name_c.as_ptr(),
                    &mut metadata,
                    libc::AT_SYMLINK_NOFOLLOW,
                )
            };
            if stat_result != 0 && unsafe { *errno() } != libc::ENOENT {
                return Err(FAILED.into());
            }
            let exists = stat_result == 0;
            if exists {
                let current = open_at(&parent, &name, false)?;
                if self.originals.get(path) != Some(&stamp(&current)?) {
                    return Err(CONFLICT.into());
                }
            } else if self.originals.contains_key(path) {
                return Err(CONFLICT.into());
            }
            let temp_name = CString::new(format!(".safent-{}", opaque_id()?))
                .map_err(|_| FAILED.to_string())?;
            let fd = unsafe {
                libc::openat(
                    parent.as_raw_fd(),
                    temp_name.as_ptr(),
                    libc::O_WRONLY
                        | libc::O_CREAT
                        | libc::O_EXCL
                        | libc::O_NOFOLLOW
                        | libc::O_CLOEXEC,
                    0o600,
                )
            };
            if fd < 0 {
                return Err(FAILED.into());
            }
            let mut file = unsafe { File::from_raw_fd(fd) };
            let write = file.write_all(bytes).and_then(|_| file.sync_all());
            if exists
                && (|| {
                    let current = open_at(&parent, &name, false)?;
                    if self.originals.get(path) != Some(&stamp(&current)?) {
                        return Err(CONFLICT.to_string());
                    }
                    Ok(())
                })()
                .is_err()
            {
                unsafe {
                    libc::unlinkat(parent.as_raw_fd(), temp_name.as_ptr(), 0);
                }
                return Err(CONFLICT.into());
            }
            let moved = if write.is_err() {
                -1
            } else if exists {
                unsafe {
                    libc::renameat(
                        parent.as_raw_fd(),
                        temp_name.as_ptr(),
                        parent.as_raw_fd(),
                        name_c.as_ptr(),
                    )
                }
            } else {
                // No-replace create: a file appearing after our existence check wins.
                let result = unsafe {
                    libc::linkat(
                        parent.as_raw_fd(),
                        temp_name.as_ptr(),
                        parent.as_raw_fd(),
                        name_c.as_ptr(),
                        0,
                    )
                };
                if result == 0 {
                    unsafe {
                        libc::unlinkat(parent.as_raw_fd(), temp_name.as_ptr(), 0);
                    }
                }
                result
            };
            if moved != 0 {
                unsafe {
                    libc::unlinkat(parent.as_raw_fd(), temp_name.as_ptr(), 0);
                }
                return Err(FAILED.into());
            }
            let new_file = open_at(&parent, &name, false)?;
            self.originals.insert(path.to_string(), stamp(&new_file)?);
            Ok(())
        }
    }
}

#[cfg(not(unix))]
mod scoped {
    use super::*;
    pub struct Folder;
    impl Folder {
        pub fn open(_: &std::path::Path) -> Result<(Self, Vec<Entry>), String> {
            Err(FAILED.into())
        }
        pub fn read(&self, _: &str) -> Result<Vec<u8>, String> {
            Err(FAILED.into())
        }
        pub fn write(&mut self, _: &str, _: &[u8]) -> Result<(), String> {
            Err(FAILED.into())
        }
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::os::unix::fs::symlink;
    #[test]
    fn only_current_main_boot_origin_can_use_folder_capabilities() {
        let policy = crate::window_policy::WindowPolicy::new();
        let origin = tauri::Url::parse("http://127.0.0.1:1234/app/chat").unwrap();
        assert!(!policy.allows_host_folder("main", &origin));
        policy.set_authorized_origin(origin.clone());
        assert!(policy.allows_host_folder("main", &origin));
        assert!(!policy.allows_host_folder("other", &origin));
        for raw in [
            "http://127.0.0.1:9999/app",
            "https://evil.test",
            "tauri://localhost",
            "http://user@127.0.0.1:1234",
        ] {
            assert!(!policy.allows_host_folder("main", &tauri::Url::parse(raw).unwrap()));
        }
    }
    #[test]
    fn write_batch_is_expiring_single_use_and_binds_path_and_size() {
        let mut batch = Batch {
            id: "id".into(),
            expires: Instant::now() + Duration::from_secs(30),
            files: HashMap::from([("a".into(), 3)]),
        };
        assert!(consume_write(&mut batch, "wrong", "a", 3).is_err());
        assert!(consume_write(&mut batch, "id", "other", 3).is_err());
        assert!(consume_write(&mut batch, "id", "a", 3).is_ok());
        assert!(consume_write(&mut batch, "id", "a", 3).is_err());
        batch.files.insert("a".into(), 3);
        batch.expires = Instant::now();
        assert!(consume_write(&mut batch, "id", "a", 3).is_err());
        batch.expires = Instant::now() + Duration::from_secs(30);
        assert!(consume_write(&mut batch, "id", "a", 4).is_err());
    }
    #[test]
    fn rejects_traversal_absolute_and_ambiguous_paths() {
        for path in [
            "",
            "/etc/passwd",
            "../x",
            "a/../x",
            "a//b",
            "a\\b",
            "./x",
            "x\0",
        ] {
            assert!(!valid_path(path));
        }
        assert!(valid_path("images/consulta.png"));
    }
    #[test]
    fn bounded_manifest_and_selected_relative_reads() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("a.txt"), "hello").unwrap();
        let (folder, files) = scoped::Folder::open(dir.path()).unwrap();
        assert_eq!(files.len(), 1);
        assert_eq!(folder.read("a.txt").unwrap(), b"hello");
        assert!(folder.read("../a.txt").is_err());
        assert!(folder.read("unlisted").is_err());
    }
    #[test]
    fn symlinks_and_hardlinks_are_never_imported() {
        for hard in [false, true] {
            let dir = tempfile::tempdir().unwrap();
            let outside = tempfile::NamedTempFile::new().unwrap();
            if hard {
                std::fs::hard_link(outside.path(), dir.path().join("link")).unwrap();
            } else {
                symlink(outside.path(), dir.path().join("link")).unwrap();
            }
            assert!(scoped::Folder::open(dir.path()).is_err());
        }
    }
    #[test]
    fn replaced_directory_cannot_escape_during_read_or_write() {
        let dir = tempfile::tempdir().unwrap();
        let outside = tempfile::tempdir().unwrap();
        std::fs::create_dir(dir.path().join("sub")).unwrap();
        std::fs::write(dir.path().join("sub/a"), "old").unwrap();
        let (mut folder, _) = scoped::Folder::open(dir.path()).unwrap();
        std::fs::rename(dir.path().join("sub"), dir.path().join("old-sub")).unwrap();
        symlink(outside.path(), dir.path().join("sub")).unwrap();
        assert!(folder.read("sub/a").is_err());
        assert!(folder.write("sub/new", b"no").is_err());
        assert!(!outside.path().join("new").exists());
    }
    #[test]
    fn writeback_is_atomic_and_detects_host_changes() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("a"), "old").unwrap();
        let (mut folder, _) = scoped::Folder::open(dir.path()).unwrap();
        folder.write("a", b"approved").unwrap();
        assert_eq!(std::fs::read(dir.path().join("a")).unwrap(), b"approved");
        folder.write("new/result.txt", b"result").unwrap();
        std::fs::write(dir.path().join("a"), "external edit").unwrap();
        assert!(folder.write("a", b"overwrite").is_err());
    }
    #[test]
    fn limits_fail_before_import_and_write_permission_is_path_scoped() {
        assert!(validate_batch(&[Entry {
            path: "a".into(),
            size: MAX_FILE + 1
        }])
        .is_err());
        assert!(validate_batch(&[
            Entry {
                path: "a".into(),
                size: 1
            },
            Entry {
                path: "a".into(),
                size: 2
            }
        ])
        .is_err());
        let dir = tempfile::tempdir().unwrap();
        let file = std::fs::File::create(dir.path().join("large")).unwrap();
        file.set_len(MAX_FILE + 1).unwrap();
        assert!(scoped::Folder::open(dir.path()).is_err());
    }
}
