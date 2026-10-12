//! Download, verify, and install a newer GitHub release.
//! HTTP and the installer are injected so tests never touch the network.

use std::collections::{BTreeMap, HashSet};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitStatus};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use flate2::read::GzDecoder;
use homelab_backup_domain::yaml::python_str;
use sha2::{Digest, Sha256};

const RELEASE_BASE_URL: &str =
    "https://github.com/flier268/homelab-backup/releases/latest/download";
const INSTALL_ROOT: &str = "/usr/local/lib/homelab-backup";
const CHECKSUM_MAX_BYTES: usize = 64 * 1024;
const EXTRACT_MAX_BYTES: u64 = 512 * 1024 * 1024;
const EXTRACT_MAX_MEMBERS: usize = 10_000;
const DOWNLOAD_CHUNK_BYTES: usize = 1024 * 1024;
const DECOMPRESS_CEILING: u64 = EXTRACT_MAX_BYTES + 64 * 1024 * 1024;
const TAR_BLOCK: u64 = 512;

#[derive(Debug, thiserror::Error)]
#[error("{0}")]
pub struct UpgradeError(String);

impl UpgradeError {
    fn new(message: impl Into<String>) -> Self {
        Self(message.into())
    }
}

trait HttpClient {
    fn open(&self, url: &str) -> Result<HttpBody, OpenError>;
}

struct HttpBody {
    content_length: Option<String>,
    reader: Box<dyn Read>,
}

enum OpenError {
    Status(u16),
    Transport(String),
}

struct UreqClient;

impl HttpClient for UreqClient {
    fn open(&self, url: &str) -> Result<HttpBody, OpenError> {
        let agent = ureq::AgentBuilder::new()
            .timeout_connect(Duration::from_secs(30))
            .timeout_read(Duration::from_secs(30))
            .timeout_write(Duration::from_secs(30))
            .build();
        let agent_name = user_agent();
        match agent
            .get(url)
            .set("Accept", "application/octet-stream")
            .set("User-Agent", &agent_name)
            .call()
        {
            Ok(response) => {
                let content_length = response.header("Content-Length").map(str::to_owned);
                let reader: Box<dyn Read> = Box::new(response.into_reader());
                Ok(HttpBody {
                    content_length,
                    reader,
                })
            }
            Err(ureq::Error::Status(code, _)) => Err(OpenError::Status(code)),
            Err(ureq::Error::Transport(error)) => Err(OpenError::Transport(error.to_string())),
        }
    }
}

fn release_install(source_root: &Path) -> Result<i32, String> {
    let status = Command::new(source_root.join("bin/backupctl"))
        .arg("install")
        .current_dir(source_root)
        .status()
        .map_err(|error| error.to_string())?;
    Ok(exit_status_code(status))
}

fn exit_status_code(status: ExitStatus) -> i32 {
    if let Some(code) = status.code() {
        return code;
    }
    std::os::unix::process::ExitStatusExt::signal(&status)
        .map(|signal| -signal)
        .unwrap_or(1)
}

fn user_agent() -> String {
    format!("homelab-backup/{}", env!("CARGO_PKG_VERSION"))
}

pub fn run_upgrade() -> Result<(), UpgradeError> {
    upgrade_with(
        Path::new(INSTALL_ROOT),
        env!("CARGO_PKG_VERSION"),
        &UreqClient,
        &release_install,
    )
}

fn upgrade_with(
    install_root: &Path,
    version: &str,
    http: &dyn HttpClient,
    installer: &dyn Fn(&Path) -> Result<i32, String>,
) -> Result<(), UpgradeError> {
    installed_release(install_root)?;
    println!("Current version: {version}");
    let (latest_version, archive_name, expected_digest) = download_checksum(http)?;
    let current = version_tuple(version)?;
    let latest = version_tuple(&latest_version)?;
    println!("Latest stable version: {latest_version}");
    if latest == current {
        println!("Already up to date.");
        return Ok(());
    }
    if latest < current {
        return Err(UpgradeError::new(format!(
            "latest stable release {latest_version} is older than installed version {version}; refusing to downgrade"
        )));
    }
    let temporary = TemporaryDir::create()?;
    let archive_path = temporary.path.join(&archive_name);
    let extract_root = temporary.path.join("source");
    fs::create_dir(&extract_root).map_err(|error| {
        UpgradeError::new(format!("upgrade workspace could not be created: {error}"))
    })?;
    fs::set_permissions(&extract_root, fs::Permissions::from_mode(0o700)).map_err(|error| {
        UpgradeError::new(format!("upgrade workspace could not be created: {error}"))
    })?;
    println!("Downloading homelab-backup {latest_version}...");
    download_archive(http, &archive_name, &expected_digest, &archive_path)?;
    println!("SHA-256 verified. Validating release archive...");
    let source_root = extract_archive(&archive_path, &extract_root, &latest_version)?;
    println!("Installing homelab-backup {latest_version}...");
    run_installer(&source_root, installer)?;
    println!("Upgrade to homelab-backup {latest_version} completed.");
    Ok(())
}

fn open_download(http: &dyn HttpClient, url: &str, stage: &str) -> Result<HttpBody, UpgradeError> {
    match http.open(url) {
        Ok(body) => Ok(body),
        Err(OpenError::Status(code)) => Err(UpgradeError::new(format!(
            "{stage} failed: GitHub returned HTTP {code}"
        ))),
        Err(OpenError::Transport(reason)) => {
            Err(UpgradeError::new(format!("{stage} failed: {reason}")))
        }
    }
}

fn download_checksum(http: &dyn HttpClient) -> Result<(String, String, String), UpgradeError> {
    let url = format!("{RELEASE_BASE_URL}/SHA256SUMS");
    let mut body = open_download(http, &url, "checksum download")?;
    let length = parsed_content_length(body.content_length.as_deref(), "checksum download")?;
    if length.is_some_and(|value| value > i128::from(CHECKSUM_MAX_BYTES as u32)) {
        return Err(UpgradeError::new(
            "checksum download exceeds the 64 KiB limit",
        ));
    }
    let raw = read_limited(&mut body.reader, CHECKSUM_MAX_BYTES + 1)
        .map_err(|error| UpgradeError::new(format!("checksum download failed: {error}")))?;
    if raw.len() > CHECKSUM_MAX_BYTES {
        return Err(UpgradeError::new(
            "checksum download exceeds the 64 KiB limit",
        ));
    }
    if let Some(length) = length
        && i128::from(raw.len() as u32) != length
    {
        return Err(UpgradeError::new("checksum download was incomplete"));
    }
    if raw.iter().any(|byte| *byte >= 128) {
        return Err(UpgradeError::new("checksum file is not ASCII text"));
    }
    let text =
        String::from_utf8(raw).map_err(|_| UpgradeError::new("checksum file is not ASCII text"))?;
    let lines = python_splitlines(&text);
    if lines.len() != 1 {
        return Err(UpgradeError::new(
            "checksum file must contain exactly one record",
        ));
    }
    let Some((digest, archive_name)) = match_checksum_line(lines[0]) else {
        return Err(UpgradeError::new("checksum file has an invalid record"));
    };
    let Some(version) = archive_version(&archive_name) else {
        return Err(UpgradeError::new(
            "checksum file names an invalid release archive",
        ));
    };
    Ok((version, archive_name, digest))
}

fn download_archive(
    http: &dyn HttpClient,
    archive_name: &str,
    expected_digest: &str,
    destination: &Path,
) -> Result<(), UpgradeError> {
    let url = format!("{RELEASE_BASE_URL}/{archive_name}");
    let mut body = open_download(http, &url, "release archive download")?;
    let length = parsed_content_length(body.content_length.as_deref(), "release archive download")?;
    let mut output = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(destination)
        .map_err(|error| UpgradeError::new(format!("release archive download failed: {error}")))?;
    let mut hasher = Sha256::new();
    let mut downloaded: u128 = 0;
    let mut buffer = vec![0u8; DOWNLOAD_CHUNK_BYTES];
    loop {
        let read = match body.reader.read(&mut buffer) {
            Ok(0) => break,
            Ok(count) => count,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => {
                return Err(UpgradeError::new(format!(
                    "release archive download failed: {error}"
                )));
            }
        };
        output.write_all(&buffer[..read]).map_err(|error| {
            UpgradeError::new(format!("release archive download failed: {error}"))
        })?;
        hasher.update(&buffer[..read]);
        downloaded += u128::from(read as u32);
    }
    if let Some(length) = length
        && i128::try_from(downloaded).ok() != Some(length)
    {
        return Err(UpgradeError::new("release archive download was incomplete"));
    }
    let actual = format!("{:x}", hasher.finalize());
    if actual != expected_digest {
        return Err(UpgradeError::new(format!(
            "release archive SHA-256 mismatch: expected {expected_digest}, got {actual}"
        )));
    }
    Ok(())
}

fn parsed_content_length(header: Option<&str>, stage: &str) -> Result<Option<i128>, UpgradeError> {
    let Some(header) = header else {
        return Ok(None);
    };
    match python_int(header) {
        Some(value) if value >= 0 => Ok(Some(value)),
        _ => Err(UpgradeError::new(format!(
            "{stage} returned an invalid Content-Length header"
        ))),
    }
}

fn read_limited(reader: &mut dyn Read, max: usize) -> io::Result<Vec<u8>> {
    let mut buffer = vec![0u8; max];
    let mut filled = 0;
    while filled < max {
        match reader.read(&mut buffer[filled..]) {
            Ok(0) => break,
            Ok(count) => filled += count,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error),
        }
    }
    buffer.truncate(filled);
    Ok(buffer)
}

struct Member {
    name: String,
    mode: u32,
    size: i64,
    typeflag: u8,
    data_offset: u64,
}

struct RawHeader {
    name: String,
    prefix: String,
    mode: u32,
    size: i64,
    typeflag: u8,
}

struct Header {
    name: String,
    mode: u32,
    size: i64,
    typeflag: u8,
}

fn extract_archive(
    archive_path: &Path,
    destination: &Path,
    version: &str,
) -> Result<PathBuf, UpgradeError> {
    let scratch = std::env::temp_dir().join(unique_name("homelab-backup-upgrade-tar-"));
    let _cleanup = DeleteFile(scratch.clone());
    decompress_archive(archive_path, &scratch)?;
    let mut tar = File::open(&scratch).map_err(tar_io)?;
    let members = parse_tar(&mut tar)?;
    validate_members(&members, version)?;
    materialize(&mut tar, destination, &members, version)?;
    Ok(destination.join(format!("homelab-backup-{version}")))
}

fn decompress_archive(archive_path: &Path, tar_path: &Path) -> Result<(), UpgradeError> {
    let input = File::open(archive_path).map_err(tar_io)?;
    let mut decoder = GzDecoder::new(input);
    let mut output = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(tar_path)
        .map_err(tar_io)?;
    let mut buffer = vec![0u8; DOWNLOAD_CHUNK_BYTES];
    let mut total = 0u64;
    loop {
        let read = match decoder.read(&mut buffer) {
            Ok(0) => break,
            Ok(count) => count,
            Err(error) => return Err(tar_io(error)),
        };
        total = total.saturating_add(read as u64);
        if total > DECOMPRESS_CEILING {
            return Err(UpgradeError::new(
                "release archive expands beyond the 512 MiB limit",
            ));
        }
        output.write_all(&buffer[..read]).map_err(tar_io)?;
    }
    Ok(())
}

fn parse_tar(file: &mut File) -> Result<Vec<Member>, UpgradeError> {
    let mut members = Vec::new();
    let mut globals = BTreeMap::new();
    let mut pending: Option<BTreeMap<String, String>> = None;
    let mut long_name: Option<String> = None;
    let mut long_link: Option<String> = None;
    let mut need_follower = false;
    let mut dircheck = true;
    loop {
        if members.len() > EXTRACT_MAX_MEMBERS {
            return Err(UpgradeError::new(
                "release archive has an invalid number of members",
            ));
        }
        let Some(raw) = read_raw_header(file)? else {
            if need_follower {
                return Err(tar_err("end of file header"));
            }
            break;
        };
        let header = interpret(raw, dircheck);
        match header.typeflag {
            // git archive writes a global pax header. It is metadata, not a member.
            b'g' => {
                globals.extend(read_pax_fields(file, header.size)?);
                need_follower = true;
                dircheck = false;
            }
            b'x' | b'X' => {
                let mut fields = globals.clone();
                fields.extend(read_pax_fields(file, header.size)?);
                pending = Some(fields);
                need_follower = true;
                dircheck = false;
            }
            b'L' => {
                long_name = Some(read_gnu_string(file, header.size)?);
                need_follower = true;
                dircheck = false;
            }
            b'K' => {
                long_link = Some(read_gnu_string(file, header.size)?);
                need_follower = true;
                dircheck = false;
            }
            b'S' => return Err(tar_err("unsupported sparse member")),
            typeflag => {
                let mut name = header.name;
                let mut size = header.size;
                let applied = pending.take().unwrap_or_else(|| globals.clone());
                if let Some(path) = applied.get("path") {
                    name = path.trim_end_matches('/').to_owned();
                }
                if let Some(value) = applied.get("size") {
                    size = parse_pax_size(value);
                }
                if let Some(long) = long_name.take() {
                    name = long;
                }
                let _ = long_link.take();
                if typeflag == b'5' {
                    name = name.trim_end_matches('/').to_owned();
                }
                let data_offset = skip_member_body(file, typeflag, size)?;
                members.push(Member {
                    name,
                    mode: header.mode,
                    size,
                    typeflag,
                    data_offset,
                });
                need_follower = false;
                dircheck = true;
            }
        }
    }
    Ok(members)
}

fn read_raw_header(file: &mut File) -> Result<Option<RawHeader>, UpgradeError> {
    let position = file.stream_position().map_err(tar_io)?;
    let mut block = [0u8; 512];
    let mut filled = 0;
    while filled < block.len() {
        match file.read(&mut block[filled..]) {
            Ok(0) if filled == 0 && position == 0 => return Err(tar_err("empty file")),
            Ok(0) if filled == 0 => return Ok(None),
            Ok(0) => return Err(tar_err("truncated header")),
            Ok(count) => filled += count,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(tar_io(error)),
        }
    }
    if block.iter().all(|byte| *byte == 0) {
        return Ok(None);
    }
    let stored = parse_number(&block[148..156]).map_err(|_| tar_err("invalid header"))?;
    let (unsigned, signed) = header_checksums(&block);
    if stored != i64::from(unsigned) && stored != i64::from(signed) {
        return Err(tar_err("bad checksum"));
    }
    let name = header_text(&block[0..100])?;
    let mode_number = parse_number(&block[100..108]).map_err(|_| tar_err("invalid header"))?;
    let mode = u32::try_from(mode_number).map_err(|_| tar_err("invalid header"))?;
    let size = parse_number(&block[124..136]).map_err(|_| tar_err("invalid header"))?;
    let typeflag = block[156];
    header_text(&block[157..257])?;
    let prefix = header_text(&block[345..500])?;
    Ok(Some(RawHeader {
        name,
        prefix,
        mode,
        size,
        typeflag,
    }))
}

fn interpret(raw: RawHeader, dircheck: bool) -> Header {
    let mut name = raw.name;
    let mut typeflag = raw.typeflag;
    if dircheck && typeflag == 0 && name.ends_with('/') {
        typeflag = b'5';
    }
    if typeflag == b'5' {
        name = name.trim_end_matches('/').to_owned();
    }
    if !raw.prefix.is_empty() && !matches!(typeflag, b'L' | b'K' | b'S') {
        name = format!("{}/{name}", raw.prefix);
    }
    Header {
        name,
        mode: raw.mode,
        size: raw.size,
        typeflag,
    }
}

fn header_checksums(block: &[u8; 512]) -> (u32, i32) {
    let mut unsigned = 0u32;
    let mut signed = 0i32;
    for (index, &byte) in block.iter().enumerate() {
        let value = if (148..156).contains(&index) {
            b' '
        } else {
            byte
        };
        unsigned += u32::from(value);
        signed += i32::from(value as i8);
    }
    (unsigned, signed)
}

fn header_text(field: &[u8]) -> Result<String, UpgradeError> {
    let end = field
        .iter()
        .position(|byte| *byte == 0)
        .unwrap_or(field.len());
    String::from_utf8(field[..end].to_vec()).map_err(|_| tar_err("invalid header"))
}

fn parse_number(field: &[u8]) -> Result<i64, ()> {
    if field.is_empty() {
        return Err(());
    }
    if field[0] == 0x80 || field[0] == 0xff {
        let mut value: i128 = 0;
        for &byte in &field[1..] {
            value = (value << 8) | i128::from(byte);
        }
        if field[0] == 0xff {
            let width = field.len() - 1;
            value -= 1i128 << (8 * width);
        }
        return i64::try_from(value).map_err(|_| ());
    }
    let text = std::str::from_utf8(field).map_err(|_| ())?;
    let end = text.find('\0').unwrap_or(text.len());
    let trimmed = text[..end].trim();
    if trimmed.is_empty() {
        return Ok(0);
    }
    i64::from_str_radix(trimmed, 8).map_err(|_| ())
}

fn read_pax_fields(file: &mut File, size: i64) -> Result<BTreeMap<String, String>, UpgradeError> {
    if !(0..=8 * 1024 * 1024).contains(&size) {
        return Err(tar_err("invalid header"));
    }
    let bytes = read_counted(file, size as u64)?;
    parse_pax(&bytes)
}

fn parse_pax(bytes: &[u8]) -> Result<BTreeMap<String, String>, UpgradeError> {
    let mut fields = BTreeMap::new();
    let mut position = 0;
    while position < bytes.len() && bytes[position] != 0 {
        let start = position;
        while position < bytes.len() && bytes[position].is_ascii_digit() {
            position += 1;
        }
        if position == start || position >= bytes.len() || bytes[position] != b' ' {
            return Err(tar_err("invalid header"));
        }
        let length: usize = std::str::from_utf8(&bytes[start..position])
            .map_err(|_| tar_err("invalid header"))?
            .parse()
            .map_err(|_| tar_err("invalid header"))?;
        if length < 5 || start + length > bytes.len() {
            return Err(tar_err("invalid header"));
        }
        let record_end = start + length;
        if bytes[record_end - 1] != b'\n' {
            return Err(tar_err("invalid header"));
        }
        let keyword_value = &bytes[position + 1..record_end - 1];
        let Some(split) = keyword_value.iter().position(|byte| *byte == b'=') else {
            return Err(tar_err("invalid header"));
        };
        if split == 0 {
            return Err(tar_err("invalid header"));
        }
        let keyword = std::str::from_utf8(&keyword_value[..split])
            .map_err(|_| tar_err("invalid header"))?
            .to_owned();
        let value = std::str::from_utf8(&keyword_value[split + 1..])
            .map_err(|_| tar_err("invalid header"))?
            .to_owned();
        fields.insert(keyword, value);
        position = record_end;
    }
    Ok(fields)
}

fn parse_pax_size(value: &str) -> i64 {
    if let Ok(size) = value.parse::<i64>() {
        return size;
    }
    if !value.is_empty() && value.bytes().all(|byte| byte.is_ascii_digit()) {
        i64::MAX
    } else {
        0
    }
}

fn read_gnu_string(file: &mut File, size: i64) -> Result<String, UpgradeError> {
    if !(0..=8 * 1024 * 1024).contains(&size) {
        return Err(tar_err("invalid header"));
    }
    let bytes = read_counted(file, size as u64)?;
    let end = bytes
        .iter()
        .position(|byte| *byte == 0)
        .unwrap_or(bytes.len());
    String::from_utf8(bytes[..end].to_vec()).map_err(|_| tar_err("invalid header"))
}

fn read_counted(file: &mut File, size: u64) -> Result<Vec<u8>, UpgradeError> {
    let mut bytes = vec![0u8; size as usize];
    file.read_exact(&mut bytes)
        .map_err(|_| tar_err("unexpected end of data"))?;
    let padding = block_len(size) - size;
    if padding > 0 {
        let position = file.stream_position().map_err(tar_io)?;
        file.seek(SeekFrom::Start(position + padding))
            .map_err(tar_io)?;
    }
    Ok(bytes)
}

fn skip_member_body(file: &mut File, typeflag: u8, size: i64) -> Result<u64, UpgradeError> {
    let offset = file.stream_position().map_err(tar_io)?;
    if !has_payload(typeflag) {
        return Ok(offset);
    }
    if size < 0 {
        return Err(tar_err("invalid header"));
    }
    let next = offset
        .checked_add(block_len(size as u64))
        .ok_or_else(|| tar_err("invalid header"))?;
    file.seek(SeekFrom::Start(next)).map_err(tar_io)?;
    Ok(offset)
}

fn has_payload(typeflag: u8) -> bool {
    let regular = matches!(typeflag, b'0' | 0 | b'7');
    let supported = matches!(
        typeflag,
        b'0' | 0 | b'1' | b'2' | b'3' | b'4' | b'5' | b'6' | b'7'
    );
    regular || !supported
}

fn block_len(size: u64) -> u64 {
    let remainder = size % TAR_BLOCK;
    if remainder == 0 {
        size
    } else {
        size + (TAR_BLOCK - remainder)
    }
}

fn validate_members(members: &[Member], version: &str) -> Result<(), UpgradeError> {
    if members.is_empty() || members.len() > EXTRACT_MAX_MEMBERS {
        return Err(UpgradeError::new(
            "release archive has an invalid number of members",
        ));
    }
    let expected_root = format!("homelab-backup-{version}");
    let mut seen = HashSet::new();
    let mut total = 0u128;
    let mut saw_install = false;
    let mut saw_guide = false;
    for member in members {
        let is_dir = member.typeflag == b'5';
        let relative = member_path(&member.name, is_dir, &expected_root)?;
        if !seen.insert(relative.clone()) {
            return Err(UpgradeError::new(format!(
                "release archive contains a duplicate path: {}",
                python_str(&relative)
            )));
        }
        let is_reg = matches!(member.typeflag, b'0' | 0 | b'7');
        if !is_dir && !is_reg {
            return Err(UpgradeError::new(format!(
                "release archive contains an unsupported member: {}",
                python_str(&member.name)
            )));
        }
        if is_reg {
            if member.size < 0 {
                return Err(UpgradeError::new(format!(
                    "release archive has an invalid member size: {}",
                    python_str(&member.name)
                )));
            }
            total += member.size as u128;
            if total > u128::from(EXTRACT_MAX_BYTES) {
                return Err(UpgradeError::new(
                    "release archive expands beyond the 512 MiB limit",
                ));
            }
        }
        if relative == format!("{expected_root}/bin/backupctl") {
            if !is_reg || member.size == 0 || member.mode & 0o111 == 0 || member.mode & 0o022 != 0 {
                return Err(UpgradeError::new(
                    "release archive contains an invalid bin/backupctl",
                ));
            }
            saw_install = true;
        }
        if relative == format!("{expected_root}/GUIDE.html") {
            if !is_reg || member.size == 0 || member.mode & 0o022 != 0 {
                return Err(UpgradeError::new(
                    "release archive contains an invalid GUIDE.html",
                ));
            }
            saw_guide = true;
        }
    }
    if !saw_install {
        return Err(UpgradeError::new(
            "release archive is missing bin/backupctl",
        ));
    }
    if !saw_guide {
        return Err(UpgradeError::new("release archive is missing GUIDE.html"));
    }
    Ok(())
}

fn member_path(name: &str, is_dir: bool, expected_root: &str) -> Result<String, UpgradeError> {
    let mut parts: Vec<&str> = name.split('/').collect();
    let unsafe_path = name.starts_with('/')
        || parts
            .iter()
            .any(|part| part.is_empty() || *part == "." || *part == "..");
    if unsafe_path {
        let prefix_ok = parts.len() > 1
            && parts[..parts.len() - 1]
                .iter()
                .all(|part| !part.is_empty() && *part != "." && *part != "..");
        if !(is_dir && parts.last() == Some(&"") && prefix_ok) {
            return Err(UpgradeError::new(format!(
                "release archive has an unsafe path: {}",
                python_str(name)
            )));
        }
        parts.pop();
    }
    if parts.is_empty() || parts[0] != expected_root {
        return Err(UpgradeError::new(format!(
            "release archive member is outside {}: {}",
            python_str(expected_root),
            python_str(name)
        )));
    }
    Ok(parts.join("/"))
}

fn materialize(
    file: &mut File,
    destination: &Path,
    members: &[Member],
    version: &str,
) -> Result<(), UpgradeError> {
    let expected_root = format!("homelab-backup-{version}");
    for member in members {
        let relative = member_path(&member.name, member.typeflag == b'5', &expected_root)?;
        let target = join_relative(destination, &relative);
        if member.typeflag == b'5' {
            fs::create_dir_all(&target).map_err(tar_io)?;
            fs::set_permissions(&target, fs::Permissions::from_mode(member.mode & 0o777))
                .map_err(tar_io)?;
            continue;
        }
        if let Some(parent) = target.parent() {
            fs::create_dir_all(parent).map_err(tar_io)?;
        }
        let mut output = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&target)
            .map_err(tar_io)?;
        if member.size > 0 {
            file.seek(SeekFrom::Start(member.data_offset))
                .map_err(tar_io)?;
            copy_exact(file, &mut output, member.size as u64).map_err(|_| {
                UpgradeError::new(format!(
                    "cannot read release archive member: {}",
                    python_str(&member.name)
                ))
            })?;
        }
        fs::set_permissions(&target, fs::Permissions::from_mode(member.mode & 0o777))
            .map_err(tar_io)?;
    }
    Ok(())
}

fn join_relative(root: &Path, relative: &str) -> PathBuf {
    let mut path = root.to_path_buf();
    for part in relative.split('/') {
        path.push(part);
    }
    path
}

fn copy_exact(input: &mut File, output: &mut File, size: u64) -> io::Result<()> {
    let mut remaining = size;
    let mut buffer = vec![0u8; DOWNLOAD_CHUNK_BYTES];
    while remaining > 0 {
        let chunk = usize::try_from(remaining.min(buffer.len() as u64)).unwrap_or(buffer.len());
        input.read_exact(&mut buffer[..chunk])?;
        output.write_all(&buffer[..chunk])?;
        remaining -= chunk as u64;
    }
    Ok(())
}

fn tar_err(message: &str) -> UpgradeError {
    UpgradeError::new(format!("release archive validation failed: {message}"))
}

fn tar_io(error: io::Error) -> UpgradeError {
    UpgradeError::new(format!("release archive validation failed: {error}"))
}

fn version_tuple(version: &str) -> Result<(u64, u64, u64), UpgradeError> {
    let parts: Vec<&str> = version.split('.').collect();
    if parts.len() != 3
        || parts
            .iter()
            .any(|part| part.is_empty() || !part.bytes().all(|byte| byte.is_ascii_digit()))
    {
        return Err(UpgradeError::new(format!(
            "invalid semantic version: {}",
            python_str(version)
        )));
    }
    let parse = |part: &str| {
        part.parse::<u64>().map_err(|_| {
            UpgradeError::new(format!("invalid semantic version: {}", python_str(version)))
        })
    };
    Ok((parse(parts[0])?, parse(parts[1])?, parse(parts[2])?))
}

fn archive_version(name: &str) -> Option<String> {
    let version = name
        .strip_prefix("homelab-backup-")?
        .strip_suffix(".tar.gz")?;
    version_tuple(version).ok()?;
    Some(version.to_owned())
}

fn match_checksum_line(line: &str) -> Option<(String, String)> {
    let (digest, name) = line.split_once("  ")?;
    if digest.len() != 64 || !digest.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return None;
    }
    archive_version(name)?;
    Some((digest.to_ascii_lowercase(), name.to_owned()))
}

fn installed_release(install_root: &Path) -> Result<PathBuf, UpgradeError> {
    let Some(raw) = std::env::var_os("HOMELAB_BACKUP_RELEASE_ROOT") else {
        return Err(not_installed());
    };
    if raw.is_empty() {
        return Err(not_installed());
    }
    let Some(raw) = raw.to_str() else {
        return Err(UpgradeError::new("installed release layout is invalid"));
    };
    let release = canonicalize_strict(Path::new(raw))?;
    let current = canonicalize_strict(&install_root.join("current"))?;
    let releases_root = canonicalize_strict(&install_root.join("releases"))?;
    let name_ok = release
        .file_name()
        .and_then(|name| name.to_str())
        .is_some_and(release_name_ok);
    if release != current || release.parent() != Some(releases_root.as_path()) || !name_ok {
        return Err(UpgradeError::new("installed release layout is invalid"));
    }
    Ok(release)
}

fn not_installed() -> UpgradeError {
    UpgradeError::new("upgrade is only available from the installed /usr/local/sbin/backupctl")
}

fn release_name_ok(name: &str) -> bool {
    let Some(rest) = name.strip_prefix("release.") else {
        return false;
    };
    !rest.is_empty()
        && rest
            .chars()
            .all(|character| character.is_ascii_alphanumeric())
}

fn canonicalize_strict(path: &Path) -> Result<PathBuf, UpgradeError> {
    path.canonicalize().map_err(|error| {
        UpgradeError::new(format!(
            "installed release layout is invalid: {}",
            python_os_error(&error, path)
        ))
    })
}

fn python_os_error(error: &io::Error, path: &Path) -> String {
    let code = error.raw_os_error().unwrap_or(0);
    let reason = match code {
        1 => "Operation not permitted",
        2 => "No such file or directory",
        13 => "Permission denied",
        17 => "File exists",
        20 => "Not a directory",
        21 => "Is a directory",
        40 => "Too many levels of symbolic links",
        _ => "OS error",
    };
    format!(
        "[Errno {code}] {reason}: {}",
        python_str(&path.display().to_string())
    )
}

fn run_installer(
    source_root: &Path,
    installer: &dyn Fn(&Path) -> Result<i32, String>,
) -> Result<(), UpgradeError> {
    match installer(source_root) {
        Ok(0) => Ok(()),
        Ok(code) => Err(UpgradeError::new(format!(
            "installer failed with exit status {code}"
        ))),
        Err(error) => Err(UpgradeError::new(format!(
            "installer could not be started: {error}"
        ))),
    }
}

fn python_splitlines(text: &str) -> Vec<&str> {
    let bytes = text.as_bytes();
    let mut lines = Vec::new();
    let mut start = 0;
    let mut index = 0;
    while index < bytes.len() {
        match bytes[index] {
            b'\n' | 0x0b | 0x0c | 0x1c | 0x1d | 0x1e => {
                lines.push(&text[start..index]);
                index += 1;
                start = index;
            }
            b'\r' => {
                lines.push(&text[start..index]);
                index += 1;
                if index < bytes.len() && bytes[index] == b'\n' {
                    index += 1;
                }
                start = index;
            }
            _ => index += 1,
        }
    }
    if start < bytes.len() {
        lines.push(&text[start..]);
    }
    lines
}

fn python_int(value: &str) -> Option<i128> {
    let text = value.trim();
    let (negative, digits) = if let Some(rest) = text.strip_prefix('+') {
        (false, rest)
    } else if let Some(rest) = text.strip_prefix('-') {
        (true, rest)
    } else {
        (false, text)
    };
    if digits.is_empty() || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    if digits.len() > 38 {
        return Some(if negative { i128::MIN } else { i128::MAX });
    }
    let mut number: i128 = 0;
    for byte in digits.bytes() {
        number = number
            .checked_mul(10)?
            .checked_add(i128::from(byte - b'0'))?;
    }
    Some(if negative { -number } else { number })
}

fn unique_name(prefix: &str) -> String {
    static SEQUENCE: AtomicU64 = AtomicU64::new(0);
    let sequence = SEQUENCE.fetch_add(1, Ordering::Relaxed);
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or(0);
    format!("{prefix}{}-{nanos}-{sequence}", std::process::id())
}

struct TemporaryDir {
    path: PathBuf,
}

impl TemporaryDir {
    fn create() -> Result<Self, UpgradeError> {
        let path = std::env::temp_dir().join(unique_name("homelab-backup-upgrade-"));
        fs::create_dir(&path).map_err(|error| {
            UpgradeError::new(format!("upgrade workspace could not be created: {error}"))
        })?;
        let directory = Self { path };
        fs::set_permissions(&directory.path, fs::Permissions::from_mode(0o700)).map_err(
            |error| UpgradeError::new(format!("upgrade workspace could not be created: {error}")),
        )?;
        Ok(directory)
    }
}

impl Drop for TemporaryDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.path);
    }
}

struct DeleteFile(PathBuf);

impl Drop for DeleteFile {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}

#[cfg(test)]
mod tests {
    use std::collections::VecDeque;
    use std::fs::File;
    use std::io::Cursor;
    use std::os::unix::fs::{PermissionsExt, symlink};
    use std::path::{Path, PathBuf};
    use std::sync::{Mutex, MutexGuard};

    use super::*;

    const CURRENT: &str = env!("CARGO_PKG_VERSION");

    struct EnvGuard {
        key: &'static str,
        previous: Option<std::ffi::OsString>,
    }

    impl EnvGuard {
        fn set(key: &'static str, value: &str) -> Self {
            let previous = std::env::var_os(key);
            unsafe { std::env::set_var(key, value) };
            Self { key, previous }
        }

        fn remove(key: &'static str) -> Self {
            let previous = std::env::var_os(key);
            unsafe { std::env::remove_var(key) };
            Self { key, previous }
        }
    }

    impl Drop for EnvGuard {
        fn drop(&mut self) {
            unsafe {
                match self.previous.take() {
                    Some(value) => std::env::set_var(self.key, value),
                    None => std::env::remove_var(self.key),
                }
            }
        }
    }

    fn env_lock() -> MutexGuard<'static, ()> {
        static LOCK: Mutex<()> = Mutex::new(());
        LOCK.lock().unwrap_or_else(|poison| poison.into_inner())
    }

    struct TempPath(PathBuf);

    impl TempPath {
        fn new() -> Self {
            let path = std::env::temp_dir().join(unique_name("homelab-backup-upgrade-test-"));
            fs::create_dir(&path).unwrap();
            Self(path)
        }
    }

    impl Drop for TempPath {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    struct Layout {
        install_root: PathBuf,
        _env: EnvGuard,
    }

    impl Layout {
        fn create(root: &Path) -> Self {
            let install_root = root.join("lib/homelab-backup");
            let release = install_root.join("releases/release.current");
            fs::create_dir_all(&release).unwrap();
            symlink("releases/release.current", install_root.join("current")).unwrap();
            let env = EnvGuard::set(
                "HOMELAB_BACKUP_RELEASE_ROOT",
                release.to_str().expect("utf-8 temp path"),
            );
            Self {
                install_root,
                _env: env,
            }
        }
    }

    enum ScriptItem {
        Transport(String),
        Body {
            body: Vec<u8>,
            content_length: Option<String>,
        },
    }

    struct ScriptedHttp {
        calls: Mutex<Vec<String>>,
        items: Mutex<VecDeque<ScriptItem>>,
    }

    impl ScriptedHttp {
        fn new(items: Vec<ScriptItem>) -> Self {
            Self {
                calls: Mutex::new(Vec::new()),
                items: Mutex::new(VecDeque::from(items)),
            }
        }

        fn calls(&self) -> Vec<String> {
            self.calls.lock().unwrap().clone()
        }
    }

    impl HttpClient for ScriptedHttp {
        fn open(&self, url: &str) -> Result<HttpBody, OpenError> {
            self.calls.lock().unwrap().push(url.to_owned());
            let Some(item) = self.items.lock().unwrap().pop_front() else {
                return Err(OpenError::Transport("scripted response exhausted".into()));
            };
            match item {
                ScriptItem::Transport(reason) => Err(OpenError::Transport(reason)),
                ScriptItem::Body {
                    body,
                    content_length,
                } => Ok(HttpBody {
                    content_length,
                    reader: Box::new(Cursor::new(body)),
                }),
            }
        }
    }

    struct Entry {
        name: String,
        typeflag: u8,
        mode: u32,
        linkname: String,
        data: Vec<u8>,
    }

    fn entry(name: &str, typeflag: u8, mode: u32, linkname: &str, data: &[u8]) -> Entry {
        Entry {
            name: name.to_owned(),
            typeflag,
            mode,
            linkname: linkname.to_owned(),
            data: data.to_vec(),
        }
    }

    fn next_version() -> String {
        let (major, minor, patch) = version_tuple(CURRENT).unwrap();
        format!("{major}.{minor}.{}", patch + 1)
    }

    fn sha256_hex(bytes: &[u8]) -> String {
        let mut hasher = Sha256::new();
        hasher.update(bytes);
        format!("{:x}", hasher.finalize())
    }

    fn checksum_bytes(archive: &[u8], version: &str) -> Vec<u8> {
        let digest = sha256_hex(archive).to_ascii_uppercase();
        format!("{digest}  homelab-backup-{version}.tar.gz\n").into_bytes()
    }

    fn release_archive(
        version: &str,
        extra: &[Entry],
        include_installer: bool,
        installer_mode: u32,
        include_guide: bool,
        guide_mode: u32,
    ) -> Vec<u8> {
        let root = format!("homelab-backup-{version}");
        let mut tar = Vec::new();
        push_entry(
            &mut tar,
            "pax_global_header",
            b'g',
            0o644,
            "",
            b"52 comment=889453ad15ee5c49b8b64c0804e5210b902369c8\n",
        );
        push_entry(&mut tar, &format!("{root}/"), b'5', 0o755, "", b"");
        if include_installer {
            push_entry(
                &mut tar,
                &format!("{root}/bin/backupctl"),
                b'0',
                installer_mode,
                "",
                b"#!/usr/bin/env bash\nexit 0\n",
            );
        }
        if include_guide {
            push_entry(
                &mut tar,
                &format!("{root}/GUIDE.html"),
                b'0',
                guide_mode,
                "",
                b"<!doctype html><title>Guide</title>\n",
            );
        }
        for item in extra {
            push_entry(
                &mut tar,
                &item.name,
                item.typeflag,
                item.mode,
                &item.linkname,
                &item.data,
            );
        }
        tar.extend_from_slice(&[0u8; 1024]);
        gzip_bytes(&tar)
    }

    fn push_entry(
        tar: &mut Vec<u8>,
        name: &str,
        typeflag: u8,
        mode: u32,
        linkname: &str,
        data: &[u8],
    ) {
        assert!(name.len() <= 100, "{name}");
        assert!(linkname.len() <= 100, "{linkname}");
        let mut block = [0u8; 512];
        block[..name.len()].copy_from_slice(name.as_bytes());
        write_octal(&mut block[100..108], u64::from(mode));
        write_octal(&mut block[108..116], 0);
        write_octal(&mut block[116..124], 0);
        write_octal(&mut block[124..136], data.len() as u64);
        write_octal(&mut block[136..148], 0);
        block[148..156].fill(b' ');
        block[156] = typeflag;
        block[157..157 + linkname.len()].copy_from_slice(linkname.as_bytes());
        block[257..262].copy_from_slice(b"ustar");
        block[263..265].copy_from_slice(b"00");
        let checksum: u32 = block.iter().map(|byte| u32::from(*byte)).sum();
        let rendered = format!("{checksum:06o}");
        block[148..154].copy_from_slice(rendered.as_bytes());
        block[154] = 0;
        block[155] = b' ';
        tar.extend_from_slice(&block);
        tar.extend_from_slice(data);
        let padding = (512 - (data.len() % 512)) % 512;
        tar.extend(std::iter::repeat_n(0u8, padding));
    }

    fn write_octal(field: &mut [u8], value: u64) {
        let digits = field.len() - 1;
        let text = format!("{value:0digits$o}");
        assert!(text.len() <= digits, "{text}");
        field[..text.len()].copy_from_slice(text.as_bytes());
        field[digits] = 0;
    }

    fn gzip_bytes(data: &[u8]) -> Vec<u8> {
        let mut encoder = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::default());
        encoder.write_all(data).unwrap();
        encoder.finish().unwrap()
    }

    fn body(bytes: Vec<u8>, content_length: Option<usize>) -> ScriptItem {
        ScriptItem::Body {
            content_length: content_length.map(|length| length.to_string()),
            body: bytes,
        }
    }

    #[test]
    fn upgrade_user_agent_uses_crate_version() {
        assert_eq!(user_agent(), "homelab-backup/1.0.11");
    }

    #[test]
    fn upgrade_repository_layout_is_rejected_before_network_access() {
        let _lock = env_lock();
        let _env = EnvGuard::remove("HOMELAB_BACKUP_RELEASE_ROOT");
        let http = ScriptedHttp::new(Vec::new());
        let error =
            upgrade_with(Path::new(INSTALL_ROOT), CURRENT, &http, &|_root| Ok(0)).unwrap_err();
        assert!(
            error
                .to_string()
                .contains("only available from the installed"),
            "{error}"
        );
        assert!(http.calls().is_empty());
    }

    #[test]
    fn upgrade_same_version_is_a_noop_without_archive_download() {
        let _lock = env_lock();
        let temp = TempPath::new();
        let layout = Layout::create(&temp.0);
        let checksum = format!("{}  homelab-backup-{CURRENT}.tar.gz\n", "0".repeat(64));
        let http = ScriptedHttp::new(vec![body(
            checksum.clone().into_bytes(),
            Some(checksum.len()),
        )]);
        let installs = AtomicU64::new(0);
        upgrade_with(&layout.install_root, CURRENT, &http, &|_root| {
            installs.fetch_add(1, Ordering::SeqCst);
            Ok(0)
        })
        .unwrap();
        assert_eq!(http.calls().len(), 1);
        assert!(http.calls()[0].ends_with("/SHA256SUMS"));
        assert_eq!(installs.load(Ordering::SeqCst), 0);
    }

    #[test]
    fn upgrade_new_version_is_verified_extracted_and_installed_then_cleaned() {
        let _lock = env_lock();
        let temp = TempPath::new();
        let layout = Layout::create(&temp.0);
        let version = next_version();
        let archive = release_archive(&version, &[], true, 0o755, true, 0o644);
        let checksum = checksum_bytes(&archive, &version);
        let http = ScriptedHttp::new(vec![
            body(checksum.clone(), Some(checksum.len())),
            body(archive.clone(), Some(archive.len())),
        ]);
        let seen = Mutex::new(None);
        let previous = fs::read_link(layout.install_root.join("current")).unwrap();
        upgrade_with(
            &layout.install_root,
            CURRENT,
            &http,
            &|source_root: &Path| {
                assert!(source_root.join("bin/backupctl").is_file());
                assert_eq!(
                    fs::read(source_root.join("GUIDE.html")).unwrap(),
                    b"<!doctype html><title>Guide</title>\n"
                );
                *seen.lock().unwrap() = Some(source_root.to_path_buf());
                Ok(0)
            },
        )
        .unwrap();
        assert_eq!(
            fs::read_link(layout.install_root.join("current")).unwrap(),
            previous
        );
        assert_eq!(
            http.calls(),
            vec![
                format!("{RELEASE_BASE_URL}/SHA256SUMS"),
                format!("{RELEASE_BASE_URL}/homelab-backup-{version}.tar.gz"),
            ]
        );
        let source = seen.lock().unwrap().clone().expect("installer ran");
        assert!(!source.exists());
    }

    #[test]
    fn upgrade_installer_failure_still_cleans_temporary_tree() {
        let _lock = env_lock();
        let temp = TempPath::new();
        let layout = Layout::create(&temp.0);
        let version = next_version();
        let archive = release_archive(&version, &[], true, 0o755, true, 0o644);
        let checksum = checksum_bytes(&archive, &version);
        let http = ScriptedHttp::new(vec![body(checksum.clone(), None), body(archive, None)]);
        let seen = Mutex::new(None);
        let error = upgrade_with(
            &layout.install_root,
            CURRENT,
            &http,
            &|source_root: &Path| {
                *seen.lock().unwrap() = Some(source_root.to_path_buf());
                Ok(2)
            },
        )
        .unwrap_err();
        assert!(error.to_string().contains("exit status 2"), "{error}");
        let source = seen.lock().unwrap().clone().expect("installer ran");
        assert!(!source.exists());
    }

    #[test]
    fn upgrade_older_latest_release_is_rejected_without_archive_download() {
        let _lock = env_lock();
        let temp = TempPath::new();
        let layout = Layout::create(&temp.0);
        let checksum = format!("{}  homelab-backup-{CURRENT}.tar.gz\n", "0".repeat(64));
        let http = ScriptedHttp::new(vec![body(checksum.into_bytes(), None)]);
        let error =
            upgrade_with(&layout.install_root, &next_version(), &http, &|_root| Ok(0)).unwrap_err();
        assert!(
            error.to_string().contains("refusing to downgrade"),
            "{error}"
        );
        assert_eq!(http.calls().len(), 1);
    }

    #[test]
    fn upgrade_checksum_download_rejects_network_and_content_errors() {
        let version = next_version();
        let valid = format!("{}  homelab-backup-{version}.tar.gz\n", "0".repeat(64)).into_bytes();
        let cases: Vec<(&str, ScriptItem, &str)> = vec![
            (
                "network",
                ScriptItem::Transport("offline".into()),
                "checksum download failed",
            ),
            (
                "too-large",
                body(vec![b'x'; CHECKSUM_MAX_BYTES + 1], None),
                "64 KiB",
            ),
            (
                "truncated",
                body(valid.clone(), Some(valid.len() + 1)),
                "incomplete",
            ),
            ("multiple", body(valid.repeat(2), None), "exactly one"),
            (
                "invalid",
                body(b"not-a-checksum\n".to_vec(), None),
                "invalid record",
            ),
        ];
        for (label, item, message) in cases {
            let http = ScriptedHttp::new(vec![item]);
            let error = download_checksum(&http).unwrap_err();
            assert!(error.to_string().contains(message), "{label}: {error}");
        }
    }

    #[test]
    fn upgrade_archive_download_rejects_truncation_and_digest_mismatch() {
        let content = b"archive-data".to_vec();
        let digest = sha256_hex(&content);
        let cases = [
            (
                "truncated",
                body(content.clone(), Some(content.len() + 1)),
                digest.clone(),
                "incomplete",
            ),
            (
                "digest",
                body(content.clone(), None),
                "0".repeat(64),
                "SHA-256 mismatch",
            ),
        ];
        for (label, item, expected, message) in cases {
            let temp = TempPath::new();
            let destination = temp.0.join("release.tar.gz");
            let http = ScriptedHttp::new(vec![item]);
            let error = download_archive(
                &http,
                &format!("homelab-backup-{}.tar.gz", next_version()),
                &expected,
                &destination,
            )
            .unwrap_err();
            assert!(error.to_string().contains(message), "{label}: {error}");
        }
    }

    #[test]
    fn upgrade_archive_validation_rejects_unsafe_members() {
        let version = next_version();
        let root = format!("homelab-backup-{version}");
        let current_parts = version_tuple(CURRENT).unwrap();
        let wrong = format!(
            "homelab-backup-{}.{}.{}/file",
            current_parts.0,
            current_parts.1,
            current_parts.2 + 2
        );
        let cases: Vec<(&str, Vec<Entry>, bool, bool, &str)> = vec![
            (
                "traversal",
                vec![entry(&format!("{root}/../escape"), b'0', 0o644, "", b"x")],
                true,
                true,
                "unsafe path",
            ),
            (
                "outside",
                vec![entry("another-root/file", b'0', 0o644, "", b"x")],
                true,
                true,
                "outside",
            ),
            (
                "symlink",
                vec![entry(
                    &format!("{root}/link"),
                    b'2',
                    0o777,
                    "/etc/passwd",
                    b"",
                )],
                true,
                true,
                "unsupported member",
            ),
            (
                "hardlink",
                vec![entry(
                    &format!("{root}/hardlink"),
                    b'1',
                    0o644,
                    &format!("{root}/bin/backupctl"),
                    b"",
                )],
                true,
                true,
                "unsupported member",
            ),
            (
                "device",
                vec![entry(&format!("{root}/device"), b'3', 0o644, "", b"")],
                true,
                true,
                "unsupported member",
            ),
            (
                "fifo",
                vec![entry(&format!("{root}/fifo"), b'6', 0o644, "", b"")],
                true,
                true,
                "unsupported member",
            ),
            (
                "wrong-version",
                vec![entry(&wrong, b'0', 0o644, "", b"x")],
                true,
                true,
                "outside",
            ),
            (
                "missing-installer",
                Vec::new(),
                false,
                true,
                "missing bin/backupctl",
            ),
            (
                "missing-guide",
                Vec::new(),
                true,
                false,
                "missing GUIDE.html",
            ),
        ];
        for (label, extra, include_installer, include_guide, message) in cases {
            let temp = TempPath::new();
            let archive_path = temp.0.join("release.tar.gz");
            fs::write(
                &archive_path,
                release_archive(
                    &version,
                    &extra,
                    include_installer,
                    0o755,
                    include_guide,
                    0o644,
                ),
            )
            .unwrap();
            let destination = temp.0.join("source");
            fs::create_dir(&destination).unwrap();
            let error = extract_archive(&archive_path, &destination, &version).unwrap_err();
            assert!(error.to_string().contains(message), "{label}: {error}");
        }
    }

    #[test]
    fn upgrade_archive_validation_rejects_unsafe_installer_permissions() {
        for mode in [0o644u32, 0o777] {
            let temp = TempPath::new();
            let version = next_version();
            let archive_path = temp.0.join("release.tar.gz");
            fs::write(
                &archive_path,
                release_archive(&version, &[], true, mode, true, 0o644),
            )
            .unwrap();
            let destination = temp.0.join("source");
            fs::create_dir(&destination).unwrap();
            let error = extract_archive(&archive_path, &destination, &version).unwrap_err();
            assert!(
                error.to_string().contains("invalid bin/backupctl"),
                "{mode:o}: {error}"
            );
        }
    }

    #[test]
    fn upgrade_archive_validation_rejects_unsafe_guide_permissions() {
        let temp = TempPath::new();
        let version = next_version();
        let archive_path = temp.0.join("release.tar.gz");
        fs::write(
            &archive_path,
            release_archive(&version, &[], true, 0o755, true, 0o666),
        )
        .unwrap();
        let destination = temp.0.join("source");
        fs::create_dir(&destination).unwrap();
        let error = extract_archive(&archive_path, &destination, &version).unwrap_err();
        assert!(error.to_string().contains("invalid GUIDE.html"), "{error}");
    }

    #[test]
    fn upgrade_installer_runs_bash_in_release_root_and_propagates_failure() {
        let temp = TempPath::new();
        fs::create_dir(temp.0.join("bin")).unwrap();
        fs::write(
            temp.0.join("bin/backupctl"),
            "#!/bin/sh\npwd > marker\nexit 3\n",
        )
        .unwrap();
        std::fs::set_permissions(
            temp.0.join("bin/backupctl"),
            fs::Permissions::from_mode(0o755),
        )
        .unwrap();
        let error = run_installer(&temp.0, &release_install).unwrap_err();
        assert!(error.to_string().contains("exit status 3"), "{error}");
        let marker = fs::read_to_string(temp.0.join("marker")).unwrap();
        assert_eq!(
            PathBuf::from(marker.trim()).canonicalize().unwrap(),
            temp.0.canonicalize().unwrap()
        );
    }

    #[test]
    fn upgrade_parser_reads_git_archive_members() {
        let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        let temp = TempPath::new();
        let tar_path = temp.0.join("archive.tar");
        let output = File::create(&tar_path).unwrap();
        let status = Command::new("git")
            .arg("-C")
            .arg(&repo)
            .args([
                "-c",
                "tar.umask=0022",
                "archive",
                "--format=tar",
                "--prefix=homelab-backup-1.0.12/",
                "HEAD",
                "--",
                "Dockerfile.volume-rsync",
            ])
            .stdout(output)
            .status()
            .unwrap();
        assert!(status.success(), "git archive failed");
        let mut file = File::open(&tar_path).unwrap();
        let members = parse_tar(&mut file).unwrap();
        let names: Vec<_> = members.iter().map(|member| member.name.as_str()).collect();
        assert_eq!(
            names,
            [
                "homelab-backup-1.0.12",
                "homelab-backup-1.0.12/Dockerfile.volume-rsync"
            ]
        );
        assert_eq!(members[1].mode & 0o777, 0o644);
    }
}
