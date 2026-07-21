# Testing

已執行下列靜態與 smoke tests：

- 先建立測試用虛擬環境：`python3 -m venv .venv`
- 安裝鎖定依賴：`.venv/bin/python -m pip install --require-hashes -r requirements.txt`
- 更新依賴時先在虛擬環境安裝 `pip-tools`，再執行
  `pip-compile --upgrade --generate-hashes --strip-extras --output-file=requirements.txt requirements.in`。
  `requirements.in` 只記錄直接依賴意圖；`requirements.txt` 是指令產生、包含各發布架構 wheel hash 的鎖檔，不手動增刪 hash。
- 以下 Python 指令請以 `.venv/bin/python` 執行。
- `python3 -m py_compile backupctl homelab_backup/*.py`
- `bash -n install.sh`
- `bash -n backup-configs.sh`
- `bash -n restore-configs.sh`
- `bash -n config-ops-runtime.sh`
- `shellcheck install.sh backup-configs.sh restore-configs.sh config-ops-runtime.sh`
- `python3 -m unittest discover -s tests -v`
- repository 與模擬安裝後的 `backupctl --help`、`backupctl --version`

Python 單元測試依 package 元件拆分為 `test_schedule.py`、`test_config.py`、
`test_storage.py`、`test_backup.py`、`test_restore.py` 與 `test_cli.py`；共用測試
fixture 集中在 `tests/helpers.py`。

自動化測試涵蓋 manifest schema 與路徑安全、Cron 解析、staging 清理、
staging symlink 防護、managed-leaf control parent、filesystem allowlist、控制路徑與工作 root 隔離、hook／精確服務重啟、壞
manifest／排程的逐服務隔離、單次停服後同步的 slot 清理、同步時產生的 path inventory、
hook 動態產生 required path，以及 hook 前拒絕缺少的 required named volume、
retention 錯誤彙總、maintenance 後 repository check、snapshot host 範圍、現有部署／全新重建 restore preflight、
required／excluded sources、file／directory 型別漂移、volume 防清空、manifest
原子替換、非 TTY restore 確認政策、設定 archive 的 age 加密、
成員白名單、非 tmpfs fail-closed、跨 UID ciphertext 發布、設定 bundle rollback、
錯誤 KEY 的 rotation rollback，以及
repository／安裝後 launcher。跨 UID 案例需 root 與 `runuser`，條件不符時會明確 skip。

仍需在實際主機執行：

1. `sudo backupctl validate`
2. 建立測試 snapshot
3. 執行 `sudo backupctl restore <service>`，確認只下載到 private restore workspace，
   不會發布或修改 live manifest／Compose／payload
4. 在隔離環境準備所有 target 均不存在的狀態，執行 `restore --apply`，確認資料
   完成後才發布 Compose files 與 manifest
5. 執行互動式 `sudo backupctl restore`，確認預設全選與鍵盤操作
6. 在隔離環境測試 `--apply --start`
7. 以測試 SSH key 執行 `backup-configs.sh`、`restore-configs.sh` 與 `--rotate`

Rootful Docker 與 Btrfs 真實整合測試為 opt-in：

```bash
sudo HOMELAB_BACKUP_INTEGRATION=1 \
  HOMELAB_BACKUP_BTRFS_ROOT=/path/on/btrfs \
  python3 -m unittest tests.test_integration -v
```

未設定時會安全跳過；Docker 測試使用本機 helper image，Btrfs 測試只在明確指定
的 Btrfs root 下建立並刪除測試 subvolume。

## 一次性 QEMU VM

若 host 已安裝 `qemu-system-x86_64`、`qemu-img`、`genisoimage`、`curl`、
`ssh` 與 `rsync`，可建立隔離的 Ubuntu 24.04 VM 並執行完整測試：

```bash
./vm-test.sh all
```

腳本會下載官方 cloud image 並以官方 `SHA256SUMS` 驗證，建立 32 GiB sparse
qcow2 root overlay，以及一顆只供測試格式化的 8 GiB Btrfs 虛擬磁碟。Guest
內會安裝 rootful Docker、建立 helper image，然後以 root 執行全部單元測試及
Docker/Btrfs opt-in integration tests。Host 專案透過 SSH 複製；不會掛載 host
Docker socket 或 host filesystem。

常用操作：

```bash
./vm-test.sh test
./vm-test.sh start
./vm-test.sh ssh
./vm-test.sh stop
./vm-test.sh reset
```

`test`（以及預設的 `all`）每次都會先停止現有 VM，刪除並重建 root overlay、
Btrfs 測試磁碟與 cloud-init seed，再執行測試，最後關機。因此前一次執行留下的
檔案、Docker container/volume/image、Btrfs subvolume、套件或 Python venv 不會
影響下一次測試。已驗證且設為唯讀的 Ubuntu base image 與專用 SSH key 會保留，
它們不包含測試執行時產生的狀態。

若 `/dev/kvm` 可讀寫會自動使用 KVM，否則退回較慢的 TCG。VM 狀態保存在
git ignored 的 `.vm-test/`；`reset` 只移除 writable disks 與產生的 seed，保留
已驗證的 base image 和專用 SSH key。
