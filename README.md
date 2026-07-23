# Homelab Docker 備份

Docker Compose 服務的 Restic + OneDrive 備份工具。

- 每個 `/srv/stacks/**/backup.yaml` 自行宣告 Cron、retention、來源與一致性策略；
  可用安全的巢狀目錄分類服務，目錄位置不等於服務 ID。
- 支援 trusted root 下由容器 UID/GID 擁有的任意深層路徑，以及本機 rootful
  Docker named volume；中間 symlink 不會被跟隨，leaf symlink 只備份 link 本身。
- `backup.yaml` 會自動保存到每份 snapshot 的 `_meta/backup.yaml`，不必列入 `sources.paths`。
- 單一 systemd timer 每 5 分鐘掃描到期服務。
- 還原時可使用互動式選單，所有 repository 服務預設全選。
- 批次備份與還原會隔離各服務錯誤；單一服務失敗不會跳過後續服務，最後
  統一列出失敗摘要並回傳非零狀態。
- 備份以單份 manifest 為交易邊界；所有來源 staging 完成後才提交 Restic
  snapshot。任一來源失敗會跳過整份 manifest、清除半成品 staging，不產生
  該服務的部分備份。
- 一致性策略支援停服、hook、外部靜止、live best-effort 與 Btrfs subvolume
  snapshot；所有模式都可先執行具 timeout／required 政策的 argv action。
- 支援本機部署交叉驗證後覆寫，以及所有 target 均不存在的全新重建；混合
  狀態一律拒絕。

## 全新安裝

```bash
cd ~/Apps
git clone https://github.com/flier268/homelab-backup.git
cd homelab-backup
sudo ./install.sh
```

也可從 GitHub Releases 下載版本化的 `homelab-backup-X.Y.Z.tar.gz`，先用同一個
Release 內的 `SHA256SUMS` 驗證，再解壓並執行安裝：

```bash
sha256sum --check SHA256SUMS
tar -xzf homelab-backup-X.Y.Z.tar.gz
cd homelab-backup-X.Y.Z
sudo ./install.sh
```

維護者推送與 `homelab_backup.VERSION` 相同的 `vX.Y.Z` 標籤後，GitHub Actions
會在 CI 全數通過時自動建立 Release、產生 release notes，並附上 archive 與
checksum。一般 push 與 pull request 只執行 CI，不會發布。

升版時只需修改 `homelab_backup/__init__.py` 的 `VERSION`，不必全域取代測試
內容。提交並確認 CI 通過後，建立完全相同版本的 `vX.Y.Z` tag 並推送；
Release workflow 會拒絕 tag 與 `VERSION` 不一致的發布。

安裝期間不要關機；若因斷電中止，重新執行同一個 `install.sh` 讓完整 release
重新發布。安裝器不維護跨斷電 transaction journal。

## 升級

已安裝的版本可直接升級至 GitHub Releases 的最新穩定版：

```bash
sudo backupctl upgrade
```

此指令只允許從 `/usr/local/sbin/backupctl` 的 installed layout 執行，不支援從
Git checkout 啟動。它會先下載最新 Release 的 `SHA256SUMS`，確認版本高於目前
版本後才下載對應的 `homelab-backup-X.Y.Z.tar.gz`；若已是最新版則直接成功結束，
不下載 archive，也不允許降版或安裝 prerelease。

archive 必須完整符合 Release 內的 SHA-256，且封裝只能包含預期的單一版本目錄、
一般檔案與目錄，通過路徑及 `install.sh` 驗證後才會交給既有安裝器。checksum
與 archive 都透過 GitHub HTTPS 下載；此校驗沿用 Release 的 `SHA256SUMS`，
不另外提供獨立簽章信任根。

下載、校驗、解壓或安裝任一步驟失敗時，暫存內容都會清除。既有安裝器只有在
Python runtime、鎖定依賴、Docker helper、launcher 與 systemd units 全部準備
完成後才原子切換 `current`；失敗時目前版本仍維持啟用。

接著：

1. 設定 OneDrive rclone remote。
2. 編輯 `/etc/homelab-backup/config.yaml`。
3. 建立各服務的 `backup.yaml`。
4. 執行 `sudo backupctl validate`。
5. 執行 `sudo backupctl init` 建立全新 repository。
6. 手動備份並完成還原測試。
7. 執行 `sudo ./backup-configs.sh`，貼入 SSH 公鑰並加密災難復原必要設定。
8. 最後才啟用 systemd timers。

服務 ID、顯示名稱與部署目錄彼此獨立。例如：

```text
/srv/stacks/
└── Minecraft/
    └── Advent of Ascension Plus-2026/
        └── backup.yaml
```

```yaml
version: 1
service: advent-of-ascension-plus-2026
name: "Advent of Ascension Plus-2026"
```

`service` 是用於 CLI、Restic tag 與本機 state 的穩定唯一 ID，只能包含英數、
`.`、`_`、`-`；可選的 `name` 是顯示文字，可在單字間使用單一空白。不同分類下
的 `service` 仍不得重複。快照會記錄 manifest 相對目錄；本機設定不存在時，
新快照可將 manifest 還原到原分類位置，舊快照則回退到
`/srv/stacks/<service>`。

## 解除安裝

停止並停用 timers、移除 systemd units、`backupctl`、已安裝 releases 與其
Docker helper images：

```bash
sudo ./uninstall.sh
```

預設保留 `/etc/homelab-backup`、`/var/lib/homelab-backup` 與
`/var/cache/homelab-backup`，方便稍後重新安裝。若確定連本機設定、密碼、state、
staging、restore workspace 與 cache 都不再需要，可明確要求清除：

```bash
sudo ./uninstall.sh --purge
```

兩種模式都不會移除安裝器透過 APT 安裝的共用系統套件、遠端 Restic repository，
也不會刪除 `/srv/stacks` 或 `/srv/data`。執行中的手動 `backupctl` 程序仍在使用
release 時，解除安裝會拒絕刪除程式檔。

## 災難復原必要設定

Git 保存模式只建立加密 archive：

```text
configs/homelab-backup-configs.zip.age
```

```bash
sudo ./backup-configs.sh
```

此腳本只允許 root 執行。明文只會在 `/run` 的 root 專用暫存目錄中封裝，
離開暫存目錄前會先用貼入的 `ssh-ed25519` 或 `ssh-rsa` 公鑰經 `age`
加密，並先確認 `/run` 確實位於 tmpfs。Git 模式只提交密文，另一模式會產生
timestamped 密文。

邊界：這是全新專案，只支援目前的加密 archive，不包含舊明文設定或舊 archive
遷移流程。全域設定、`backup.yaml` 與 snapshot inventory 都使用目前唯一的
`version: 1`；不處理升級或舊版 fallback。

## 支援與安全邊界

- 僅支援 Linux、本機 rootful Docker，以及 `ext4`、`xfs`、`btrfs`。
- 備份 path 必須位於唯一 trusted root 之下；控制路徑由 root 管理且不可經過
  symlink、跨 mount 或 nested Btrfs subvolume。資料樹可由容器 UID/GID 擁有。
- 支援普通檔案、目錄、symlink、ACL、xattr 與 payload 內 hardlink；拒絕 FIFO、
  socket 與 device node。還原會保留原始數字 UID/GID 與 mode。
- `stop`、`hooks`、`external` 會拒絕已知 writer；`live` 與未完整受 Btrfs
  snapshot 保護的來源只提供 `best-effort` 一致性。
- 不支援 remote/rootless Docker、跨 mount、ZFS、ext2/3、FUSE、NFS 或 autofs。
- 所有 Compose YAML、`compose.env_file`、Restic 密碼與 rclone 設定必須是
  root-owned、不可 group/world write 的普通檔案，且不可為 symlink。Compose
  不載入隱含 `.env` 或外部 `COMPOSE_*` 設定；需要插值時必須在 manifest 明確設定：

  ```yaml
  compose:
    files: [compose.yaml]
    env_file: compose.env
  ```
- Actions 不經 shell，必須指定 `run_as`，且不可把密碼或 token 放入 argv。
  完整欄位、writer 判定與 Btrfs 限制請見 `GUIDE.html`。

從預設加密檔放回系統，依提示貼入 SSH 私鑰並解密：

```bash
sudo ./restore-configs.sh
```

非互動執行時需明確加上 `--yes`：

```bash
sudo ./restore-configs.sh --yes \
  /safe/path/configs.zip.age
```

換 KEY 不必重新讀取目前系統設定。輸入舊私鑰解密，接著貼入新公鑰：

```bash
sudo ./backup-configs.sh --rotate configs/homelab-backup-configs.zip.age
```

腳本會先以舊私鑰解密並驗證 archive 內容，再以新 age KEY 加密並原子替換。
私鑰只直接傳給 `age`，不放入參數、環境變數或一般磁碟。

## 手動備份與空間保護

```bash
sudo backupctl backup minecraft
sudo backupctl backup advent-of-ascension-plus-2026
```

Palworld 零停機範例可參考 `examples/palworld.backup.yaml`。若其中宣告的資料 path
本身是 Btrfs subvolume，`snapshot` 會從唯讀 filesystem snapshot staging；若只是
普通目錄，仍會直接 staging，偵測到 writer 時整份備份會標為 `best-effort`。

備份會在來源靜止後保守估算 path 與 Docker volume 的 staging 大小，並要求
staging filesystem 至少保留 1 GiB。空間不足時預設中止；只有人工操作可明確
放行，排程 `run-due` 不會自動略過保護：

```bash
sudo backupctl backup minecraft --allow-low-space
```

## 還原全部服務

互動選取，預設全選：

```bash
sudo backupctl restore --apply --start
```

非互動式全部還原：

```bash
sudo backupctl restore --all \
  --restore-manifest --apply --start --yes
```

安全政策：只要 stdin 不是 TTY，所有 `restore`（包含只下載、不加
`--apply`）都必須明確指定 `--yes`。

還原前會檢查 snapshot 身分與可用空間；`--yes` 不會略過空間保護。現有部署
必須通過 manifest 交叉驗證，全新重建則要求所有 target 都不存在；混合狀態
一律拒絕。歷史 snapshot、刪除與 restore workspace 清理方式請見 `GUIDE.html`。

完整說明請開啟 `GUIDE.html`。
