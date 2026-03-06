// stratum-boot-health — Post-boot security stack verification
// Checks: Secure Boot, lockdown mode, signing cert in trusted keyrings,
//         DKMS module signatures, systemd failed units.
// Runs as systemd oneshot at boot. Outputs JSON status + stashes failures.

use std::fs;
use std::path::Path;
use std::process::Command;
use serde::{Deserialize, Serialize};
use chrono::Utc;

const STATUS_DIR: &str = "$HOME/.local/share/stratum-boot-health";
const STASH_BIN: &str = "$HOME/.local/bin/stash";
const EXPECTED_CERT_CN: &str = "Stratum Module Signing CA";
const DKMS_MODULES: &[&str] = &["vmmon", "vmnet", "nvidia", "nvidia-modeset", "nvidia-uvm", "nvidia-drm"];
const KERNEL_MODULE_DIRS: &str = "/usr/lib/modules";

#[derive(Debug, Serialize, Deserialize)]
struct HealthStatus {
    timestamp: String,
    kernel: String,
    secure_boot: CheckResult,
    lockdown: CheckResult,
    signing_cert: CheckResult,
    dkms_modules: Vec<ModuleCheck>,
    systemd_failed: CheckResult,
    overall: String,
    failures: Vec<String>,
}

#[derive(Debug, Serialize, Deserialize)]
struct CheckResult {
    ok: bool,
    detail: String,
}

#[derive(Debug, Serialize, Deserialize)]
struct ModuleCheck {
    name: String,
    kernel: String,
    ok: bool,
    signer: Option<String>,
    sig_hashalgo: Option<String>,
    detail: String,
}

fn check_secure_boot() -> CheckResult {
    let path = "/sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c";
    if let Ok(bytes) = fs::read(path) {
        if bytes.len() >= 5 {
            let enabled = bytes[4] == 1;
            return CheckResult {
                ok: enabled,
                detail: if enabled { "Secure Boot: enabled".into() } else { "Secure Boot: DISABLED".into() },
            };
        }
    }
    CheckResult { ok: false, detail: "Secure Boot: EFI variable not readable".into() }
}

fn check_lockdown() -> CheckResult {
    match fs::read_to_string("/sys/kernel/security/lockdown") {
        Ok(content) => {
            let active = content.split_whitespace()
                .find(|s| s.starts_with('[') && s.ends_with(']'))
                .map(|s| s.trim_matches(|c| c == '[' || c == ']').to_string())
                .unwrap_or_else(|| "none".into());
            let ok = active == "integrity" || active == "confidentiality";
            CheckResult { ok, detail: format!("lockdown={}", active) }
        }
        Err(e) => CheckResult { ok: false, detail: format!("Cannot read lockdown: {}", e) },
    }
}

fn check_signing_cert() -> CheckResult {
    match fs::read_to_string("/proc/keys") {
        Ok(content) => {
            let found = content.lines().any(|l| l.contains(EXPECTED_CERT_CN));
            CheckResult {
                ok: found,
                detail: if found {
                    format!("'{}' found in kernel keyrings", EXPECTED_CERT_CN)
                } else {
                    format!("'{}' NOT found in kernel keyrings", EXPECTED_CERT_CN)
                },
            }
        }
        Err(e) => CheckResult { ok: false, detail: format!("Cannot read /proc/keys: {}", e) },
    }
}

fn check_dkms_modules() -> Vec<ModuleCheck> {
    let mut results = Vec::new();
    let Ok(kernels) = fs::read_dir(KERNEL_MODULE_DIRS) else { return results };

    for entry in kernels.flatten() {
        let kver = entry.file_name().to_string_lossy().to_string();
        let dkms_dir = format!("{}/{}/updates/dkms", KERNEL_MODULE_DIRS, kver);
        if !Path::new(&dkms_dir).exists() { continue; }

        for &modname in DKMS_MODULES {
            let mod_path = format!("{}/{}.ko.zst", dkms_dir, modname);
            if !Path::new(&mod_path).exists() { continue; }

            let output = Command::new("modinfo").arg(&mod_path).output();
            match output {
                Ok(out) => {
                    let stdout = String::from_utf8_lossy(&out.stdout);
                    let signer = stdout.lines()
                        .find(|l| l.starts_with("signer:"))
                        .map(|l| l.split_once(':').map(|x| x.1.trim().to_string()).unwrap_or_default());
                    let hashalgo = stdout.lines()
                        .find(|l| l.starts_with("sig_hashalgo:"))
                        .map(|l| l.split_once(':').map(|x| x.1.trim().to_string()).unwrap_or_default());
                    let ok = signer.as_deref() == Some(EXPECTED_CERT_CN)
                        && hashalgo.as_deref() == Some("sha512");
                    let detail = match (&signer, &hashalgo) {
                        (Some(s), Some(h)) => format!("signer={} algo={}", s, h),
                        _ => "no signature info".into(),
                    };
                    results.push(ModuleCheck { name: modname.into(), kernel: kver.clone(), ok, signer, sig_hashalgo: hashalgo, detail });
                }
                Err(e) => results.push(ModuleCheck {
                    name: modname.into(), kernel: kver.clone(), ok: false,
                    signer: None, sig_hashalgo: None, detail: format!("modinfo failed: {}", e),
                }),
            }
        }
    }
    results
}

fn check_systemd_failed() -> CheckResult {
    match Command::new("systemctl").args(["--failed", "--no-legend", "--no-pager"]).output() {
        Ok(out) => {
            let stdout = String::from_utf8_lossy(&out.stdout);
            let lines: Vec<&str> = stdout.lines().filter(|l| !l.trim().is_empty()).collect();
            if lines.is_empty() {
                CheckResult { ok: true, detail: "0 failed units".into() }
            } else {
                CheckResult { ok: false, detail: format!("{} failed: {}", lines.len(), lines.join(", ")) }
            }
        }
        Err(e) => CheckResult { ok: false, detail: format!("systemctl error: {}", e) },
    }
}

fn main() {
    let kernel = fs::read_to_string("/proc/version")
        .map(|s| s.split_whitespace().nth(2).unwrap_or("unknown").to_string())
        .unwrap_or_else(|_| "unknown".into());

    let secure_boot = check_secure_boot();
    let lockdown = check_lockdown();
    let signing_cert = check_signing_cert();
    let dkms_modules = check_dkms_modules();
    let systemd_failed = check_systemd_failed();

    let mut failures = Vec::new();
    if !secure_boot.ok { failures.push(format!("⚠️  {}", secure_boot.detail)); }
    if !lockdown.ok { failures.push(format!("⚠️  {}", lockdown.detail)); }
    if !signing_cert.ok { failures.push(format!("⚠️  {}", signing_cert.detail)); }
    for m in &dkms_modules {
        if !m.ok { failures.push(format!("⚠️  module {}/{}: {}", m.kernel, m.name, m.detail)); }
    }
    if !systemd_failed.ok { failures.push(format!("⚠️  {}", systemd_failed.detail)); }

    let overall = if failures.is_empty() { "healthy".to_string() } else { "degraded".to_string() };

    let status = HealthStatus {
        timestamp: Utc::now().to_rfc3339(),
        kernel: kernel.clone(),
        secure_boot, lockdown, signing_cert, dkms_modules, systemd_failed,
        overall: overall.clone(), failures: failures.clone(),
    };

    let _ = fs::create_dir_all(STATUS_DIR);
    if let Ok(json) = serde_json::to_string_pretty(&status) {
        let _ = fs::write(format!("{}/status.json", STATUS_DIR), &json);
    }

    let mut feed = format!("# Boot Health Feed\n*Updated: {}*\n\nKernel: {}\nOverall: **{}**\n\n",
        status.timestamp, kernel, overall);
    feed.push_str(&format!("- Secure Boot: {}\n", status.secure_boot.detail));
    feed.push_str(&format!("- Lockdown: {}\n", status.lockdown.detail));
    feed.push_str(&format!("- Signing cert: {}\n", status.signing_cert.detail));
    feed.push_str(&format!("- systemd: {}\n", status.systemd_failed.detail));
    if !failures.is_empty() {
        feed.push_str("\n## Failures\n");
        for f in &failures { feed.push_str(&format!("- {}\n", f)); }
    }
    let _ = fs::write(format!("{}/feed.md", STATUS_DIR), feed);

    for failure in &failures {
        let _ = Command::new(STASH_BIN).arg(format!("stratum-boot-health: {}", failure)).output();
    }

    println!("stratum-boot-health: {} (kernel: {})", overall, kernel);
    for f in &failures { println!("  {}", f); }
    if failures.is_empty() { println!("  ✅ All checks passed"); }

    std::process::exit(if overall == "degraded" { 1 } else { 0 });
}
