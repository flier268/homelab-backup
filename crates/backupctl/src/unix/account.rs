//! Local account lookup for `run_as`. Numeric `UID:GID` does not need passwd.

use std::ffi::{CStr, CString};

use rustix::process::{getegid, geteuid};

use super::error::SecurityError;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Account {
    pub name: String,
    pub uid: u32,
    pub gid: u32,
    pub home: String,
}

pub fn account(value: &str) -> Result<Account, SecurityError> {
    if let Some((uid, gid)) = numeric_identity(value) {
        return Ok(Account {
            name: value.to_owned(),
            uid,
            gid,
            home: "/".to_owned(),
        });
    }
    if !valid_user_name(value) {
        return Err(SecurityError::invalid(
            "user must be a local user name or quoted \"UID:GID\" string",
        ));
    }
    lookup_name(value)
}

pub fn fixed_environment(value: &str) -> Result<Vec<(String, String)>, SecurityError> {
    let record = account(value)?;
    Ok(vec![
        ("HOME".to_owned(), record.home),
        ("LANG".to_owned(), "C.UTF-8".to_owned()),
        ("LC_ALL".to_owned(), "C.UTF-8".to_owned()),
        (
            "PATH".to_owned(),
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin".to_owned(),
        ),
    ])
}

pub fn drop_privileges(value: &str) -> Result<Option<(u32, u32)>, SecurityError> {
    let record = account(value)?;
    if geteuid().as_raw() == record.uid && getegid().as_raw() == record.gid {
        return Ok(None);
    }
    if geteuid().as_raw() != 0 {
        return Err(SecurityError::invalid(format!(
            "cannot run as {}:{} without root coordinator privileges",
            record.uid, record.gid
        )));
    }
    Ok(Some((record.uid, record.gid)))
}

fn numeric_identity(value: &str) -> Option<(u32, u32)> {
    let (uid, gid) = value.split_once(':')?;
    if uid.is_empty()
        || gid.is_empty()
        || !uid.bytes().all(|byte| byte.is_ascii_digit())
        || !gid.bytes().all(|byte| byte.is_ascii_digit())
    {
        return None;
    }
    let uid = uid.parse::<u64>().ok()?;
    let gid = gid.parse::<u64>().ok()?;
    let max = u32::MAX as u64 - 1;
    if uid > max || gid > max {
        return None;
    }
    Some((uid as u32, gid as u32))
}

fn valid_user_name(value: &str) -> bool {
    let mut chars = value.chars();
    match chars.next() {
        Some(first) if first == '_' || first.is_ascii_lowercase() => {}
        _ => return false,
    }
    let rest = chars.as_str();
    rest.len() <= 31
        && rest
            .chars()
            .all(|ch| ch.is_ascii_lowercase() || ch.is_ascii_digit() || ch == '_' || ch == '-')
}

fn lookup_name(name: &str) -> Result<Account, SecurityError> {
    let c_name = CString::new(name).map_err(|_| SecurityError::invalid("invalid user name"))?;
    let mut buffer = vec![0u8; 16 * 1024];
    let mut record = std::mem::MaybeUninit::<Passwd>::zeroed();
    let mut result = std::ptr::null_mut();
    let code = unsafe {
        getpwnam_r(
            c_name.as_ptr(),
            record.as_mut_ptr(),
            buffer.as_mut_ptr().cast(),
            buffer.len(),
            &mut result,
        )
    };
    if code != 0 || result.is_null() {
        return Err(SecurityError::invalid(format!(
            "local user does not exist: {name}"
        )));
    }
    let record = unsafe { record.assume_init() };
    let home = unsafe { CStr::from_ptr(record.pw_dir) }
        .to_string_lossy()
        .into_owned();
    Ok(Account {
        name: name.to_owned(),
        uid: record.pw_uid,
        gid: record.pw_gid,
        home,
    })
}

#[repr(C)]
struct Passwd {
    pw_name: *mut i8,
    pw_passwd: *mut i8,
    pw_uid: u32,
    pw_gid: u32,
    pw_gecos: *mut i8,
    pw_dir: *mut i8,
    pw_shell: *mut i8,
}

unsafe extern "C" {
    fn getpwnam_r(
        name: *const i8,
        pwd: *mut Passwd,
        buf: *mut i8,
        buflen: usize,
        result: *mut *mut Passwd,
    ) -> i32;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn numeric_identity_does_not_need_passwd() {
        let record = account("12345:23456").unwrap();
        assert_eq!(
            (record.uid, record.gid, record.home.as_str()),
            (12345, 23456, "/")
        );
        let env = fixed_environment("12345:23456").unwrap();
        assert_eq!(env.iter().find(|(key, _)| key == "HOME").unwrap().1, "/");
    }
}
